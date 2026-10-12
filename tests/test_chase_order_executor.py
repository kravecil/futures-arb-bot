"""
Исполнение заявок по политике execution: смещение цены, локальное догонание (перевыставление
заявок), бюджет шагов и отклонения, market-добивка, нативный chase.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.core.domain.enums import AppLogLevel, ChaseMode, NetworkMode, OrderType, TimeInForce
from futures_arb_bot.core.domain.market_data import OrderRequest, OrderResult
from futures_arb_bot.core.domain.options import ChaseOptions, ExchangeConfigEntry, OrderExecutionOptions
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.event_log import InMemoryEventLog
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.trade_executor import ArbTradeExecutor

from .fakes import D, FakeConfigProvider, FakeConnector, SteppingClock, make_log, ticker

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NONE = CancellationToken.none()


@dataclass
class Harness:
    executor: ArbTradeExecutor
    long: FakeConnector
    short: FakeConnector
    stats: DefaultStatisticsCollector
    log: InMemoryEventLog


async def create(
    execution: OrderExecutionOptions,
    long_id: str = "binanceusdm",
    short_id: str = "bybit",
    timeout_ms: int = 20_000,
    short_override: OrderExecutionOptions | None = None,
) -> Harness:
    config = FakeConfigProvider()
    config.current.general.network_mode = NetworkMode.TESTNET
    config.current.arbitrage.enabled = True
    config.current.arbitrage.order_size_usd = D(100)
    config.current.arbitrage.min_spread_percent_down = D("0.15")
    config.current.arbitrage.stop_loss_spread_percent = D("2.5")
    config.current.arbitrage.order_execution_timeout_ms = timeout_ms
    config.current.arbitrage.order_poll_interval_ms = 20
    config.current.arbitrage.execution = execution

    if short_override is not None:
        # так переопределение задаётся в exchanges.yaml
        config.current.exchanges.items.append(ExchangeConfigEntry(id=short_id, execution=short_override))

    longs = FakeConnector(long_id)
    shorts = FakeConnector(short_id)
    registry = ConnectorRegistry()
    await registry.replace([longs, shorts])

    clock = SteppingClock(NOW, timedelta(milliseconds=60))
    stats = DefaultStatisticsCollector(config, clock)
    log = make_log(config, clock)
    executor = ArbTradeExecutor(config, registry, stats, log, clock)
    return Harness(executor, longs, shorts, stats, log)


def estimate(long_id: str, short_id: str) -> SpreadEstimate:
    return SpreadEstimate(
        "BTC/USDT:USDT", OpportunityLeg(long_id, D(100), D("0.1")), OpportunityLeg(short_id, D(101), D("0.1")), D(1), D(1), D(10_000_000), NOW
    )


async def open_(harness: Harness, long_id: str = "binanceusdm", short_id: str = "bybit") -> None:
    await harness.executor.process_opportunities([estimate(long_id, short_id)], NONE)


def never_filled(request: OrderRequest, index: int) -> OrderResult:
    """Заявка не исполняется вовсе (биржа держит её в стакане с нулевым объёмом)."""
    return OrderResult.ok(f"o{index}", request.price, D(0))


def orders(connector: FakeConnector):
    return list(connector.placed_orders.values())


async def test_default_policy_keeps_marketable_limit_behaviour():
    harness = await create(OrderExecutionOptions())
    await open_(harness)

    [long_order] = orders(harness.long)
    assert long_order.type == OrderType.LIMIT
    assert long_order.price == D(100)
    assert long_order.time_in_force == TimeInForce.GTC
    assert long_order.exchange_params is None
    assert harness.long.cancelled_orders == []
    assert harness.executor.has_open_positions


async def test_limit_offset_moves_entry_prices_apart():
    harness = await create(OrderExecutionOptions(type=OrderType.LIMIT, limit_offset_bps=D(10)))
    await open_(harness)

    [long_order] = orders(harness.long)
    [short_order] = orders(harness.short)
    # BUY агрессивнее (выше), SELL — ниже; объём ноги: 100 USDT / 100 = 1 BTC
    assert long_order.price == D("100.1")
    assert short_order.price == D("100.899")
    assert harness.executor.has_open_positions


async def test_chase_requotes_order_by_step_until_filled():
    harness = await create(
        OrderExecutionOptions(
            type=OrderType.CHASE_LIMIT,
            chase=ChaseOptions(mode=ChaseMode.SIMULATED, max_steps=5, step_interval_ms=1, step_bps=D(10), max_deviation_bps=D(1000)),
        )
    )
    # лонг: первые две заявки не исполняются, третья — да; шорт исполняется сразу
    harness.long.place_order_override = lambda request, index: never_filled(request, index) if index < 3 else None

    await open_(harness)

    prices = [o.price for o in orders(harness.long)]
    assert prices == [D(100), D("100.1"), D("100.2")]
    assert len(harness.long.cancelled_orders) == 2
    assert harness.executor.has_open_positions


async def test_chase_falls_back_to_market_when_steps_are_exhausted():
    harness = await create(
        OrderExecutionOptions(
            type=OrderType.CHASE_LIMIT,
            chase=ChaseOptions(
                mode=ChaseMode.SIMULATED, max_steps=2, step_interval_ms=1, step_bps=D(10), max_deviation_bps=D(1000), fallback_to_market=True
            ),
        )
    )
    # limit-заявки не исполняются, market-добивка — да
    harness.long.place_order_override = lambda request, index: (
        OrderResult.ok(f"m{index}", D("100.3"), request.amount)
        if request.type == OrderType.MARKET
        else OrderResult.ok(f"o{index}", request.price, D(0))
    )

    await open_(harness)

    placed = orders(harness.long)
    # начальная заявка + 2 шага догонания + market
    assert len(placed) == 4
    assert [o.price for o in placed[:3]] == [D(100), D("100.1"), D("100.2")]
    assert all(o.type == OrderType.LIMIT for o in placed[:3])

    fallback = placed[-1]
    assert fallback.type == OrderType.MARKET
    assert fallback.price is None
    assert fallback.amount == D(1)  # добивается остаток ноги, а не исходный объём
    assert not fallback.reduce_only
    assert len(harness.long.cancelled_orders) == 3
    assert harness.executor.has_open_positions


async def test_partial_fill_is_not_ordered_twice_by_chase_and_fallback():
    harness = await create(
        OrderExecutionOptions(
            type=OrderType.CHASE_LIMIT,
            chase=ChaseOptions(max_steps=1, step_interval_ms=1, step_bps=D(10), max_deviation_bps=D(1000), fallback_to_market=True),
        )
    )

    def place(request: OrderRequest, index: int) -> OrderResult:
        # первая заявка набрала половину, вторая — ничего, market добирает остаток
        if request.type == OrderType.MARKET:
            return OrderResult.ok(f"m{index}", D("100.2"), request.amount)
        return OrderResult.ok(f"o{index}", request.price, D("0.5") if index == 1 else D(0))

    harness.long.place_order_override = place

    await open_(harness)

    placed = orders(harness.long)
    assert len(placed) == 3
    assert placed[1].amount == D("0.5")  # вторая заявка — только остаток
    assert placed[1].price == D("100.1")
    assert placed[-1].type == OrderType.MARKET
    assert placed[-1].amount == D("0.5")  # market добивает ненабранное
    assert harness.executor.has_open_positions


async def test_without_fallback_exhausted_chase_ends_in_rollback():
    harness = await create(
        OrderExecutionOptions(
            type=OrderType.CHASE_LIMIT,
            chase=ChaseOptions(max_steps=1, step_interval_ms=1, step_bps=D(10), max_deviation_bps=D(1000), fallback_to_market=False),
        ),
        timeout_ms=3_000,
    )
    harness.long.place_order_override = lambda _request, index: OrderResult.ok(f"o{index}", None, D(0))

    await open_(harness)

    assert len(harness.long.placed_orders) == 2  # начальная заявка + один шаг
    assert not harness.executor.has_open_positions  # объёма нет — позиция не регистрируется
    assert harness.stats.snapshot(NOW + timedelta(minutes=1)).orders_failed == 1


async def test_chase_stops_when_next_step_would_break_deviation_budget():
    harness = await create(
        OrderExecutionOptions(
            type=OrderType.CHASE_LIMIT,
            chase=ChaseOptions(max_steps=10, step_interval_ms=1, step_bps=D(20), max_deviation_bps=D(25), fallback_to_market=False),
        ),
        timeout_ms=3_000,
    )
    harness.long.place_order_override = lambda _request, index: OrderResult.ok(f"o{index}", None, D(0))

    await open_(harness)

    placed = orders(harness.long)
    # шаг 20 bps ещё в бюджете (25), шаг 40 bps — уже нет: перестановок больше не было
    assert len(placed) == 2
    assert placed[1].price == D("100.2")


async def test_native_chase_sends_a_single_order_with_exchange_params():
    harness = await create(
        OrderExecutionOptions(
            type=OrderType.CHASE_LIMIT,
            chase=ChaseOptions(mode=ChaseMode.NATIVE, max_steps=4, step_interval_ms=1, params={"chaseType": "priceChase"}),
        ),
        long_id="kucoinfutures",
        timeout_ms=1_500,
    )
    harness.long.place_order_override = lambda request, index: OrderResult.ok(f"o{index}", request.price, D(0))

    await open_(harness, long_id="kucoinfutures")

    [order] = orders(harness.long)
    assert order.type == OrderType.CHASE_LIMIT
    assert order.price == D(100)
    assert order.exchange_params is not None
    assert order.exchange_params["chaseType"] == "priceChase"

    # догоняет биржа: локальных перестановок нет, по таймауту заявка просто снимается
    assert not harness.executor.has_open_positions


async def test_unsupported_native_chase_is_downgraded_to_local_requotes():
    # у binanceusdm нативного chase нет → исполнитель догоняет сам, нативные параметры не шлёт
    harness = await create(
        OrderExecutionOptions(
            type=OrderType.CHASE_LIMIT,
            chase=ChaseOptions(
                mode=ChaseMode.NATIVE,
                max_steps=2,
                step_interval_ms=1,
                step_bps=D(10),
                max_deviation_bps=D(1000),
                fallback_to_market=False,
                params={"chaseType": "priceChase"},
            ),
        ),
        timeout_ms=3_000,
    )
    harness.long.place_order_override = lambda request, index: OrderResult.ok(f"o{index}", request.price, D(0))

    await open_(harness)

    assert all(o.exchange_params is None for o in orders(harness.long))
    assert len(harness.long.placed_orders) == 3  # начальная заявка + 2 шага

    # понижение зафиксировано в журнале — один раз, а не на каждой сделке
    assert any("нативный chase" in e.message and e.level == AppLogLevel.WARNING for e in harness.log.latest(50))


async def test_close_follows_its_own_execution_policy():
    harness = await create(OrderExecutionOptions(type=OrderType.LIMIT, close=OrderExecutionOptions(type=OrderType.LIMIT, limit_offset_bps=D(10))))
    await open_(harness)

    # закрытие по execution.close — limit reduceOnly SELL по bid со смещением (100.4 − 10 bps)
    harness.long.tickers = {"BTC/USDT:USDT": ticker("binanceusdm", "BTC/USDT:USDT", bid=D("100.4"), ask=D("100.5"))}
    harness.short.tickers = {"BTC/USDT:USDT": ticker("bybit", "BTC/USDT:USDT", bid=D("100.6"), ask=D("100.7"))}

    await harness.executor.manage_open_positions({"binanceusdm": harness.long.tickers, "bybit": harness.short.tickers}, NONE)

    long_close = orders(harness.long)[-1]
    assert long_close.type == OrderType.LIMIT
    assert long_close.reduce_only
    assert long_close.price == D("100.2996")  # 100.4 − 10 bps: агрессивнее для SELL
    assert not harness.executor.has_open_positions


async def test_per_exchange_override_replaces_only_that_leg():
    harness = await create(
        OrderExecutionOptions(type=OrderType.LIMIT),
        short_override=OrderExecutionOptions(limit_offset_bps=D(30), time_in_force=TimeInForce.IOC),
    )
    await open_(harness)

    [long_order] = orders(harness.long)
    [short_order] = orders(harness.short)

    assert long_order.price == D(100)  # глобальный offset = 0
    assert long_order.time_in_force == TimeInForce.GTC
    assert short_order.price == D("100.697")  # переопределение bybit: −30 bps от 101
    assert short_order.time_in_force == TimeInForce.IOC
