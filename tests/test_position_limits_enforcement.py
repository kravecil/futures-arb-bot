"""
Регрессия принуждения лимитов позиций: max_open_positions/max_positions_per_exchange считаются
по фактической экспозиции бирж (сверка через fetch_positions), а не только по памяти сеанса;
market-закрытие без подтверждения биржи не снимает позицию с учёта; заявка, не отменённая при
откате открытия, не теряется, а доснимается циклом ведения.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.core.domain.enums import CloseReason, NetworkMode, OrderSide, OrderStatus
from futures_arb_bot.core.domain.market_data import OrderResult, PositionSnapshot
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.trade_executor import ArbTradeExecutor

from .fakes import D, FakeConfigProvider, FakeConnector, ManualClock, SteppingClock, make_log, ticker

BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NONE = CancellationToken.none()


async def create_testnet(max_open_positions: int = 4, max_positions_per_exchange: int = 4):
    config = FakeConfigProvider()
    config.current.general.network_mode = NetworkMode.TESTNET
    config.current.arbitrage.enabled = True
    config.current.arbitrage.order_size_usd = D(100)
    config.current.arbitrage.min_spread_percent_down = D("0.15")
    config.current.arbitrage.stop_loss_spread_percent = D("2.5")
    config.current.arbitrage.order_execution_timeout_ms = 250
    config.current.arbitrage.order_poll_interval_ms = 50
    config.current.arbitrage.rebalance_tolerance_percent = D("1.0")
    config.current.arbitrage.max_open_positions = max_open_positions
    config.current.arbitrage.max_positions_per_exchange = max_positions_per_exchange

    long_connector = FakeConnector("binanceusdm", taker_percent=D("0.1"))
    short_connector = FakeConnector("bybit", taker_percent=D("0.1"))
    registry = ConnectorRegistry()
    await registry.replace([long_connector, short_connector])

    clock = SteppingClock(NOW, timedelta(milliseconds=60))
    stats = DefaultStatisticsCollector(config, clock)
    executor = ArbTradeExecutor(config, registry, stats, make_log(config, clock), clock)
    executor._position_settle_delay = 0
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


# ------------------------- лимиты внутри процесса -------------------------


async def test_max_open_positions_1_blocks_the_second_arbitrage():
    executor, long_c, short_c, _ = await create_testnet(max_open_positions=1)

    await executor.process_opportunities([estimate(BTC, D(100), D(101)), estimate(ETH, D(50), D(51))], NONE)

    assert executor.has_open_positions
    assert len(long_c.placed_orders) == 1  # открыт только первый кандидат
    assert len(short_c.placed_orders) == 1


async def test_max_positions_per_exchange_1_blocks_the_second_arbitrage_on_the_same_exchange():
    executor, long_c, short_c, _ = await create_testnet(max_open_positions=4, max_positions_per_exchange=1)

    await executor.process_opportunities([estimate(BTC, D(100), D(101)), estimate(ETH, D(50), D(51))], NONE)

    # обе биржи участвуют в каждом арбитраже: после BTC слоты исчерпаны
    assert len(long_c.placed_orders) == 1
    assert len(short_c.placed_orders) == 1


# ------------------------- сверка с биржей -------------------------


async def test_positions_left_on_the_exchanges_after_restart_consume_max_open_positions():
    executor, long_c, short_c, _ = await create_testnet(max_open_positions=1)

    # «прошлый сеанс» оставил ETH-арбитраж на обеих биржах — этот процесс о нём не знает
    long_c.open_positions.append(PositionSnapshot(ETH, OrderSide.BUY, D(2), D(50)))
    short_c.open_positions.append(PositionSnapshot(ETH, OrderSide.SELL, D(2), D(51)))

    await executor.process_opportunities([estimate(BTC, D(100), D(101))], NONE)

    # лимит 1 уже занят чужой парой позиций — нового арбитража быть не должно
    assert not executor.has_open_positions
    assert long_c.placed_orders == {}
    assert short_c.placed_orders == {}


async def test_foreign_leg_on_one_exchange_consumes_its_max_positions_per_exchange_slot():
    executor, long_c, short_c, _ = await create_testnet(max_open_positions=4, max_positions_per_exchange=1)

    # на bybit живёт «чужой» шорт: арбитража (2+ биржи) нет, но слот биржи занят
    short_c.open_positions.append(PositionSnapshot(ETH, OrderSide.SELL, D(2), D(51)))

    await executor.process_opportunities([estimate(BTC, D(100), D(101))], NONE)

    assert long_c.placed_orders == {}
    assert short_c.placed_orders == {}


async def test_session_positions_are_not_double_counted_against_exchange_snapshot():
    executor, long_c, short_c, _ = await create_testnet(max_open_positions=4, max_positions_per_exchange=2)

    # биржи на запросе сверки отдают нашу же BTC-позицию — вторым слотом она не считается
    long_c.open_positions.append(PositionSnapshot(BTC, OrderSide.BUY, D(1), D(100)))
    short_c.open_positions.append(PositionSnapshot(BTC, OrderSide.SELL, D(1), D(101)))

    await executor.process_opportunities([estimate(BTC, D(100), D(101))], NONE)
    assert executor.has_open_positions

    await executor.process_opportunities([estimate(ETH, D(50), D(51))], NONE)

    # ETH открылся: 1 сеансовая позиция + 0 внешних (BTC — наша же, дедуплицирована)
    assert len(long_c.placed_orders) == 2
    assert len(short_c.placed_orders) == 2


async def test_reconcile_failure_does_not_block_trading():
    executor, long_c, short_c, _ = await create_testnet(max_open_positions=1)
    long_c.fail_fetch_positions = True
    short_c.fail_fetch_positions = True

    await executor.process_opportunities([estimate(BTC, D(100), D(101))], NONE)

    # сверка упала — работаем по позициям сеанса, торговля не встала
    assert executor.has_open_positions
    assert len(long_c.placed_orders) == 1


# ------------------------- честное market-закрытие -------------------------


async def test_market_close_without_confirmation_keeps_position_in_tracking():
    executor, long_c, short_c, stats = await create_testnet(max_open_positions=1)

    await executor.process_opportunities([estimate(BTC, D(100), D(101))], NONE)
    assert executor.has_open_positions

    # стоп-лосс: спред расширился выше порога
    long_c.tickers = {BTC: ticker("binanceusdm", BTC, bid=D("104.9"), ask=D(105))}
    short_c.tickers = {BTC: ticker("bybit", BTC, bid=D(111), ask=D("111.2"))}

    # закрытие лонга «принято, но без объёма», и на бирже лонг по-прежнему живёт
    long_c.place_order_override = lambda request, index: OrderResult.ok(f"unfilled-{index}", None, D(0)) if request.reduce_only else None
    long_c.open_positions.append(PositionSnapshot(BTC, OrderSide.BUY, D(1), D(100)))

    await executor.manage_open_positions(tickers(long_c, short_c), NONE)

    # такую ногу нельзя считать закрытой: позиция остаётся в учёте, слот лимита занят
    assert executor.has_open_positions
    assert stats.snapshot(NOW + timedelta(minutes=1)).closed_positions == []

    # биржа подтвердила отсутствие позиции — закрытие довершается при следующей попытке (после паузы повтора)
    long_c.open_positions.clear()
    executor._clock = ManualClock(executor._clock.now() + timedelta(seconds=2))
    await executor.manage_open_positions(tickers(long_c, short_c), NONE)

    assert not executor.has_open_positions
    closed = stats.snapshot(NOW + timedelta(minutes=1)).closed_positions
    assert len(closed) == 1
    assert closed[0].reason == CloseReason.STOP_LOSS


# ------------------------- неотменённые заявки отката -------------------------


async def test_uncancelled_leg_is_not_forgotten_and_keeps_being_cancelled():
    executor, long_c, short_c, _ = await create_testnet()

    # лонг выставлен и живёт в стакане, шорт отклонён → откат открытия, но отмена лонга не проходит
    long_c.place_order_override = lambda request, index: None if request.reduce_only else OrderResult.ok(f"stuck-{index}", request.price, D(0))
    short_c.place_order_override = lambda _r, _i: OrderResult.fail("нет ликвидности")
    long_c.cancel_order_override = lambda _order_id: False

    await executor.process_opportunities([estimate(BTC, D(100), D(101))], NONE)

    # позиция не открыта — но и заявка не «забыта»: она всё ещё в стакане
    assert not executor.has_open_positions
    assert long_c.order_states["stuck-1"].status == OrderStatus.OPEN
    assert long_c.cancelled_orders == []

    current = tickers(long_c, short_c)
    await executor.manage_open_positions(current, NONE)
    await executor.manage_open_positions(current, NONE)

    # цикл ведения не бросает заявку: пока отмена не проходит, она помнится и опрашивается
    assert long_c.order_states["stuck-1"].status == OrderStatus.OPEN
    assert long_c.cancelled_orders == []

    # отмена прошла — заявка снята, цикл ведения её отпускает
    long_c.cancel_order_override = None
    await executor.manage_open_positions(current, NONE)

    assert long_c.order_states["stuck-1"].status == OrderStatus.CANCELED
    assert len(long_c.cancelled_orders) == 1
