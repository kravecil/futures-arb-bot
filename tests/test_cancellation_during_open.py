"""Ctrl+C посреди открытия: живые заявки снимаются, набранное откатывается, ничего не теряется."""

import asyncio
from datetime import UTC, datetime, timedelta

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.core.domain.enums import NetworkMode, OrderSide, OrderType
from futures_arb_bot.core.domain.market_data import OrderResult
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.trade_executor import ArbTradeExecutor

from .fakes import D, FakeConfigProvider, FakeConnector, ManualClock, make_log

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
BTC = "BTC/USDT:USDT"


async def create():
    config = FakeConfigProvider()
    config.current.general.network_mode = NetworkMode.TESTNET
    config.current.arbitrage.enabled = True
    config.current.arbitrage.order_size_usd = D(100)
    config.current.arbitrage.order_execution_timeout_ms = 600_000  # без остановки ждали бы 10 минут
    config.current.arbitrage.order_poll_interval_ms = 20
    long_c = FakeConnector("binanceusdm")
    short_c = FakeConnector("bybit")
    registry = ConnectorRegistry()
    await registry.replace([long_c, short_c])
    clock = ManualClock(NOW)
    stats = DefaultStatisticsCollector(config, clock)
    return ArbTradeExecutor(config, registry, stats, make_log(config, clock), clock), long_c, short_c, stats


def estimate() -> SpreadEstimate:
    return SpreadEstimate(BTC, OpportunityLeg("binanceusdm", D(100), D("0.1")), OpportunityLeg("bybit", D(101), D("0.1")), D(1), D(1), D(10**7), NOW)


async def test_stop_while_waiting_for_fills_cancels_and_rolls_back():
    executor, long_c, short_c, stats = await create()
    long_c.place_order_override = lambda r, i: (
        OrderResult.ok(f"r{i}", None, r.amount) if r.reduce_only else OrderResult.ok(f"o{i}", r.price, D("0.3"))
    )
    short_c.place_order_override = lambda r, i: OrderResult.ok(f"o{i}", r.price, D(0))

    ct = CancellationToken()
    task = asyncio.create_task(executor.process_opportunities([estimate()], ct))
    await asyncio.sleep(0.1)
    ct.cancel()
    await asyncio.wait_for(task, timeout=2)  # остановка не ждёт таймаута исполнения

    assert not executor.has_open_positions
    assert len(long_c.cancelled_orders) == 1
    assert len(short_c.cancelled_orders) == 1
    rollback = list(long_c.placed_orders.values())[-1]
    assert rollback.reduce_only and rollback.type == OrderType.MARKET and rollback.side == OrderSide.SELL
    assert rollback.amount == D("0.3")
    assert stats.snapshot(NOW + timedelta(minutes=1)).orders_failed == 1


async def test_stop_between_legs_rolls_back_first_leg_and_skips_second():
    executor, long_c, short_c, stats = await create()
    ct = CancellationToken()

    def place_long(request, index):
        if not request.reduce_only:
            ct.cancel()  # Ctrl+C пришёл, пока выставлялась первая нога
        return None

    long_c.place_order_override = place_long

    await executor.process_opportunities([estimate()], ct)

    assert short_c.placed_orders == {}  # вторая нога не выставлялась
    [opening, rollback] = list(long_c.placed_orders.values())
    assert not opening.reduce_only
    assert rollback.reduce_only and rollback.amount == opening.amount
    assert not executor.has_open_positions
    assert stats.snapshot(NOW).orders_failed == 1


async def test_fully_filled_pair_is_registered_even_if_stopped_right_after():
    executor, _, _, stats = await create()
    ct = CancellationToken()
    task = asyncio.create_task(executor.process_opportunities([estimate()], ct))
    await task
    ct.cancel()
    assert executor.has_open_positions  # закроется в _shutdown при close_positions_on_exit
    assert stats.snapshot(NOW).orders_opened == 1
