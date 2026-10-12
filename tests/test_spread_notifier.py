"""
Неблокирующий отправитель уведомлений: порог спреда, кулдаун на символ, лимит в минуту,
защита от серии сбоев и слив очереди при остановке. Время — ручное, транспорт — фейковый.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.core.domain.notifications import NotificationDelivery
from futures_arb_bot.core.domain.options import BotOptions, NotificationOptions
from futures_arb_bot.core.engine.spread_notifier import QueuedSpreadNotifier

from .fakes import D, FakeConfigProvider, FakeTransport, ManualClock, make_log

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def configured(tweak: Callable[[NotificationOptions], None] | None = None) -> BotOptions:
    options = BotOptions()
    options.general.write_log_file = False
    options.arbitrage.min_spread_percent_up = D("0.4")
    options.notifications.enabled = True
    options.notifications.bot_token = "token"
    options.notifications.admin_chat_id = "42"
    options.notifications.max_retries = 0
    if tweak is not None:
        tweak(options.notifications)
    return options


def estimate(symbol: str = "BTC/USDT:USDT", net=D("0.8")) -> SpreadEstimate:
    return SpreadEstimate(
        symbol, OpportunityLeg("binanceusdm", D(100), D("0.1")), OpportunityLeg("bybit", D(101), D("0.1")), D(1), net, D(1_000_000), NOW
    )


@asynccontextmanager
async def create(options: BotOptions, clock: ManualClock | None = None) -> AsyncIterator[tuple[QueuedSpreadNotifier, FakeTransport]]:
    clock = clock or ManualClock(NOW)
    config = FakeConfigProvider(options)
    transport = FakeTransport()
    notifier = QueuedSpreadNotifier(config, transport, make_log(config, clock), clock)
    try:
        yield notifier, transport
    finally:
        await notifier.aclose()


async def wait_until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    """Дождаться условия, пока фоновая задача обрабатывает очередь."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition() and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert condition(), "фоновая отправка не дождалась условия"


def set_attr(name: str, value):
    return lambda n: setattr(n, name, value)


async def test_disabled_section_sends_nothing():
    async with create(configured(set_attr("enabled", False))) as (notifier, transport):
        notifier.notify(estimate())
        await asyncio.sleep(0.03)
        assert transport.call_count == 0
        assert notifier.pending_count == 0


async def test_missing_token_or_chat_sends_nothing():
    async with create(configured(set_attr("admin_chat_id", ""))) as (notifier, transport):
        notifier.notify(estimate())
        await asyncio.sleep(0.03)
        assert transport.call_count == 0


async def test_threshold_inherited_from_arbitrage_entry():
    options = configured(set_attr("signal_cooldown_seconds", 0))
    options.arbitrage.min_spread_percent_up = D("0.9")
    async with create(options) as (notifier, transport):
        notifier.notify(estimate(net=D("0.8")))
        await asyncio.sleep(0.03)
        assert transport.call_count == 0

        notifier.notify(estimate(net=D("1.2")))
        await wait_until(lambda: transport.call_count == 1)


async def test_own_threshold_overrides_arbitrage_entry():
    async with create(configured(set_attr("min_spread_percent", D("1.5")))) as (notifier, transport):
        notifier.notify(estimate(net=D("1.4")))
        await asyncio.sleep(0.03)
        assert transport.call_count == 0

        notifier.notify(estimate(net=D("1.6")))
        await wait_until(lambda: transport.call_count == 1)
        [sent] = transport.sent
        assert "+1.600%" in sent


async def test_cooldown_holds_the_same_symbol():
    clock = ManualClock(NOW)
    async with create(configured(set_attr("signal_cooldown_seconds", 120)), clock) as (notifier, transport):
        notifier.notify(estimate())
        await wait_until(lambda: transport.call_count == 1)

        notifier.notify(estimate())
        await asyncio.sleep(0.03)
        assert transport.call_count == 1

        clock.advance(timedelta(seconds=121))
        notifier.notify(estimate())
        await wait_until(lambda: transport.call_count == 2)


async def test_different_symbols_do_not_share_cooldown():
    async with create(configured()) as (notifier, transport):
        notifier.notify(estimate("BTC/USDT:USDT"))
        notifier.notify(estimate("ETH/USDT:USDT"))
        await wait_until(lambda: transport.call_count == 2)


async def test_per_minute_limit_drops_the_rest():
    def tweak(n: NotificationOptions) -> None:
        n.signal_cooldown_seconds = 0
        n.max_per_minute = 2

    async with create(configured(tweak)) as (notifier, transport):
        for symbol in ("A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT", "D/USDT:USDT"):
            notifier.notify(estimate(symbol))

        await wait_until(lambda: transport.call_count == 2)
        await asyncio.sleep(0.03)

        assert transport.call_count == 2
        assert notifier.suppressed_count >= 2


async def test_transient_failures_open_the_circuit():
    def tweak(n: NotificationOptions) -> None:
        n.signal_cooldown_seconds = 0
        n.failure_circuit_limit = 2

    async with create(configured(tweak)) as (notifier, transport):
        transport.outcomes.append(NotificationDelivery.transient("5xx"))

        notifier.notify(estimate("A/USDT:USDT"))
        notifier.notify(estimate("B/USDT:USDT"))
        await wait_until(lambda: transport.call_count == 2)

        notifier.notify(estimate("C/USDT:USDT"))
        await asyncio.sleep(0.05)
        assert transport.call_count == 2


async def test_permanent_failure_opens_the_circuit_at_once():
    async with create(configured(set_attr("signal_cooldown_seconds", 0))) as (notifier, transport):
        transport.outcomes.append(NotificationDelivery.permanent("401 verify.token"))

        notifier.notify(estimate("A/USDT:USDT"))
        await wait_until(lambda: transport.call_count == 1)

        notifier.notify(estimate("B/USDT:USDT"))
        await asyncio.sleep(0.05)
        assert transport.call_count == 1


async def test_success_resets_the_failure_counter():
    def tweak(n: NotificationOptions) -> None:
        n.signal_cooldown_seconds = 0
        n.failure_circuit_limit = 2

    async with create(configured(tweak)) as (notifier, transport):
        transport.outcomes.append(NotificationDelivery.transient("5xx"))
        transport.outcomes.append(None)

        notifier.notify(estimate("A/USDT:USDT"))
        await wait_until(lambda: transport.call_count == 1)

        notifier.notify(estimate("B/USDT:USDT"))
        await wait_until(lambda: transport.call_count == 2)

        notifier.notify(estimate("C/USDT:USDT"))
        await wait_until(lambda: transport.call_count == 3)


async def test_close_drains_the_pending_queue():
    options = configured(set_attr("signal_cooldown_seconds", 0))
    config = FakeConfigProvider(options)
    clock = ManualClock(NOW)
    transport = FakeTransport()
    transport.delay = timedelta(milliseconds=40)
    notifier = QueuedSpreadNotifier(config, transport, make_log(config, clock), clock)

    notifier.notify(estimate("A/USDT:USDT"))
    notifier.notify(estimate("B/USDT:USDT"))
    await notifier.aclose()

    assert transport.call_count == 2
