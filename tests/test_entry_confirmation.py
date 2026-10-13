"""
Защита входа от фантомных спредов: связка должна продержаться confirm_scans сканов подряд и
подтвердиться свежими стаканами обеих бирж с учётом глубины под объём сделки. Объём сделки —
общий для обеих ног, кратный шагу лота каждой биржи.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.core.domain.enums import NetworkMode, OrderSide
from futures_arb_bot.core.domain.market_data import MarketInfo, OrderBookSnapshot
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.trade_executor import ArbTradeExecutor, _common_step

from .fakes import D, FakeConfigProvider, FakeConnector, ManualClock, make_log, ticker

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NONE = CancellationToken.none()
BTC = "BTC/USDT:USDT"


async def create(confirm_scans: int = 1, confirm_order_book: bool = False, mode: NetworkMode = NetworkMode.DRY_RUN):
    config = FakeConfigProvider()
    arb = config.current.arbitrage
    config.current.general.network_mode = mode
    arb.enabled = True
    arb.order_size_usd = D(100)
    arb.min_spread_percent_up = D("0.4")
    arb.slippage_buffer_percent = D(0)
    arb.confirm_scans = confirm_scans
    arb.confirm_order_book = confirm_order_book
    arb.order_execution_timeout_ms = 250
    arb.order_poll_interval_ms = 50

    long_c = FakeConnector("binanceusdm", taker_percent=D("0.1"))
    short_c = FakeConnector("bybit", taker_percent=D("0.1"))
    long_c.tickers = {BTC: ticker("binanceusdm", BTC, bid=D("99.9"), ask=D(100))}
    short_c.tickers = {BTC: ticker("bybit", BTC, bid=D(101), ask=D("101.1"))}
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


def opened(stats) -> int:
    return stats.snapshot(NOW + timedelta(minutes=1)).orders_opened


# ------------------------- устойчивость сигнала -------------------------


async def test_single_scan_signal_is_not_traded_when_two_scans_required():
    executor, *_, stats, _, _ = await create(confirm_scans=2)

    await executor.process_opportunities([estimate()], NONE)
    assert opened(stats) == 0

    await executor.process_opportunities([estimate()], NONE)
    assert opened(stats) == 1


async def test_signal_streak_resets_when_spread_disappears():
    executor, *_, stats, _, _ = await create(confirm_scans=2)

    await executor.process_opportunities([estimate()], NONE)
    await executor.process_opportunities([], NONE)  # скан без сигнала: «скачок» был разовым
    await executor.process_opportunities([estimate()], NONE)

    assert opened(stats) == 0


# ------------------------- подтверждение стаканом -------------------------


async def test_phantom_spread_is_rejected_by_order_book():
    executor, long_c, _short_c, stats, _, log = await create(confirm_order_book=True)
    # котировка дешёвой биржи устарела: на деле ask там уже 101, а не 100
    long_c.order_books[BTC] = OrderBookSnapshot("binanceusdm", BTC, ((D("100.9"), D(10)),), ((D(101), D(10)),))

    await executor.process_opportunities([estimate()], NONE)

    assert opened(stats) == 0
    assert any("не подтвердился стаканом" in e.message for e in log.latest(20))


async def test_thin_book_is_rejected():
    executor, long_c, _, stats, _, log = await create(confirm_order_book=True)
    long_c.order_books[BTC] = OrderBookSnapshot("binanceusdm", BTC, ((D("99.9"), D(10)),), ((D(100), D("0.01")),))  # 0.01 BTC < объёма

    await executor.process_opportunities([estimate()], NONE)

    assert opened(stats) == 0
    assert any("не хватает глубины" in e.message for e in log.latest(20))


async def test_missing_book_blocks_entry_and_rejection_is_cached():
    executor, long_c, _, stats, clock, _ = await create(confirm_order_book=True)
    long_c.order_books[BTC] = None

    await executor.process_opportunities([estimate()], NONE)
    await executor.process_opportunities([estimate()], NONE)
    assert opened(stats) == 0
    assert len(long_c.order_book_calls) == 1  # повторно не дёргаем биржу, пока идёт пауза

    clock.advance(timedelta(seconds=11))
    long_c.order_books.pop(BTC)  # стакан снова доступен (строится из тикера)
    await executor.process_opportunities([estimate()], NONE)
    assert opened(stats) == 1


async def test_confirmed_entry_uses_order_book_prices():
    executor, long_c, short_c, _, _, _ = await create(confirm_order_book=True, mode=NetworkMode.TESTNET)
    executor._config.current.arbitrage.execution.type = None
    # чтобы набрать 1 BTC, придётся взять второй уровень asks: худшая цена 100.2
    long_c.order_books[BTC] = OrderBookSnapshot("binanceusdm", BTC, ((D("99.9"), D(5)),), ((D(100), D("0.5")), (D("100.2"), D(5))))
    short_c.order_books[BTC] = OrderBookSnapshot("bybit", BTC, ((D("101.5"), D(5)),), ((D("101.6"), D(5)),))

    await executor.process_opportunities([estimate()], NONE)

    [buy] = [o for o in long_c.placed_orders.values() if not o.reduce_only]
    assert buy.side == OrderSide.BUY
    assert buy.price == D("100.2")  # от цены набора объёма в стакане, а не от устаревшей котировки 100


# ------------------------- общий шаг лота -------------------------


def test_common_step_is_lcm_of_decimal_steps():
    assert _common_step([D("0.001"), D(10)]) == D(10)
    assert _common_step([D("0.4"), D("0.6")]) == D("1.2")
    assert _common_step([D("0.1")]) == D("0.1")
    assert _common_step([]) is None


async def test_trade_amount_is_a_multiple_of_both_lot_steps():
    executor, long_c, short_c, _, _, _ = await create(mode=NetworkMode.TESTNET)
    executor._config.current.arbitrage.order_size_usd = D(25)
    # «OKX»: 1 контракт = 10 монет; «Bybit»: шаг 0.1 монеты
    long_c._markets = [MarketInfo(BTC, True, True, "USDT", D(10), D("0.1"), D("0.02"), D(10), D(10))]
    short_c._markets = [MarketInfo(BTC, True, True, "USDT", D("0.1"), D("0.1"), D("0.02"), D(1), D("0.1"))]

    await executor.process_opportunities([estimate(D("0.07"), D("0.0708"))], NONE)

    entry_long = [o for o in long_c.placed_orders.values() if not o.reduce_only]
    entry_short = [o for o in short_c.placed_orders.values() if not o.reduce_only]
    assert entry_long and entry_short
    # 25 $ / 0.07 = 357.1 монеты → 350: одинаково и кратно шагу обеих бирж
    assert entry_long[0].amount == entry_short[0].amount == D(350)


async def test_too_coarse_common_lot_skips_trade():
    executor, long_c, _short_c, stats, _, log = await create(mode=NetworkMode.TESTNET)
    executor._config.current.arbitrage.order_size_usd = D(5)
    long_c._markets = [MarketInfo(BTC, True, True, "USDT", D(1), D("0.1"), D("0.02"), D(1), D(1))]

    await executor.process_opportunities([estimate()], NONE)  # 1 BTC ≈ 100 $ при размере сделки 5 $

    assert opened(stats) == 0
    assert not long_c.placed_orders
    assert any("требует" in e.message for e in log.latest(20))
