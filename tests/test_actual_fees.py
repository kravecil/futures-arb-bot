"""Фактические комиссии: берутся из ответа биржи (fee/fees CCXT), а без них — оценка по taker."""

from datetime import UTC, datetime, timedelta

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.core.domain.enums import NetworkMode
from futures_arb_bot.core.domain.market_data import OrderResult
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.trade_executor import ArbTradeExecutor
from futures_arb_bot.infrastructure.ccxt_connector import fee_in_quote

from .fakes import D, FakeConfigProvider, FakeConnector, SteppingClock, make_log, ticker

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NONE = CancellationToken.none()
BTC = "BTC/USDT:USDT"


async def create():
    config = FakeConfigProvider()
    config.current.general.network_mode = NetworkMode.TESTNET
    config.current.arbitrage.enabled = True
    config.current.arbitrage.order_size_usd = D(100)
    config.current.arbitrage.min_spread_percent_down = D("0.15")
    config.current.arbitrage.stop_loss_spread_percent = D("2.5")
    config.current.arbitrage.order_execution_timeout_ms = 250
    config.current.arbitrage.order_poll_interval_ms = 20
    long_c = FakeConnector("binanceusdm", taker_percent=D("0.1"))
    short_c = FakeConnector("bybit", taker_percent=D("0.1"))
    registry = ConnectorRegistry()
    await registry.replace([long_c, short_c])
    clock = SteppingClock(NOW, timedelta(milliseconds=60))
    stats = DefaultStatisticsCollector(config, clock)
    return ArbTradeExecutor(config, registry, stats, make_log(config, clock), clock), long_c, short_c, stats


def estimate() -> SpreadEstimate:
    return SpreadEstimate(BTC, OpportunityLeg("binanceusdm", D(100), D("0.1")), OpportunityLeg("bybit", D(101), D("0.1")), D(1), D(1), D(10**7), NOW)


async def test_reported_maker_fees_replace_taker_estimate():
    executor, long_c, short_c, stats = await create()
    # обе ноги исполнены мейкером: 0.02 % вместо 0.1 % taker
    long_c.place_order_override = lambda r, i: OrderResult.ok(f"l{i}", r.price or D(100), r.amount, fee_quote=D("0.02"))
    short_c.place_order_override = lambda r, i: OrderResult.ok(f"s{i}", r.price or D(101), r.amount, fee_quote=D("0.0202"))

    await executor.process_opportunities([estimate()], NONE)
    [position] = stats.snapshot(NOW).still_open
    assert position.fees_entry_usd == D("0.0402")

    # стоп-лосс: закрытие тоже с фактическими комиссиями
    long_c.tickers = {BTC: ticker("binanceusdm", BTC, D("104.9"), D(105))}
    short_c.tickers = {BTC: ticker("bybit", BTC, D(111), D("111.2"))}
    await executor.manage_open_positions({"binanceusdm": long_c.tickers, "bybit": short_c.tickers}, NONE)

    [trade] = stats.snapshot(NOW).closed_positions
    assert trade.fees_usd == D("0.0402") + D("0.0402")


async def test_missing_fee_falls_back_to_taker_estimate():
    executor, _, _, stats = await create()
    await executor.process_opportunities([estimate()], NONE)
    [position] = stats.snapshot(NOW).still_open
    # 1 BTC × (100 + 101) × 0.1 %
    assert position.fees_entry_usd == D("0.201")


def test_fee_in_quote_converts_base_currency_and_rejects_foreign():
    assert fee_in_quote({"fee": {"cost": 0.05, "currency": "USDT"}}, BTC, D(100)) == D("0.05")
    assert fee_in_quote({"fees": [{"cost": 0.0001, "currency": "BTC"}, {"cost": 0.01, "currency": "USDT"}]}, BTC, D(100)) == D("0.02")
    assert fee_in_quote({"fee": {"cost": -0.01, "currency": "USDT"}}, BTC, None) == D("-0.01")  # ребейт мейкеру
    assert fee_in_quote({"fee": {"cost": 0.1, "currency": "BNB"}}, BTC, D(100)) is None
    assert fee_in_quote({"fee": None}, BTC, D(100)) is None
