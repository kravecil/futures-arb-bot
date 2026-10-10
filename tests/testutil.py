"""Общие помощники тестов исполнителя — порт harness'ов из C#-тестов."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import NamedTuple

from fakes import NOW, FakeConfigProvider, FakeConnector, FakeTime, SteppingTime

from futures_arb_bot.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.domain.enums import NetworkMode
from futures_arb_bot.domain.options import ExchangeConfigEntry, OrderExecutionOptions
from futures_arb_bot.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.engine.event_log import EventLog
from futures_arb_bot.engine.executor import ArbTradeExecutor
from futures_arb_bot.engine.statistics_collector import StatisticsCollector

D = Decimal


class Harness(NamedTuple):
    Executor: ArbTradeExecutor
    Long: FakeConnector
    Short: FakeConnector
    Stats: StatisticsCollector
    Log: EventLog
    Config: FakeConfigProvider
    Time: object


def create_harness(
    execution: OrderExecutionOptions | None = None,
    long_id: str = "binanceusdm",
    short_id: str = "bybit",
    timeout_ms: int = 20_000,
    short_override: OrderExecutionOptions | None = None,
    mode: NetworkMode = NetworkMode.Testnet,
    order_size: Decimal = D("100"),
    poll_ms: int = 20,
    stepping_time: bool = True,
) -> Harness:
    config = FakeConfigProvider()
    config.Current.General.NetworkMode = mode
    config.Current.Arbitrage.Enabled = True
    config.Current.Arbitrage.OrderSizeUsd = order_size
    config.Current.Arbitrage.MinSpreadPercentDown = D("0.15")
    config.Current.Arbitrage.StopLossSpreadPercent = D("2.5")
    config.Current.Arbitrage.OrderExecutionTimeoutMs = timeout_ms
    config.Current.Arbitrage.OrderPollIntervalMs = poll_ms
    config.Current.Arbitrage.Execution = execution or OrderExecutionOptions()

    if short_override is not None:
        # так переопределение задаётся в exchanges.yaml
        config.Current.Exchanges.Items.append(ExchangeConfigEntry(Id=short_id, Execution=short_override))

    longs = FakeConnector(long_id)
    shorts = FakeConnector(short_id)

    registry = ConnectorRegistry()
    registry.Replace([longs, shorts])

    time = SteppingTime(NOW, timedelta(milliseconds=60)) if stepping_time else FakeTime(NOW)
    stats = StatisticsCollector(config, time)
    log = EventLog(config, time)
    executor = ArbTradeExecutor(config, registry, stats, log, time)

    return Harness(executor, longs, shorts, stats, log, config, time)


def estimate(
    long_price: Decimal = D("100"),
    short_price: Decimal = D("101"),
    symbol: str = "BTC/USDT:USDT",
    long_id: str = "binanceusdm",
    short_id: str = "bybit",
    gross: Decimal = D("1"),
    net: Decimal = D("1"),
    volume: Decimal = D("10000000"),
) -> SpreadEstimate:
    return SpreadEstimate(
        symbol,
        OpportunityLeg(long_id, long_price, D("0.1")),
        OpportunityLeg(short_id, short_price, D("0.1")),
        gross,
        net,
        volume,
        NOW,
    )


def never_filled(request, index):
    """Заявка не исполняется вовсе (биржа держит её в стакане с нулевым объёмом)."""

    return OrderResult_ok(index, request)


def OrderResult_ok(index, request):
    from futures_arb_bot.domain.market_data import OrderResult

    return OrderResult.Ok(f"o{index}", request.Price, D("0"))


def tickers_map(long_harness: Harness) -> dict:
    return {
        long_harness.Long.Id: long_harness.Long.Tickers,
        long_harness.Short.Id: long_harness.Short.Tickers,
    }
