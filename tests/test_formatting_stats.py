"""Порт Formatting/StatisticsCollectorTests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from fakes import NOW, FakeConfigProvider, FakeTime
from testutil import estimate

from futures_arb_bot.domain.arbitrage import PositionPair
from futures_arb_bot.domain.enums import CloseReason, NetworkMode
from futures_arb_bot.domain.market_data import ExchangeFees
from futures_arb_bot.engine import formatting
from futures_arb_bot.engine.statistics_collector import StatisticsCollector

D = Decimal


def test_price_formats_by_magnitude():
    assert formatting.price(D("1234.5678")) == "1,234.57"
    assert formatting.price(D("100")) == "100"
    assert formatting.price(D("101.23456")) == "101.2346"
    assert formatting.price(D("0.00123456")) == "0.001235"
    assert formatting.price(D("0.5")) == "0.5"


def test_usd_signed_with_two_decimals():
    assert formatting.usd(D("12.34")) == "+12.34 $"
    assert formatting.usd(D("-0.5")) == "-0.50 $"
    assert formatting.usd(D("0")) == "0.00 $"
    assert formatting.usd(D("1234.5")) == "+1,234.50 $"


def test_volume_humanized():
    assert formatting.volume(D("12345678")) == "12.3M"
    assert formatting.volume(D("456000")) == "456K"
    assert formatting.volume(D("2000000")) == "2M"
    assert formatting.volume(D("999")) == "999"


def test_pct_signed_three_digits_and_optional():
    assert formatting.pct(D("0.5")) == "+0.500%"
    assert formatting.pct(D("-0.5")) == "-0.500%"
    assert formatting.pct(D("0")) == "0.000%"
    assert formatting.optional_pct(None) == "н/д"


def test_duration():
    assert formatting.duration(timedelta(hours=2, minutes=3, seconds=4)) == "02:03:04"
    assert formatting.duration(timedelta(days=1, hours=2, minutes=3, seconds=4)) == "1д 02:03:04"


def test_join_skips_empty():
    assert formatting.join(" ", "a", "", None, "b") == "a b"


def test_statistics_collector_roundtrip():
    config = FakeConfigProvider()
    time = FakeTime(NOW)
    stats = StatisticsCollector(config, time)

    stats.RecordScanTick(100)
    stats.RecordScanTick(50)
    est = estimate(net=D("0.5"))
    stats.RecordOpportunity(est)
    stats.RecordOpportunity(estimate(symbol="ETH/USDT:USDT", net=D("0.9")))
    stats.RecordRequest("bybit", timedelta(milliseconds=210), success=True)
    stats.RecordRequest("bybit", timedelta(milliseconds=0), success=False)
    stats.RecordMarkets("bybit", "Bybit", 400, ExchangeFees(D("0.1"), D("0.02")), NetworkMode.DryRun)

    position = PositionPair(
        Symbol="BTC/USDT:USDT",
        LongExchangeId="binanceusdm",
        ShortExchangeId="bybit",
        LongSize=D("1"),
        ShortSize=D("1"),
        EntryLong=D("100"),
        EntryShort=D("101"),
        OpenedAt=NOW,
    )
    position.ClosedLongVolume = D("1")
    position.ClosedShortVolume = D("1")
    position.Reason = CloseReason.TakeProfit
    position.RealizedPnlUsd = D("0.5")
    position.FeesEntryUsd = D("0.2")
    stats.RecordOpened(position)
    stats.RecordOpenFailed("XRP/USDT:USDT", "no liquidity")
    stats.RecordClosed(position)

    report = stats.Snapshot(NOW + timedelta(minutes=2))
    assert report.ScanTicks == 2
    assert report.TickersProcessed == 150
    assert report.OpportunitiesDetected == 2
    assert report.BestEver.Symbol == "ETH/USDT:USDT"
    assert report.OrdersOpened == 1
    assert report.OrdersFailed == 1
    assert report.RealizedPnlUsd == D("0.5")
    assert report.ClosedPositions[0].FeesUsd == D("0.2")
    assert report.StillOpen == []
    assert [e.Id for e in report.Exchanges] == ["bybit"]
    assert report.Exchanges[0].Errors == 1
    assert report.Exchanges[0].AvgLatencyMs == 105.0
    assert report.TopSymbols[0] in (("BTC/USDT:USDT", 1), ("ETH/USDT:USDT", 1))

    dashboard = stats.BuildDashboard([est], 2, 400)
    assert dashboard.OnlineExchanges == 2
    assert dashboard.Errors == 1
    assert dashboard.OpenPositions == 0
    assert dashboard.RealizedPnlUsd == D("0.5")
    assert dashboard.PnlSimulated is True  # DryRun
