using FuturesArbBot.Core.Engine;

namespace FuturesArbBot.Tests;

public class SymbolFilterTests
{
    private static MarketInfo Market(string symbol, string quote = "USDT", bool perp = true, bool linear = true) =>
        new(symbol, perp, linear, quote, null, 0.1m, 0.02m);

    [Fact]
    public void Allows_linear_perp_in_usdt_with_volume()
    {
        var filter = new SymbolFilter(new FakeConfigProvider());
        Assert.True(filter.IsAllowed(Market("BTC/USDT:USDT"), 10_000_000m));
    }

    [Fact]
    public void Rejects_non_usdt_quote()
    {
        var filter = new SymbolFilter(new FakeConfigProvider());
        Assert.False(filter.IsAllowed(Market("BTC/USD:BTC", quote: "BTC"), 10_000_000m));
    }

    [Fact]
    public void Rejects_low_volume()
    {
        var config = new FakeConfigProvider();
        config.Current.Symbols.MinQuoteVolume24hUsd = 1_000_000m;
        var filter = new SymbolFilter(config);

        Assert.False(filter.IsAllowed(Market("XYZ/USDT:USDT"), 999_999m));
        Assert.True(filter.IsAllowed(Market("XYZ/USDT:USDT"), 1_500_000m));
    }

    [Fact]
    public void Rejects_spot_and_inverse()
    {
        var filter = new SymbolFilter(new FakeConfigProvider());
        Assert.False(filter.IsAllowed(Market("BTC/USDT", perp: false), 10_000_000m));
        Assert.False(filter.IsAllowed(Market("BTC/USD:BTC", linear: false), 10_000_000m));
    }

    [Fact]
    public void Exclude_wildcard_blocks_matching_symbols()
    {
        var config = new FakeConfigProvider();
        config.Current.Symbols.Exclude = ["*UP/USDT:*", "1000?ATS/USDT:*"];
        var filter = new SymbolFilter(config);

        Assert.False(filter.IsAllowed(Market("BTCUP/USDT:USDT"), 10_000_000m));
        Assert.False(filter.IsAllowed(Market("1000CATS/USDT:USDT"), 10_000_000m));
        Assert.True(filter.IsAllowed(Market("BTC/USDT:USDT"), 10_000_000m));
    }

    [Fact]
    public void Include_whitelist_narrows_universe()
    {
        var config = new FakeConfigProvider();
        config.Current.Symbols.Include = ["BTC/*", "ETH/*"];
        var filter = new SymbolFilter(config);

        Assert.True(filter.IsAllowed(Market("BTC/USDT:USDT"), 10_000_000m));
        Assert.True(filter.IsAllowed(Market("ETH/USDT:USDT"), 10_000_000m));
        Assert.False(filter.IsAllowed(Market("SOL/USDT:USDT"), 10_000_000m));
    }

    [Fact]
    public void Volume_zero_passes_market_level_check()
    {
        // на этапе загрузки рынков объём ещё неизвестен (0) — фильтр не должен отбрасывать
        var filter = new SymbolFilter(new FakeConfigProvider());
        Assert.True(filter.IsAllowed(Market("BTC/USDT:USDT"), 0m));
    }
}
