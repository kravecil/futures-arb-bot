"""
Фильтр открытия по фандингу: чистый фандинг пары за интервал — рейт шорта минус рейт лонга.
Если он строго ниже arbitrage.min_funding_rate_percent — сделка пропускается целиком.
Нет данных хотя бы по одной ноге — fail-open (сделка разрешена).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.domain.options import ArbitrageOptions, BotOptions
from futures_arb_bot.core.engine.arbitrage_scanner import DefaultArbitrageScanner
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.spread_calculator import DefaultSpreadCalculator
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.symbol_filter import DefaultSymbolFilter

from .fakes import D, FakeConfigProvider, FakeConnector, FakeExecutor, FakeNotifier, FixedClock, make_log, ticker

SYMBOL = "BTC/USDT:USDT"
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NONE = CancellationToken.none()


@dataclass
class Harness:
    scanner: DefaultArbitrageScanner
    long: FakeConnector
    short: FakeConnector
    executor: FakeExecutor
    notifier: FakeNotifier
    options: BotOptions


async def create() -> Harness:
    options = BotOptions()
    options.general.write_log_file = False
    options.arbitrage.enabled = True
    options.arbitrage.min_spread_percent_up = D("0.4")
    options.arbitrage.slippage_buffer_percent = D(0)
    config = FakeConfigProvider(options)

    # спред: ask дешёвой 100 → bid дорогой 101, комиссии круга 0.1×4 → нетто 0.6% > порога 0.4%
    long_connector = FakeConnector("binanceusdm")
    long_connector.tickers = {SYMBOL: ticker("binanceusdm", SYMBOL, bid=D("99.9"), ask=D(100), ts=NOW)}
    short_connector = FakeConnector("bybit")
    short_connector.tickers = {SYMBOL: ticker("bybit", SYMBOL, bid=D(101), ask=D("101.1"), ts=NOW)}

    registry = ConnectorRegistry()
    await registry.replace([long_connector, short_connector])

    clock = FixedClock(NOW)
    executor = FakeExecutor()
    notifier = FakeNotifier()
    scanner = DefaultArbitrageScanner(
        config,
        registry,
        DefaultSpreadCalculator(config, clock),
        DefaultSymbolFilter(config),
        executor,
        DefaultStatisticsCollector(config, clock),
        notifier,
        make_log(config, clock),
        clock,
    )
    return Harness(scanner, long_connector, short_connector, executor, notifier, options)


def enable_notifications(options: BotOptions) -> None:
    options.notifications.enabled = True
    options.notifications.bot_token = "token"
    options.notifications.admin_chat_id = "42"


def test_default_limit_is_minus_one_percent():
    assert ArbitrageOptions().min_funding_rate_percent == D(-1)


async def test_pair_income_above_limit_passes_and_funding_is_on_legs():
    # чистый доход пары = −0.2 − (−0.5) = +0.3 %, выше предела −1 % — сделка разрешена
    h = await create()
    h.long.funding_rates_percent[SYMBOL] = D("-0.5")
    h.short.funding_rates_percent[SYMBOL] = D("-0.2")

    await h.scanner.scan_once(NONE)

    [candidate] = h.executor.processed
    assert candidate.symbol == SYMBOL
    assert candidate.long_leg.funding_percent == D("-0.5")
    assert candidate.short_leg.funding_percent == D("-0.2")


async def test_pair_paying_over_limit_whole_pair_skipped():
    # чистый доход пары −1.7 %, что ниже предела −1 % — сделка не открывается вовсе
    h = await create()
    h.long.funding_rates_percent[SYMBOL] = D("1.5")
    h.short.funding_rates_percent[SYMBOL] = D("-0.2")

    await h.scanner.scan_once(NONE)

    assert h.executor.processed == []


async def test_deep_negative_funding_on_long_leg_is_income_not_a_reason_to_skip():
    # лонг с отрицательным рейтом нам платит: доход пары +1.0 %
    h = await create()
    h.long.funding_rates_percent[SYMBOL] = D("-1.5")
    h.short.funding_rates_percent[SYMBOL] = D("-0.5")

    await h.scanner.scan_once(NONE)

    assert len(h.executor.processed) == 1


async def test_skew_between_legs_is_skipped_although_each_rate_looks_normal():
    # по каждой ноге рейты «в норме», но платить пара должна 1.1 % за интервал
    h = await create()
    h.long.funding_rates_percent[SYMBOL] = D("0.9")
    h.short.funding_rates_percent[SYMBOL] = D("-0.2")

    await h.scanner.scan_once(NONE)

    assert h.executor.processed == []


async def test_missing_funding_fails_open():
    h = await create()  # фандинга нет у обеих бирж — сделку не блокируем

    await h.scanner.scan_once(NONE)

    [candidate] = h.executor.processed
    assert candidate.long_leg.funding_percent is None
    assert candidate.short_leg.funding_percent is None


async def test_funding_endpoint_failure_fails_open():
    h = await create()
    h.short.fail_fetch_funding_rates = True
    h.long.funding_rates_percent[SYMBOL] = D("-0.5")

    await h.scanner.scan_once(NONE)

    [candidate] = h.executor.processed
    assert candidate.short_leg.funding_percent is None
    assert candidate.long_leg.funding_percent == D("-0.5")


async def test_filtered_candidate_is_also_handed_to_notifier():
    h = await create()
    enable_notifications(h.options)
    h.long.funding_rates_percent[SYMBOL] = D("-0.5")
    h.short.funding_rates_percent[SYMBOL] = D("-0.2")

    await h.scanner.scan_once(NONE)

    assert len(h.notifier.notified) == 1


async def test_rejected_by_funding_candidate_is_not_notified():
    h = await create()
    enable_notifications(h.options)
    h.long.funding_rates_percent[SYMBOL] = D("1.5")
    h.short.funding_rates_percent[SYMBOL] = D("-0.2")

    await h.scanner.scan_once(NONE)

    assert h.notifier.notified == []


async def test_disabled_notifications_section_notifies_nothing():
    h = await create()
    h.long.funding_rates_percent[SYMBOL] = D("-0.5")
    h.short.funding_rates_percent[SYMBOL] = D("-0.2")

    await h.scanner.scan_once(NONE)

    assert h.notifier.notified == []


async def test_rate_equal_to_limit_passes_strictly_below_rejects():
    h = await create()
    h.long.funding_rates_percent[SYMBOL] = D(1)
    h.short.funding_rates_percent[SYMBOL] = D(0)  # чистый доход пары ровно −1 % — предел, не «ниже»

    await h.scanner.scan_once(NONE)

    assert len(h.executor.processed) == 1


async def test_configurable_limit_is_respected():
    h = await create()
    h.options.arbitrage.min_funding_rate_percent = D(0)  # связка не должна платить фандинг вовсе
    h.long.funding_rates_percent[SYMBOL] = D("0.1")
    h.short.funding_rates_percent[SYMBOL] = D("-0.001")

    await h.scanner.scan_once(NONE)

    assert h.executor.processed == []


async def test_legs_with_too_different_timestamps_do_not_make_a_signal():
    # котировка дешёвой биржи на 20 с «старше» котировки дорогой — сравнивать их уже нельзя
    h = await create()
    h.options.symbols.max_ticker_age_seconds = 600
    h.options.symbols.max_ticker_skew_seconds = 5
    h.long.tickers = {SYMBOL: ticker("binanceusdm", SYMBOL, bid=D("99.9"), ask=D(100), ts=NOW - timedelta(seconds=20))}

    await h.scanner.scan_once(NONE)

    assert h.executor.processed == []


async def test_legs_within_skew_limit_still_make_a_signal():
    h = await create()
    h.options.symbols.max_ticker_age_seconds = 600
    h.options.symbols.max_ticker_skew_seconds = 30
    h.long.tickers = {SYMBOL: ticker("binanceusdm", SYMBOL, bid=D("99.9"), ask=D(100), ts=NOW - timedelta(seconds=20))}

    await h.scanner.scan_once(NONE)

    assert len(h.executor.processed) == 1


async def test_quote_age_of_signal_is_measured():
    h = await create()
    h.options.symbols.max_ticker_age_seconds = 600
    h.options.symbols.max_ticker_skew_seconds = 5
    # котировка дешёвой биржи снята на 2 с раньше «сейчас», дорогой — в «сейчас»
    h.long.tickers = {SYMBOL: ticker("binanceusdm", SYMBOL, bid=D("99.9"), ask=D(100), ts=NOW - timedelta(seconds=2))}

    await h.scanner.scan_once(NONE)

    [candidate] = h.executor.processed
    assert candidate.long_leg.quoted_at == NOW - timedelta(seconds=2)
    assert candidate.short_leg.quoted_at == NOW
    snapshot = h.scanner.snapshot
    assert snapshot is not None and snapshot.last_tick_ms is not None
    latency = h.scanner._stats.snapshot(NOW).latency
    assert latency is not None and latency.max_quote_age_ms == 2000
