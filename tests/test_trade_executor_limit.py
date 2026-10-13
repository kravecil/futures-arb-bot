"""
Limit-исполнение: отказ/частичное исполнение/таймаут ног при открытии, ребалансировка перекоса
и поноговое закрытие (режим Testnet на фейковых биржах).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.core.domain.enums import CloseReason, NetworkMode, OrderSide, OrderType
from futures_arb_bot.core.domain.market_data import OrderResult, PositionSnapshot
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.trade_executor import ArbTradeExecutor

from .fakes import D, FakeConfigProvider, FakeConnector, SteppingClock, make_log, ticker

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NONE = CancellationToken.none()


async def create_testnet():
    config = FakeConfigProvider()
    config.current.general.network_mode = NetworkMode.TESTNET
    config.current.arbitrage.enabled = True
    config.current.arbitrage.order_size_usd = D(100)
    config.current.arbitrage.min_spread_percent_down = D("0.15")
    config.current.arbitrage.stop_loss_spread_percent = D("2.5")
    config.current.arbitrage.order_execution_timeout_ms = 250
    config.current.arbitrage.order_poll_interval_ms = 50
    config.current.arbitrage.rebalance_tolerance_percent = D("1.0")

    long_connector = FakeConnector("binanceusdm", taker_percent=D("0.1"))
    short_connector = FakeConnector("bybit", taker_percent=D("0.1"))
    registry = ConnectorRegistry()
    await registry.replace([long_connector, short_connector])

    clock = SteppingClock(NOW, timedelta(milliseconds=60))
    stats = DefaultStatisticsCollector(config, clock)
    executor = ArbTradeExecutor(config, registry, stats, make_log(config, clock), clock)
    return executor, long_connector, short_connector, stats


def estimate(symbol: str, long_price: Decimal, short_price: Decimal) -> SpreadEstimate:
    return SpreadEstimate(
        symbol,
        OpportunityLeg("binanceusdm", long_price, D("0.1")),
        OpportunityLeg("bybit", short_price, D("0.1")),
        (short_price - long_price) / long_price * 100,
        D(1),
        D(10_000_000),
        NOW,
    )


def tickers(longs: FakeConnector, shorts: FakeConnector):
    return {"binanceusdm": longs.tickers, "bybit": shorts.tickers}


def set_neutral_tickers(longs: FakeConnector, shorts: FakeConnector) -> None:
    # спред ~1%: не тейк (порог 0.15% снизу) и не стоп (порог 2.5% сверху)
    longs.tickers = {"BTC/USDT:USDT": ticker("binanceusdm", "BTC/USDT:USDT", bid=D("99.9"), ask=D(100))}
    shorts.tickers = {"BTC/USDT:USDT": ticker("bybit", "BTC/USDT:USDT", bid=D(101), ask=D("101.1"))}


def orders(connector: FakeConnector):
    return list(connector.placed_orders.values())


async def test_first_leg_rejection_prevents_second_leg_placement():
    executor, long_c, short_c, stats = await create_testnet()
    long_c.place_order_override = lambda _r, _i: OrderResult.fail("отказ биржи")

    await executor.process_opportunities([estimate("BTC/USDT:USDT", D(100), D(101))], NONE)

    assert not executor.has_open_positions
    assert short_c.placed_orders == {}  # вторая нога не выставлялась вообще
    report = stats.snapshot(NOW + timedelta(minutes=1))
    assert report.orders_opened == 0
    assert report.orders_failed == 1


async def test_second_leg_rejection_rolls_back_full_first_leg():
    executor, long_c, short_c, stats = await create_testnet()
    short_c.place_order_override = lambda _r, _i: OrderResult.fail("нет маржи")

    await executor.process_opportunities([estimate("BTC/USDT:USDT", D(100), D(101))], NONE)

    assert not executor.has_open_positions
    long_orders = orders(long_c)
    assert len(long_orders) == 2
    assert long_orders[0].type == OrderType.LIMIT  # исходный BUY
    assert long_orders[0].side == OrderSide.BUY
    assert not long_orders[0].reduce_only

    rollback = long_orders[-1]  # откат
    assert rollback.side == OrderSide.SELL
    assert rollback.type == OrderType.MARKET
    assert rollback.reduce_only
    assert rollback.amount == D(1)

    report = stats.snapshot(NOW + timedelta(minutes=1))
    assert report.orders_opened == 0
    assert report.orders_failed == 1


async def test_partial_first_leg_is_rolled_back_only_to_filled_size():
    executor, long_c, short_c, _ = await create_testnet()
    long_c.place_order_override = lambda request, index: (
        OrderResult.ok(f"rollback-{index}", None, request.amount)  # откат проходит целиком
        if request.reduce_only
        else OrderResult.ok(f"open-{index}", request.price, request.amount / 2)  # набрана половина
    )
    short_c.place_order_override = lambda _r, _i: OrderResult.fail("нет ликвидности")

    await executor.process_opportunities([estimate("BTC/USDT:USDT", D(100), D(101))], NONE)

    assert not executor.has_open_positions
    rollback = orders(long_c)[-1]
    assert rollback.reduce_only
    assert rollback.type == OrderType.MARKET
    assert rollback.amount == D("0.5")  # откатывается только набранный объём


async def test_limit_timeout_cancels_both_legs_and_rolls_back_fills():
    executor, long_c, short_c, stats = await create_testnet()
    long_c.place_order_override = lambda request, index: (
        OrderResult.ok(f"rollback-{index}", None, request.amount)
        if request.reduce_only
        else OrderResult.ok(f"open-{index}", request.price, D("0.4"))  # принято, но не доисполняется
    )
    short_c.place_order_override = lambda request, index: OrderResult.ok(f"open-{index}", request.price, D(0))

    await executor.process_opportunities([estimate("BTC/USDT:USDT", D(100), D(101))], NONE)

    assert not executor.has_open_positions
    assert len(long_c.cancelled_orders) == 1  # обе неисполненные заявки…
    assert len(short_c.cancelled_orders) == 1  # …сняты по таймауту

    rollback = orders(long_c)[-1]
    assert rollback.reduce_only
    assert rollback.amount == D("0.4")  # набранный объём откатан

    report = stats.snapshot(NOW + timedelta(minutes=1))
    assert report.orders_opened == 0
    assert report.orders_failed == 1


async def test_failed_rollback_leaves_position_tracked_for_maintenance():
    executor, long_c, short_c, stats = await create_testnet()
    long_c.place_order_override = lambda request, index: (
        OrderResult.fail("биржа не отдаёт объём")  # откат не проходит
        if request.reduce_only
        else OrderResult.ok(f"open-{index}", request.price, request.amount)  # лонг набран полностью
    )
    short_c.place_order_override = lambda _r, _i: OrderResult.fail("нет ликвидности")
    long_c.open_positions = [PositionSnapshot("BTC/USDT:USDT", OrderSide.BUY, D(1), D(100))]  # биржа видит набранный лонг

    await executor.process_opportunities([estimate("BTC/USDT:USDT", D(100), D(101))], NONE)

    # нога не должна «теряться»: позиция остаётся в учёте для цикла ведения
    assert executor.has_open_positions
    report = stats.snapshot(NOW + timedelta(minutes=1))
    assert len(report.still_open) == 1
    assert report.still_open[0].long_size == D(1)
    assert report.still_open[0].short_size == D(0)


async def test_rebalance_cuts_larger_leg_with_reduce_only_limit():
    executor, long_c, short_c, _ = await create_testnet()
    # «сбой биржи»: шортовая нога набрала вдвое больше запрошенного
    short_c.place_order_override = lambda request, index: (
        None if request.reduce_only else OrderResult.ok(f"short-{index}", request.price, request.amount * 2)
    )

    await executor.process_opportunities([estimate("BTC/USDT:USDT", D(100), D(101))], NONE)
    assert executor.has_open_positions

    set_neutral_tickers(long_c, short_c)

    shorts_before = len(short_c.placed_orders)
    await executor.manage_open_positions(tickers(long_c, short_c), NONE)

    cut = orders(short_c)[-1]
    assert len(short_c.placed_orders) == shorts_before + 1
    assert cut.side == OrderSide.BUY  # урезаем шорт — выкупаем
    assert cut.type == OrderType.LIMIT  # ребаланс — лимиткой
    assert cut.reduce_only
    assert cut.amount == D(1)  # перекос 2 − 1 = 1 BTC
    assert cut.price == D("101.1")  # по ask биржи, где выкупаем

    # второй тик: позиция сбалансирована — лишних ордеров нет
    await executor.manage_open_positions(tickers(long_c, short_c), NONE)
    assert len(short_c.placed_orders) == shorts_before + 1


async def test_balanced_position_is_not_rebalanced():
    executor, long_c, short_c, _ = await create_testnet()

    await executor.process_opportunities([estimate("BTC/USDT:USDT", D(100), D(101))], NONE)
    set_neutral_tickers(long_c, short_c)

    await executor.manage_open_positions(tickers(long_c, short_c), NONE)

    assert executor.has_open_positions
    assert len(long_c.placed_orders) == 1  # только открывающий BUY
    assert len(short_c.placed_orders) == 1  # только открывающий SELL


async def test_close_handles_mismatched_leg_sizes_and_keeps_record():
    executor, long_c, short_c, stats = await create_testnet()
    # шорт набрал 2 BTC при запросе 1 — ноги рассогласованы
    short_c.place_order_override = lambda request, index: (
        None if request.reduce_only else OrderResult.ok(f"short-{index}", request.price, request.amount * 2)
    )

    await executor.process_opportunities([estimate("BTC/USDT:USDT", D(100), D(101))], NONE)

    # стоп-лосс: спред расширился до ~11% ≥ 2.5%
    long_c.tickers = {"BTC/USDT:USDT": ticker("binanceusdm", "BTC/USDT:USDT", bid=D("104.9"), ask=D(105))}
    short_c.tickers = {"BTC/USDT:USDT": ticker("bybit", "BTC/USDT:USDT", bid=D(111), ask=D("111.2"))}

    await executor.manage_open_positions(tickers(long_c, short_c), NONE)

    assert not executor.has_open_positions

    # каждая нога закрыта по своему фактическому объёму market reduceOnly
    long_close = orders(long_c)[-1]
    assert long_close.reduce_only
    assert long_close.side == OrderSide.SELL
    assert long_close.amount == D(1)

    short_close = orders(short_c)[-1]
    assert short_close.reduce_only
    assert short_close.side == OrderSide.BUY
    assert short_close.amount == D(2)

    closed = stats.snapshot(NOW + timedelta(minutes=1)).closed_positions
    assert len(closed) == 1
    assert closed[0].reason == CloseReason.STOP_LOSS
    assert closed[0].size == D(1)  # встречный закрытый объём — min(1, 2)
