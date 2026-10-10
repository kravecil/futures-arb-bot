"""Порт ChaseOrderExecutorTests.cs — смещение цены, локальное догонание, бюджет, market-добивка."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from fakes import NOW
from testutil import create_harness, estimate

from futures_arb_bot.domain.enums import AppLogLevel, ChaseMode, OrderType, TimeInForce
from futures_arb_bot.domain.market_data import OrderResult
from futures_arb_bot.domain.options import ChaseOptions, OrderExecutionOptions

D = Decimal


def never_filled(request, index):
    """Заявка не исполняется вовсе (биржа держит её в стакане с нулевым объёмом)."""

    return OrderResult.Ok(f"o{index}", request.Price, D("0"))


def open_async(harness, long_id="binanceusdm", short_id="bybit"):
    return harness.Executor.ProcessOpportunitiesAsync([estimate(long_id=long_id, short_id=short_id)], None)


def test_default_policy_keeps_marketable_limit_behaviour():
    harness = create_harness(OrderExecutionOptions())

    asyncio.run(open_async(harness))

    orders = list(harness.Long.PlacedOrders.values())
    assert len(orders) == 1
    long_order = orders[0]
    assert long_order.Type == OrderType.Limit
    assert long_order.Price == D("100")
    assert long_order.TimeInForce == TimeInForce.Gtc
    assert long_order.ExchangeParams is None
    assert not harness.Long.CancelledOrders
    assert harness.Executor.HasOpenPositions


def test_limit_offset_moves_entry_prices_apart():
    harness = create_harness(OrderExecutionOptions(Type=OrderType.Limit, LimitOffsetBps=D("10")))

    asyncio.run(open_async(harness))

    long_order = list(harness.Long.PlacedOrders.values())[0]
    short_order = list(harness.Short.PlacedOrders.values())[0]

    # BUY агрессивнее (выше), SELL — ниже; объём ноги: 100 USDT / 100 = 1 BTC
    assert long_order.Price == D("100.1")
    assert short_order.Price == D("100.899")
    assert harness.Executor.HasOpenPositions


def test_chase_requotes_order_by_step_until_filled():
    harness = create_harness(
        OrderExecutionOptions(
            Type=OrderType.ChaseLimit,
            Chase=ChaseOptions(
                Mode=ChaseMode.Simulated, MaxSteps=5, StepIntervalMs=1, StepBps=D("10"), MaxDeviationBps=D("1000")
            ),
        )
    )

    # лонг: первые две заявки не исполняются, третья — да; шорт исполняется сразу
    harness.Long.PlaceOrderOverride = lambda request, index: never_filled(request, index) if index < 3 else None

    asyncio.run(open_async(harness))

    prices = [o.Price for o in harness.Long.PlacedOrders.values()]
    assert len(prices) == 3
    assert prices == [D("100"), D("100.1"), D("100.2")]
    assert len(harness.Long.CancelledOrders) == 2
    assert harness.Executor.HasOpenPositions


def test_chase_falls_back_to_market_when_steps_are_exhausted():
    harness = create_harness(
        OrderExecutionOptions(
            Type=OrderType.ChaseLimit,
            Chase=ChaseOptions(
                Mode=ChaseMode.Simulated,
                MaxSteps=2,
                StepIntervalMs=1,
                StepBps=D("10"),
                MaxDeviationBps=D("1000"),
                FallbackToMarket=True,
            ),
        )
    )

    # limit-заявки не исполняются, market-добивка — да
    def override(request, index):
        if request.Type == OrderType.Market:
            return OrderResult.Ok(f"m{index}", D("100.3"), request.Amount)
        return OrderResult.Ok(f"o{index}", request.Price, D("0"))

    harness.Long.PlaceOrderOverride = override

    asyncio.run(open_async(harness))

    orders = list(harness.Long.PlacedOrders.values())

    # начальная заявка + 2 шага догонания + market
    assert len(orders) == 4
    assert [o.Price for o in orders[:3]] == [D("100"), D("100.1"), D("100.2")]
    assert all(o.Type == OrderType.Limit for o in orders[:3])

    fallback = orders[-1]
    assert fallback.Type == OrderType.Market
    assert fallback.Price is None
    assert fallback.Amount == D("1")  # добивается остаток ноги, а не исходный объём
    assert not fallback.ReduceOnly
    assert len(harness.Long.CancelledOrders) == 3
    assert harness.Executor.HasOpenPositions


def test_partial_fill_is_not_ordered_twice_by_chase_and_fallback():
    harness = create_harness(
        OrderExecutionOptions(
            Type=OrderType.ChaseLimit,
            Chase=ChaseOptions(MaxSteps=1, StepIntervalMs=1, StepBps=D("10"), MaxDeviationBps=D("1000"), FallbackToMarket=True),
        )
    )

    # первая заявка набрала половину, вторая — ничего, market добирает остаток
    def override(request, index):
        if request.Type == OrderType.Market:
            return OrderResult.Ok(f"m{index}", D("100.2"), request.Amount)
        if index == 1:
            return OrderResult.Ok(f"o{index}", request.Price, D("0.5"))
        return OrderResult.Ok(f"o{index}", request.Price, D("0"))

    harness.Long.PlaceOrderOverride = override

    asyncio.run(open_async(harness))

    orders = list(harness.Long.PlacedOrders.values())
    assert len(orders) == 3
    assert orders[1].Amount == D("0.5")  # вторая заявка — только остаток
    assert orders[1].Price == D("100.1")
    assert orders[-1].Type == OrderType.Market
    assert orders[-1].Amount == D("0.5")  # market добивает ненабранное
    assert harness.Executor.HasOpenPositions


def test_without_fallback_exhausted_chase_ends_in_rollback():
    from datetime import timedelta

    harness = create_harness(
        OrderExecutionOptions(
            Type=OrderType.ChaseLimit,
            Chase=ChaseOptions(MaxSteps=1, StepIntervalMs=1, StepBps=D("10"), MaxDeviationBps=D("1000"), FallbackToMarket=False),
        ),
        timeout_ms=3_000,
    )

    harness.Long.PlaceOrderOverride = lambda request, index: OrderResult.Ok(f"o{index}", None, D("0"))

    asyncio.run(open_async(harness))

    assert len(harness.Long.PlacedOrders) == 2  # начальная заявка + один шаг
    assert not harness.Executor.HasOpenPositions  # объёма нет — позиция не регистрируется
    assert harness.Stats.Snapshot(NOW + timedelta(minutes=1)).OrdersFailed == 1


def test_chase_stops_when_next_step_would_break_deviation_budget():

    harness = create_harness(
        OrderExecutionOptions(
            Type=OrderType.ChaseLimit,
            Chase=ChaseOptions(MaxSteps=10, StepIntervalMs=1, StepBps=D("20"), MaxDeviationBps=D("25"), FallbackToMarket=False),
        ),
        timeout_ms=3_000,
    )

    harness.Long.PlaceOrderOverride = lambda request, index: OrderResult.Ok(f"o{index}", None, D("0"))

    asyncio.run(open_async(harness))

    orders = list(harness.Long.PlacedOrders.values())

    # шаг 20 bps ещё в бюджете (25), шаг 40 bps — уже нет: перестановок больше не было
    assert len(orders) == 2
    assert orders[1].Price == D("100.2")


def test_native_chase_sends_a_single_order_with_exchange_params():
    from datetime import timedelta

    harness = create_harness(
        OrderExecutionOptions(
            Type=OrderType.ChaseLimit,
            Chase=ChaseOptions(
                Mode=ChaseMode.Native,
                MaxSteps=4,
                StepIntervalMs=1,
                Params={"chaseType": "priceChase"},
            ),
        ),
        long_id="kucoinfutures",
        timeout_ms=1_500,
    )

    harness.Long.PlaceOrderOverride = never_filled

    asyncio.run(open_async(harness, long_id="kucoinfutures"))

    orders = list(harness.Long.PlacedOrders.values())
    assert len(orders) == 1
    order = orders[0]
    assert order.Type == OrderType.ChaseLimit
    assert order.Price == D("100")
    assert order.ExchangeParams["chaseType"] == "priceChase"

    # догоняет биржа: локальных перестановок нет, по таймауту заявка просто снимается
    assert not harness.Executor.HasOpenPositions
    _ = timedelta


def test_unsupported_native_chase_is_downgraded_to_local_requotes():
    # у binanceusdm нативного chase нет → исполнитель догоняет сам, нативные параметры не шлёт
    harness = create_harness(
        OrderExecutionOptions(
            Type=OrderType.ChaseLimit,
            Chase=ChaseOptions(
                Mode=ChaseMode.Native,
                MaxSteps=2,
                StepIntervalMs=1,
                StepBps=D("10"),
                MaxDeviationBps=D("1000"),
                FallbackToMarket=False,
                Params={"chaseType": "priceChase"},
            ),
        ),
        timeout_ms=3_000,
    )

    harness.Long.PlaceOrderOverride = never_filled

    asyncio.run(open_async(harness))

    assert all(o.ExchangeParams is None for o in harness.Long.PlacedOrders.values())
    assert len(harness.Long.PlacedOrders) == 3  # начальная заявка + 2 шага

    # понижение зафиксировано в журнале — один раз, а не на каждой сделке
    events = harness.Log.Latest(50)
    assert any("нативный chase" in e.Message and e.Level == AppLogLevel.Warning for e in events)


def test_close_follows_its_own_execution_policy():
    harness = create_harness(
        OrderExecutionOptions(
            Type=OrderType.Limit,
            Close=OrderExecutionOptions(Type=OrderType.Limit, LimitOffsetBps=D("10")),
        )
    )

    asyncio.run(open_async(harness))

    from fakes import make_ticker

    # закрытие по Execution:Close — limit reduceOnly SELL по bid со смещением (100.4 − 10 bps)
    harness.Long.Tickers = {"BTC/USDT:USDT": make_ticker("binanceusdm", "BTC/USDT:USDT", D("100.4"), D("100.5"))}
    harness.Short.Tickers = {"BTC/USDT:USDT": make_ticker("bybit", "BTC/USDT:USDT", D("100.6"), D("100.7"))}

    async def manage():
        await harness.Executor.ManageOpenPositionsAsync(
            {"binanceusdm": harness.Long.Tickers, "bybit": harness.Short.Tickers}, None
        )

    asyncio.run(manage())

    long_close = list(harness.Long.PlacedOrders.values())[-1]
    assert long_close.Type == OrderType.Limit
    assert long_close.ReduceOnly
    assert long_close.Price == D("100.2996")  # 100.4 − 10 bps: агрессивнее для SELL
    assert not harness.Executor.HasOpenPositions


def test_per_exchange_override_replaces_only_that_leg():
    harness = create_harness(
        OrderExecutionOptions(Type=OrderType.Limit),
        short_override=OrderExecutionOptions(LimitOffsetBps=D("30"), TimeInForce=TimeInForce.Ioc),
    )

    asyncio.run(open_async(harness))

    long_orders = list(harness.Long.PlacedOrders.values())
    short_orders = list(harness.Short.PlacedOrders.values())
    assert len(long_orders) == 1
    assert len(short_orders) == 1

    assert long_orders[0].Price == D("100")  # глобальный offset = 0
    assert long_orders[0].TimeInForce == TimeInForce.Gtc
    assert short_orders[0].Price == D("100.697")  # переопределение bybit: −30 bps от 101
    assert short_orders[0].TimeInForce == TimeInForce.Ioc
