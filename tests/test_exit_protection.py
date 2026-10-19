"""
Защита выхода от фантомных сигналов (разбор 19.10.2026, CT/USDT): тейк-профит/стоп-лосс подтверждаются
несколькими сканами, тейк-профит требует запаса по PnL, свежих котировок и подтверждения стаканом,
после закрытия символ на паузе перед повторным входом.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.core.domain.enums import CloseReason, NetworkMode
from futures_arb_bot.core.domain.market_data import OrderBookSnapshot
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.trade_executor import ArbTradeExecutor

from .fakes import D, FakeConfigProvider, FakeConnector, ManualClock, make_log, ticker

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NONE = CancellationToken.none()
BTC = "BTC/USDT:USDT"


async def create(**arbitrage):
    config = FakeConfigProvider()
    arb = config.current.arbitrage
    config.current.general.network_mode = NetworkMode.DRY_RUN
    arb.enabled = True
    arb.order_size_usd = D(100)
    arb.min_spread_percent_up = D("0.4")
    arb.min_spread_percent_down = D("0.15")
    arb.slippage_buffer_percent = D(0)
    for key, value in arbitrage.items():
        setattr(arb, key, value)

    long_c = FakeConnector("binanceusdm", taker_percent=D("0.1"))
    short_c = FakeConnector("bybit", taker_percent=D("0.1"))
    registry = ConnectorRegistry()
    await registry.replace([long_c, short_c])

    clock = ManualClock(NOW)
    log = make_log(config, clock)
    stats = DefaultStatisticsCollector(config, clock)
    executor = ArbTradeExecutor(config, registry, stats, log, clock)
    return executor, long_c, short_c, stats, clock, log


def estimate(long_price: Decimal = D(100), short_price: Decimal = D(101)) -> SpreadEstimate:
    gross = (short_price - long_price) / long_price * 100
    return SpreadEstimate(
        BTC, OpportunityLeg("binanceusdm", long_price, D("0.1")), OpportunityLeg("bybit", short_price, D("0.1")), gross, gross - D("0.4"), D(1e7), NOW
    )


def quote(long_c, short_c, clock, long_bid, long_ask, short_bid, short_ask, age=timedelta(0)):
    ts = clock.now() - age
    long_c.tickers = {BTC: ticker("binanceusdm", BTC, bid=long_bid, ask=long_ask, ts=ts)}
    short_c.tickers = {BTC: ticker("bybit", BTC, bid=short_bid, ask=short_ask, ts=ts)}


def converged(long_c, short_c, clock, age=timedelta(0)):
    """Спред сошёлся: выход даёт ~+0.4 % после комиссий (вход 100/101)."""
    quote(long_c, short_c, clock, D("100.4"), D("100.5"), D("100.6"), D("100.7"), age)


def exploded(long_c, short_c, clock):
    """Спред разошёлся выше стоп-лосса (по умолчанию 1 %)."""
    quote(long_c, short_c, clock, D("99.9"), D(100), D("103"), D("103.1"))


async def tick(executor, long_c, short_c):
    await executor.manage_open_positions({"binanceusdm": long_c.tickers, "bybit": short_c.tickers}, NONE)


def closed(stats):
    return stats.snapshot(NOW + timedelta(hours=1)).closed_positions


def opened(stats) -> int:
    return stats.snapshot(NOW + timedelta(hours=1)).orders_opened


async def test_take_profit_waits_for_confirmation_scans():
    executor, long_c, short_c, stats, clock, _ = await create(exit_confirm_scans=2)
    await executor.process_opportunities([estimate()], NONE)

    converged(long_c, short_c, clock)
    await tick(executor, long_c, short_c)
    assert executor.has_open_positions  # один скан — ещё не выход

    await tick(executor, long_c, short_c)
    assert not executor.has_open_positions
    assert closed(stats)[0].reason == CloseReason.TAKE_PROFIT


async def test_single_scan_blip_resets_exit_confirmation():
    executor, long_c, short_c, _stats, clock, _ = await create(exit_confirm_scans=2)
    await executor.process_opportunities([estimate()], NONE)

    converged(long_c, short_c, clock)
    await tick(executor, long_c, short_c)
    quote(long_c, short_c, clock, D("99.9"), D(100), D(101), D("101.1"))  # спред вернулся к входному
    await tick(executor, long_c, short_c)
    converged(long_c, short_c, clock)
    await tick(executor, long_c, short_c)

    assert executor.has_open_positions  # серия прервалась — снова нужен второй скан подряд


async def test_stop_loss_also_needs_confirmation_scans():
    executor, long_c, short_c, stats, clock, _ = await create(exit_confirm_scans=2)
    await executor.process_opportunities([estimate()], NONE)

    exploded(long_c, short_c, clock)
    await tick(executor, long_c, short_c)
    assert executor.has_open_positions

    await tick(executor, long_c, short_c)
    assert closed(stats)[0].reason == CloseReason.STOP_LOSS


async def test_take_profit_requires_min_exit_profit():
    executor, long_c, short_c, _stats, clock, _ = await create(min_exit_profit_percent=D("0.5"))
    await executor.process_opportunities([estimate()], NONE)

    converged(long_c, short_c, clock)  # расчётный PnL выхода ~+0.4 % < 0.5 %
    await tick(executor, long_c, short_c)
    assert executor.has_open_positions

    quote(long_c, short_c, clock, D("100.6"), D("100.7"), D("100.6"), D("100.7"))  # ~+0.6 %
    await tick(executor, long_c, short_c)
    assert not executor.has_open_positions


async def test_take_profit_is_deferred_on_stale_quotes_but_stop_loss_is_not():
    executor, long_c, short_c, _stats, clock, log = await create(exit_max_quote_age_ms=2000)
    await executor.process_opportunities([estimate()], NONE)

    converged(long_c, short_c, clock, age=timedelta(seconds=5))
    await tick(executor, long_c, short_c)
    assert executor.has_open_positions
    assert any("тейк-профит отложен" in e.message for e in log.latest(50))

    converged(long_c, short_c, clock)  # котировки свежие
    await tick(executor, long_c, short_c)
    assert not executor.has_open_positions

    # стоп-лосс по старой котировке всё равно срабатывает
    executor2, long2, short2, stats2, clock2, _ = await create(exit_max_quote_age_ms=2000)
    await executor2.process_opportunities([estimate()], NONE)
    quote(long2, short2, clock2, D("99.9"), D(100), D("103"), D("103.1"), age=timedelta(seconds=5))
    await tick(executor2, long2, short2)
    assert closed(stats2)[0].reason == CloseReason.STOP_LOSS


async def test_take_profit_rejected_when_order_book_does_not_confirm():
    executor, long_c, short_c, stats, clock, log = await create(exit_confirm_order_book=True)
    await executor.process_opportunities([estimate()], NONE)

    converged(long_c, short_c, clock)
    # котировки обещают прибыль, а реальный стакан — нет (лонг продастся только по 99.5)
    deep = D(1_000_000)
    long_c.order_books[BTC] = OrderBookSnapshot("binanceusdm", BTC, ((D("99.5"), deep),), ((D("100.5"), deep),), clock.now())
    await tick(executor, long_c, short_c)

    assert executor.has_open_positions
    assert any("тейк-профит не подтвердился стаканом" in e.message for e in log.latest(50))

    # стакан догнал котировки — после паузы перепроверки позиция закрывается
    del long_c.order_books[BTC]
    clock.advance(timedelta(seconds=3))
    converged(long_c, short_c, clock)
    await tick(executor, long_c, short_c)
    assert not executor.has_open_positions
    assert closed(stats)[0].pnl_usd > 0


async def test_reentry_cooldown_blocks_same_symbol():
    executor, long_c, short_c, stats, clock, _ = await create(reentry_cooldown_seconds=60, stop_loss_cooldown_seconds=600)
    await executor.process_opportunities([estimate()], NONE)
    converged(long_c, short_c, clock)
    await tick(executor, long_c, short_c)
    assert opened(stats) == 1 and not executor.has_open_positions

    clock.advance(timedelta(seconds=30))
    await executor.process_opportunities([estimate()], NONE)
    assert opened(stats) == 1  # пауза после тейк-профита

    clock.advance(timedelta(seconds=31))
    await executor.process_opportunities([estimate()], NONE)
    assert opened(stats) == 2

    # после стоп-лосса пауза длиннее
    exploded(long_c, short_c, clock)
    await tick(executor, long_c, short_c)
    assert closed(stats)[-1].reason == CloseReason.STOP_LOSS

    clock.advance(timedelta(seconds=120))
    await executor.process_opportunities([estimate()], NONE)
    assert opened(stats) == 2

    clock.advance(timedelta(seconds=500))
    await executor.process_opportunities([estimate()], NONE)
    assert opened(stats) == 3
