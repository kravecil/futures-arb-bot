"""Порт ArbTradeExecutorTests.cs — DryRun: открытие, тейк-профит со страховкой PnL, стоп-лосс."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

from fakes import NOW, FakeConfigProvider, FakeConnector, FakeTime, make_ticker
from testutil import estimate

from futures_arb_bot.domain.enums import CloseReason, NetworkMode
from futures_arb_bot.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.engine.event_log import EventLog
from futures_arb_bot.engine.executor import ArbTradeExecutor
from futures_arb_bot.engine.statistics_collector import StatisticsCollector

D = Decimal


def create(order_size_usd: Decimal = D("100")):
    config = FakeConfigProvider()
    config.Current.General.NetworkMode = NetworkMode.DryRun
    config.Current.Arbitrage.Enabled = True
    config.Current.Arbitrage.OrderSizeUsd = order_size_usd
    config.Current.Arbitrage.MinSpreadPercentUp = D("0.4")
    config.Current.Arbitrage.MinSpreadPercentDown = D("0.15")
    config.Current.Arbitrage.IncludeFees = True
    config.Current.Arbitrage.SlippageBufferPercent = D("0")

    long_connector = FakeConnector("binanceusdm", taker_percent=D("0.1"))
    short_connector = FakeConnector("bybit", taker_percent=D("0.1"))

    registry = ConnectorRegistry()
    registry.Replace([long_connector, short_connector])

    time = FakeTime(NOW)
    stats = StatisticsCollector(config, time)
    log = EventLog(config, time)
    executor = ArbTradeExecutor(config, registry, stats, log, time)

    return executor, long_connector, short_connector, stats


def test_dry_run_opens_and_closes_with_take_profit_pnl():
    executor, long_connector, short_connector, stats = create()

    async def run():
        # вход: лонг по 100 (ask дешёвой), шорт по 101 (bid дорогой)
        await executor.ProcessOpportunitiesAsync([estimate(D("100"), D("101"))], None)
        assert executor.HasOpenPositions

        # следующий тик: спред сжался до ~0.1% ≤ порога закрытия 0.15%
        long_connector.Tickers = {"BTC/USDT:USDT": make_ticker("binanceusdm", "BTC/USDT:USDT", D("100.4"), D("100.5"))}
        short_connector.Tickers = {"BTC/USDT:USDT": make_ticker("bybit", "BTC/USDT:USDT", D("100.6"), D("100.7"))}

        await executor.ManageOpenPositionsAsync({"binanceusdm": long_connector.Tickers, "bybit": short_connector.Tickers}, None)
        assert not executor.HasOpenPositions

    asyncio.run(run())

    report = stats.Snapshot(NOW + timedelta(minutes=1))
    assert report.OrdersOpened == 1
    assert len(report.ClosedPositions) == 1
    trade = report.ClosedPositions[0]
    assert trade.Reason == CloseReason.TakeProfit

    # размер = 100 USD / 100 = 1 BTC
    assert trade.Size == D("1")
    # PnL = 1 × ((100.4 − 100) + (101 − 100.7)) − комиссии (0.1 % × 4 сделки)
    expected_pnl = (D("0.4") + D("0.3")) - (D("100") + D("101") + D("100.4") + D("100.7")) * D("0.1") / D("100")
    assert abs(trade.PnlUsd - expected_pnl) < D("0.000001")
    assert trade.Simulated


def test_wide_book_fake_take_profit_is_refused():
    """Спред «сжался», но немедленный выход убыточен — тейк-профит запрещён."""

    executor, long_connector, short_connector, stats = create()

    async def run():
        await executor.ProcessOpportunitiesAsync([estimate(D("100"), D("101"))], None)

        long_connector.Tickers = {"BTC/USDT:USDT": make_ticker("binanceusdm", "BTC/USDT:USDT", D("100"), D("100.9"))}
        short_connector.Tickers = {"BTC/USDT:USDT": make_ticker("bybit", "BTC/USDT:USDT", D("101"), D("101.9"))}

        await executor.ManageOpenPositionsAsync({"binanceusdm": long_connector.Tickers, "bybit": short_connector.Tickers}, None)

        # прежняя логика назвала бы это «фиксацией прибыли» и закрыла сделку в минус
        assert executor.HasOpenPositions
        assert not stats.Snapshot(NOW + timedelta(minutes=1)).ClosedPositions

    asyncio.run(run())


def test_take_profit_fires_when_unwind_is_genuinely_in_profit():
    executor, long_connector, short_connector, stats = create()

    async def run():
        est = estimate(D("100"), D("101"), symbol="ETH/USDT:USDT")
        await executor.ProcessOpportunitiesAsync([est], None)

        # узкие стаканы и почти сошедшиеся цены: выход по 100.4/100.5 и 100.6/100.7 стоит 0.3 %,
        # остаётся 1 % − 0.3 % − 0.4 % комиссий = +0.3 % — тейк-профит легитимен
        long_connector.Tickers = {"ETH/USDT:USDT": make_ticker("binanceusdm", "ETH/USDT:USDT", D("100.4"), D("100.5"))}
        short_connector.Tickers = {"ETH/USDT:USDT": make_ticker("bybit", "ETH/USDT:USDT", D("100.6"), D("100.7"))}

        await executor.ManageOpenPositionsAsync({"binanceusdm": long_connector.Tickers, "bybit": short_connector.Tickers}, None)

    asyncio.run(run())

    closed = stats.Snapshot(NOW + timedelta(minutes=1)).ClosedPositions
    assert len(closed) == 1
    assert closed[0].Reason == CloseReason.TakeProfit
    assert closed[0].PnlUsd > 0


def test_stop_loss_closes_when_spread_explodes():
    executor, long_connector, short_connector, stats = create()

    async def run():
        est = estimate(D("2000"), D("2009"), symbol="ETH/USDT:USDT")
        await executor.ProcessOpportunitiesAsync([est], None)

        long_connector.Tickers = {"ETH/USDT:USDT": make_ticker("binanceusdm", "ETH/USDT:USDT", D("1999"), D("2000"))}
        short_connector.Tickers = {"ETH/USDT:USDT": make_ticker("bybit", "ETH/USDT:USDT", D("2060"), D("2061"))}

        await executor.ManageOpenPositionsAsync({"binanceusdm": long_connector.Tickers, "bybit": short_connector.Tickers}, None)

    asyncio.run(run())

    closed = stats.Snapshot(NOW + timedelta(minutes=1)).ClosedPositions
    assert len(closed) == 1
    assert closed[0].Reason == CloseReason.StopLoss
    assert closed[0].PnlUsd < 0


def test_duplicate_symbol_is_not_opened_twice():
    executor, _, _, stats = create()

    async def run():
        est = estimate(D("100"), D("101"))
        await executor.ProcessOpportunitiesAsync([est], None)
        await executor.ProcessOpportunitiesAsync([est.with_(DetectedAt=NOW + timedelta(seconds=5))], None)

    asyncio.run(run())

    assert stats.Snapshot(NOW + timedelta(minutes=1)).OrdersOpened == 1
