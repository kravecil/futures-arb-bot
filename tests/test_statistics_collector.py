from datetime import UTC, datetime, timedelta

from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, PositionPair, SpreadEstimate
from futures_arb_bot.core.domain.enums import NetworkMode
from futures_arb_bot.core.domain.market_data import ExchangeFees
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector

from .fakes import D, FakeConfigProvider, FixedClock

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def estimate(symbol: str, net) -> SpreadEstimate:
    return SpreadEstimate(
        symbol, OpportunityLeg("a", D(100), D("0.1")), OpportunityLeg("b", D("101.5"), D("0.1")), D("1.5"), D(net), D(10_000_000), NOW
    )


def test_counters_aggregate_correctly():
    stats = DefaultStatisticsCollector(FakeConfigProvider(), FixedClock(NOW))

    stats.record_scan_tick(800)
    stats.record_scan_tick(820)
    stats.record_opportunity(estimate("BTC/USDT:USDT", "0.9"))
    stats.record_opportunity(estimate("BTC/USDT:USDT", "1.1"))
    stats.record_opportunity(estimate("ETH/USDT:USDT", "0.5"))
    stats.record_request("a", timedelta(milliseconds=120), success=True)
    stats.record_request("b", timedelta(milliseconds=30), success=False)
    stats.record_markets("a", "Alpha", 500, ExchangeFees(D("0.1"), D("0.02")), NetworkMode.DRY_RUN)

    report = stats.snapshot(NOW + timedelta(hours=1))

    assert report.scan_ticks == 2
    assert report.tickers_processed == 1620
    assert report.opportunities_detected == 3
    assert report.opportunities_per_hour == 3
    assert report.best_ever is not None
    assert report.best_ever.symbol == "BTC/USDT:USDT"
    assert report.best_ever.net_percent == D("1.1")
    by_id = {e.id: e for e in report.exchanges}
    assert by_id["a"].display_name == "Alpha"
    assert by_id["a"].avg_latency_ms == 120.0
    assert by_id["b"].errors == 1
    assert report.top_symbols[0] == ("BTC/USDT:USDT", 2)


def test_dashboard_reflects_state():
    config = FakeConfigProvider()
    config.current.arbitrage.enabled = True
    stats = DefaultStatisticsCollector(config, FixedClock(NOW))

    position = PositionPair(
        symbol="BTC/USDT:USDT",
        long_exchange_id="a",
        short_exchange_id="b",
        long_size=D(1),
        short_size=D(1),
        entry_long=D(100),
        entry_short=D(101),
        opened_at=NOW,
        simulated=True,
    )
    stats.record_opened(position)

    dashboard = stats.build_dashboard([estimate("BTC/USDT:USDT", "0.9")], online_exchanges=2, tracked_symbols=42)
    assert dashboard.open_positions == 1
    assert dashboard.online_exchanges == 2
    assert dashboard.tracked_symbols == 42
    assert dashboard.trading_enabled
    assert dashboard.pnl_simulated

    position.exit_long = D("100.8")
    position.exit_short = D("100.9")
    position.fees_exit_usd = D("0.2")
    position.realized_pnl_usd = D("0.3")
    stats.record_closed(position)

    dashboard = stats.build_dashboard([], 2, 42)
    assert dashboard.open_positions == 0
    assert dashboard.realized_pnl_usd == D("0.3")


def test_latency_is_aggregated_for_report_and_dashboard():
    stats = DefaultStatisticsCollector(FakeConfigProvider(), FixedClock(NOW))
    assert stats.snapshot(NOW).latency is None

    stats.record_tick_duration(100)
    stats.record_tick_duration(300)
    stats.record_quote_age(50)
    stats.record_quote_age(150)

    latency = stats.snapshot(NOW).latency
    assert latency is not None
    assert (latency.ticks, latency.avg_tick_ms, latency.max_tick_ms) == (2, 200, 300)
    assert (latency.signals, latency.avg_quote_age_ms, latency.max_quote_age_ms) == (2, 100, 150)

    dashboard = stats.build_dashboard([], 2, 0)
    assert dashboard.last_tick_ms == 300
    assert dashboard.avg_quote_age_ms == 100
