from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.core.domain.enums import CloseReason, NetworkMode
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.trade_executor import ArbTradeExecutor

from .fakes import D, FakeConfigProvider, FakeConnector, FixedClock, make_log, ticker

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NONE = CancellationToken.none()


async def create(order_size_usd=D(100)):
    config = FakeConfigProvider()
    config.current.general.network_mode = NetworkMode.DRY_RUN
    config.current.arbitrage.enabled = True
    config.current.arbitrage.order_size_usd = order_size_usd
    config.current.arbitrage.min_spread_percent_up = D("0.4")
    config.current.arbitrage.min_spread_percent_down = D("0.15")
    config.current.arbitrage.include_fees = True
    config.current.arbitrage.slippage_buffer_percent = D(0)

    long_connector = FakeConnector("binanceusdm", taker_percent=D("0.1"))
    short_connector = FakeConnector("bybit", taker_percent=D("0.1"))
    registry = ConnectorRegistry()
    await registry.replace([long_connector, short_connector])

    clock = FixedClock(NOW)
    stats = DefaultStatisticsCollector(config, clock)
    executor = ArbTradeExecutor(config, registry, stats, make_log(config, clock), clock)
    return executor, long_connector, short_connector, stats


def estimate(symbol: str, long_price: Decimal, short_price: Decimal, volume=D(10_000_000)) -> SpreadEstimate:
    return SpreadEstimate(
        symbol,
        OpportunityLeg("binanceusdm", long_price, D("0.1")),
        OpportunityLeg("bybit", short_price, D("0.1")),
        (short_price - long_price) / long_price * 100,
        D(1),
        volume,
        NOW,
    )


async def tick(executor, long_connector, short_connector):
    await executor.manage_open_positions({"binanceusdm": long_connector.tickers, "bybit": short_connector.tickers}, NONE)


async def test_dry_run_opens_and_closes_with_take_profit_pnl():
    executor, long_c, short_c, stats = await create()

    # вход: лонг по 100 (ask дешёвой), шорт по 101 (bid дорогой)
    await executor.process_opportunities([estimate("BTC/USDT:USDT", D(100), D(101))], NONE)
    assert executor.has_open_positions

    # следующий тик: спред сжался до ~0.1% ≤ порога закрытия 0.15%
    long_c.tickers = {"BTC/USDT:USDT": ticker("binanceusdm", "BTC/USDT:USDT", bid=D("100.4"), ask=D("100.5"))}
    short_c.tickers = {"BTC/USDT:USDT": ticker("bybit", "BTC/USDT:USDT", bid=D("100.6"), ask=D("100.7"))}
    await tick(executor, long_c, short_c)

    assert not executor.has_open_positions

    report = stats.snapshot(NOW + timedelta(minutes=1))
    assert report.orders_opened == 1
    assert len(report.closed_positions) == 1
    trade = report.closed_positions[0]
    assert trade.reason == CloseReason.TAKE_PROFIT
    assert trade.size == D(1)  # 100 USD / 100 = 1 BTC
    # PnL = 1 × ((100.4 − 100) + (101 − 100.7)) − комиссии (0.1 % × 4 сделки)
    expected = (D("100.4") - 100) + (101 - D("100.7")) - (100 + 101 + D("100.4") + D("100.7")) * D("0.1") / 100
    assert trade.pnl_usd == pytest.approx(expected, abs=D("0.000001"))
    assert trade.simulated


async def test_take_profit_is_skipped_when_unwinding_would_lock_a_loss():
    executor, long_c, short_c, stats = await create()
    await executor.process_opportunities([estimate("BTC/USDT:USDT", D(100), D(101))], NONE)
    assert executor.has_open_positions

    # спред «по сторонам входа» 0.1 %, но выход по широким стаканам стоит 1.9 % + 0.4 % комиссий
    long_c.tickers = {"BTC/USDT:USDT": ticker("binanceusdm", "BTC/USDT:USDT", bid=D(100), ask=D("100.9"))}
    short_c.tickers = {"BTC/USDT:USDT": ticker("bybit", "BTC/USDT:USDT", bid=D(101), ask=D("101.9"))}
    await tick(executor, long_c, short_c)

    assert executor.has_open_positions
    assert stats.snapshot(NOW + timedelta(minutes=1)).closed_positions == []


async def test_take_profit_fires_when_unwind_is_genuinely_in_profit():
    executor, long_c, short_c, stats = await create()
    await executor.process_opportunities([estimate("ETH/USDT:USDT", D(100), D(101))], NONE)

    long_c.tickers = {"ETH/USDT:USDT": ticker("binanceusdm", "ETH/USDT:USDT", bid=D("100.4"), ask=D("100.5"))}
    short_c.tickers = {"ETH/USDT:USDT": ticker("bybit", "ETH/USDT:USDT", bid=D("100.6"), ask=D("100.7"))}
    await tick(executor, long_c, short_c)

    closed = stats.snapshot(NOW + timedelta(minutes=1)).closed_positions
    assert len(closed) == 1
    assert closed[0].reason == CloseReason.TAKE_PROFIT
    assert closed[0].pnl_usd > 0


async def test_stop_loss_closes_when_spread_explodes():
    executor, long_c, short_c, stats = await create()
    await executor.process_opportunities([estimate("ETH/USDT:USDT", D(2000), D(2009))], NONE)

    long_c.tickers = {"ETH/USDT:USDT": ticker("binanceusdm", "ETH/USDT:USDT", bid=D(1999), ask=D(2000))}
    short_c.tickers = {"ETH/USDT:USDT": ticker("bybit", "ETH/USDT:USDT", bid=D(2060), ask=D(2061))}
    await tick(executor, long_c, short_c)

    closed = stats.snapshot(NOW + timedelta(minutes=1)).closed_positions
    assert len(closed) == 1
    assert closed[0].reason == CloseReason.STOP_LOSS
    assert closed[0].pnl_usd < 0


async def test_duplicate_symbol_is_not_opened_twice():
    executor, _, _, stats = await create()
    first = estimate("BTC/USDT:USDT", D(100), D(101))

    await executor.process_opportunities([first], NONE)
    await executor.process_opportunities([replace(first, detected_at=NOW + timedelta(seconds=5))], NONE)

    assert stats.snapshot(NOW + timedelta(minutes=1)).orders_opened == 1
