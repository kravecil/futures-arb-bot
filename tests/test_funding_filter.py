"""Фильтр по фандингу в сканере (порт FundingFilterTests)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from fakes import NOW, FakeConfigProvider, FakeConnector, FakeExecutor, FakeNotifier, FakeTime, make_ticker

from futures_arb_bot.domain.enums import NetworkMode
from futures_arb_bot.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.engine.event_log import EventLog
from futures_arb_bot.engine.scanner import ArbitrageScanner
from futures_arb_bot.engine.spread_calculator import SpreadCalculator
from futures_arb_bot.engine.statistics_collector import StatisticsCollector
from futures_arb_bot.engine.symbol_filter import SymbolFilter

D = Decimal


def build_scanner(funding_long=None, funding_short=None):
    config = FakeConfigProvider()
    config.Current.General.NetworkMode = NetworkMode.DryRun
    config.Current.Arbitrage.Enabled = True
    config.Current.Arbitrage.MinSpreadPercentUp = D("0.4")

    long_connector = FakeConnector("binanceusdm")
    short_connector = FakeConnector("bybit")
    long_connector.Tickers = {"BTC/USDT:USDT": make_ticker("binanceusdm", "BTC/USDT:USDT", D("99.9"), D("100"), ts=NOW)}
    short_connector.Tickers = {"BTC/USDT:USDT": make_ticker("bybit", "BTC/USDT:USDT", D("101"), D("101.1"), ts=NOW)}

    if funding_long is not None:
        long_connector.FundingRatesPercent = {"BTC/USDT:USDT": funding_long}
    if funding_short is not None:
        short_connector.FundingRatesPercent = {"BTC/USDT:USDT": funding_short}

    registry = ConnectorRegistry()
    registry.Replace([long_connector, short_connector])

    time = FakeTime(NOW)
    log = EventLog(config, time)
    stats = StatisticsCollector(config, time)
    executor = FakeExecutor()
    notifier = FakeNotifier()
    calculator = SpreadCalculator(config, time)
    filter_ = SymbolFilter(config)
    scanner = ArbitrageScanner(config, registry, calculator, filter_, executor, stats, notifier, log, time)

    return scanner, executor, log, notifier, long_connector, short_connector


def scan(scanner):
    async def run():
        await scanner.ScanOnceAsync(None)

    asyncio.run(run())


def test_funding_rates_attach_to_legs():
    scanner, _, _, _, _, _ = build_scanner(funding_long=D("0.01"), funding_short=D("0.02"))
    scan(scanner)

    top = scanner.Snapshot.Top[0]
    assert top.LongLeg.FundingPercent == D("0.01")
    assert top.ShortLeg.FundingPercent == D("0.02")


def test_net_funding_below_limit_skips_deal():
    # чистый фандинг позиции = рейт шорта − рейт лонга = 0 − 1.5 = −1.5 % < предела −1 %
    scanner, executor, log, _, _, _ = build_scanner(funding_long=D("1.5"), funding_short=D("0.0"))
    scan(scanner)

    assert not executor.Processed  # сделка не открыта
    assert any("Пропуск BTC/USDT:USDT" in e.Message for e in log.Latest(50))


def test_missing_funding_data_fails_open():
    scanner, executor, _, _, long_connector, _ = build_scanner(funding_short=D("0.01"))
    long_connector.FailFetchFundingRates = True
    scan(scanner)

    # по одной ноге данных нет → сравнение не выполняется → сделка разрешена (fail-open)
    assert executor.Processed


def test_zero_funding_not_confused_with_missing_data():
    # long 0 % / short 0 % → чистый 0 ≥ предела −1 → открыть; «нет данных» дал бы тот же исход,
    # но рейты при этом обязаны прийти в ноги (отличаем null от нуля)
    scanner, executor, _, _, _, _ = build_scanner(funding_long=D("0"), funding_short=D("0"))
    scan(scanner)

    assert executor.Processed
    top = scanner.Snapshot.Top[0]
    assert top.LongLeg.FundingPercent == D("0")


def test_signals_notified_only_when_channel_usable():
    scanner, _, _, notifier, _, _ = build_scanner()
    scan(scanner)
    assert not notifier.Notified  # раздел Notifications выключен

    scanner2, _, _, notifier2, _, _ = build_scanner()
    notifications = scanner2._config.Current.Notifications
    notifications.Enabled = True
    notifications.BotToken = "token"
    notifications.AdminChatId = "chat"
    scan(scanner2)
    assert notifier2.Notified
