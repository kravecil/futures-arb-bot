from futures_arb_bot.core.domain.market_data import MarketInfo
from futures_arb_bot.core.engine.symbol_filter import DefaultSymbolFilter

from .fakes import D, FakeConfigProvider

TEN_MILLION = D(10_000_000)


def market(symbol: str, quote: str = "USDT", perp: bool = True, linear: bool = True) -> MarketInfo:
    return MarketInfo(symbol, perp, linear, quote, None, D("0.1"), D("0.02"))


def test_allows_linear_perp_in_usdt_with_volume():
    assert DefaultSymbolFilter(FakeConfigProvider()).is_allowed(market("BTC/USDT:USDT"), TEN_MILLION)


def test_rejects_non_usdt_quote():
    assert not DefaultSymbolFilter(FakeConfigProvider()).is_allowed(market("BTC/USD:BTC", quote="BTC"), TEN_MILLION)


def test_rejects_low_volume():
    config = FakeConfigProvider()
    config.current.symbols.min_quote_volume24h_usd = D(1_000_000)
    symbol_filter = DefaultSymbolFilter(config)

    assert not symbol_filter.is_allowed(market("XYZ/USDT:USDT"), D(999_999))
    assert symbol_filter.is_allowed(market("XYZ/USDT:USDT"), D(1_500_000))


def test_rejects_spot_and_inverse():
    symbol_filter = DefaultSymbolFilter(FakeConfigProvider())
    assert not symbol_filter.is_allowed(market("BTC/USDT", perp=False), TEN_MILLION)
    assert not symbol_filter.is_allowed(market("BTC/USD:BTC", linear=False), TEN_MILLION)


def test_exclude_wildcard_blocks_matching_symbols():
    config = FakeConfigProvider()
    config.current.symbols.exclude = ["*UP/USDT:*", "1000?ATS/USDT:*"]
    symbol_filter = DefaultSymbolFilter(config)

    assert not symbol_filter.is_allowed(market("BTCUP/USDT:USDT"), TEN_MILLION)
    assert not symbol_filter.is_allowed(market("1000CATS/USDT:USDT"), TEN_MILLION)
    assert symbol_filter.is_allowed(market("BTC/USDT:USDT"), TEN_MILLION)


def test_include_whitelist_narrows_universe():
    config = FakeConfigProvider()
    config.current.symbols.include = ["BTC/*", "ETH/*"]
    symbol_filter = DefaultSymbolFilter(config)

    assert symbol_filter.is_allowed(market("BTC/USDT:USDT"), TEN_MILLION)
    assert symbol_filter.is_allowed(market("ETH/USDT:USDT"), TEN_MILLION)
    assert not symbol_filter.is_allowed(market("SOL/USDT:USDT"), TEN_MILLION)


def test_volume_zero_passes_market_level_check():
    # на этапе загрузки рынков объём ещё неизвестен (0) — фильтр не должен отбрасывать
    assert DefaultSymbolFilter(FakeConfigProvider()).is_allowed(market("BTC/USDT:USDT"), D(0))
