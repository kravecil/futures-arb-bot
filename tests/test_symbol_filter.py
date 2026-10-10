"""Порт SymbolFilterTests.cs — фильтр пар, квотиры, оборот, include/exclude."""

from decimal import Decimal

from fakes import FakeConfigProvider

from futures_arb_bot.domain.market_data import MarketInfo
from futures_arb_bot.engine.symbol_filter import SymbolFilter

D = Decimal


def market(symbol: str, perpetual=True, linear=True, quote="USDT", min_amount=None) -> MarketInfo:
    return MarketInfo(symbol, perpetual, linear, quote, min_amount, D("0.05"), D("0.02"))


def create_filter(**changes) -> SymbolFilter:
    config = FakeConfigProvider()
    symbols = config.Current.Symbols
    for key, value in changes.items():
        setattr(symbols, key, value)
    return SymbolFilter(config)


def test_allows_liquid_linear_perpetual():
    assert create_filter().IsAllowed(market("BTC/USDT:USDT"), D("10000000"))


def test_rejects_spot_and_inverse():
    assert not create_filter().IsAllowed(market("BTC/USDT", perpetual=False), D("10000000"))
    assert not create_filter().IsAllowed(market("BTC/USD:BTC", linear=False), D("10000000"))


def test_rejects_other_quote_currency():
    assert not create_filter(QuoteCurrencies=["USDT"]).IsAllowed(market("BTC/USD:USDC", quote="USDC"), D("10000000"))


def test_rejects_low_volume_and_allows_unknown_volume():
    f = create_filter(MinQuoteVolume24hUsd=D("5000000"))
    assert not f.IsAllowed(market("X/USDT:USDT"), D("4999999"))
    assert f.IsAllowed(market("X/USDT:USDT"), D("5000000"))
    # объём 0 = «биржа не отдаёт оборот» — не отбрасываем
    assert f.IsAllowed(market("X/USDT:USDT"), D("0"))


def test_include_whitelist_and_exclude_blacklist():
    f = create_filter(Include=["*UP/USDT:*", "BTC/USDT:USDT"], Exclude=["*BTC/USDT:USDT"])
    assert f.IsAllowed(market("SOLUP/USDT:USDT"), D("0"))
    assert not f.IsAllowed(market("SOL/USDT:USDT"), D("0"))
    # exclude сильнее include
    assert not f.IsAllowed(market("BTC/USDT:USDT"), D("0"))


def test_wildcard_patterns_are_case_insensitive_fullmatch():
    f = create_filter(Include=["btc/usdt:*"])
    assert f.IsAllowed(market("BTC/USDT:USDT"), D("0"))
    assert not f.IsAllowed(market("XBTC/USDT:USDT"), D("0"))
    f2 = create_filter(Exclude=["?ETH/USDT:*"])
    assert not f2.IsAllowed(market("BETH/USDT:USDT"), D("0"))
    assert f2.IsAllowed(market("BBETH/USDT:USDT"), D("0"))
