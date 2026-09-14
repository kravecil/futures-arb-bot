using FuturesArbBot.Core.Domain;
using FuturesArbBot.Core.Engine;

namespace FuturesArbBot.Tests;

public class StatisticsCollectorTests
{
    private static readonly DateTimeOffset Now = new(2026, 1, 1, 12, 0, 0, TimeSpan.Zero);

    private static SpreadEstimate Estimate(string symbol, decimal net) => new(
        symbol,
        new OpportunityLeg("a", 100m, 0.1m),
        new OpportunityLeg("b", 101.5m, 0.1m),
        1.5m,
        net,
        10_000_000m,
        Now);

    [Fact]
    public void Counters_aggregate_correctly()
    {
        var config = new FakeConfigProvider();
        var stats = new StatisticsCollector(config, new FakeTimeProvider(Now));

        stats.RecordScanTick(800);
        stats.RecordScanTick(820);
        stats.RecordOpportunity(Estimate("BTC/USDT:USDT", 0.9m));
        stats.RecordOpportunity(Estimate("BTC/USDT:USDT", 1.1m));
        stats.RecordOpportunity(Estimate("ETH/USDT:USDT", 0.5m));
        stats.RecordRequest("a", TimeSpan.FromMilliseconds(120), success: true);
        stats.RecordRequest("b", TimeSpan.FromMilliseconds(30), success: false);
        stats.RecordMarkets("a", "Alpha", 500, new ExchangeFees(0.1m, 0.02m), NetworkMode.DryRun);

        var report = stats.Snapshot(Now.AddHours(1));

        Assert.Equal(2, report.ScanTicks);
        Assert.Equal(1620, report.TickersProcessed);
        Assert.Equal(3, report.OpportunitiesDetected);
        Assert.Equal(3, report.OpportunitiesPerHour);
        Assert.Equal("BTC/USDT:USDT", report.BestEver!.Symbol);
        Assert.Equal(1.1m, report.BestEver.NetPercent);
        Assert.Equal("Alpha", report.Exchanges.Single(e => e.Id == "a").DisplayName);
        Assert.Equal(120.0, report.Exchanges.Single(e => e.Id == "a").AvgLatencyMs);
        Assert.Equal(1, report.Exchanges.Single(e => e.Id == "b").Errors);

        var top = report.TopSymbols;
        Assert.Equal("BTC/USDT:USDT", top[0].Key);
        Assert.Equal(2, top[0].Value);
    }

    [Fact]
    public void Dashboard_reflects_state()
    {
        var config = new FakeConfigProvider();
        config.Current.Arbitrage.Enabled = true;
        var stats = new StatisticsCollector(config, new FakeTimeProvider(Now));

        var position = new PositionPair
        {
            Id = Guid.NewGuid(),
            Symbol = "BTC/USDT:USDT",
            LongExchangeId = "a",
            ShortExchangeId = "b",
            LongSize = 1m,
            ShortSize = 1m,
            EntryLong = 100m,
            EntryShort = 101m,
            OpenedAt = Now,
            Simulated = true,
        };
        stats.RecordOpened(position);

        var dashboard = stats.BuildDashboard([Estimate("BTC/USDT:USDT", 0.9m)], onlineExchanges: 2, trackedSymbols: 42);
        Assert.Equal(1, dashboard.OpenPositions);
        Assert.Equal(2, dashboard.OnlineExchanges);
        Assert.Equal(42, dashboard.TrackedSymbols);
        Assert.True(dashboard.TradingEnabled);
        Assert.True(dashboard.PnlSimulated);

        position.ExitLong = 100.8m;
        position.ExitShort = 100.9m;
        position.FeesExitUsd = 0.2m;
        position.RealizedPnlUsd = 0.3m;
        stats.RecordClosed(position);

        dashboard = stats.BuildDashboard([], 2, 42);
        Assert.Equal(0, dashboard.OpenPositions);
        Assert.Equal(0.3m, dashboard.RealizedPnlUsd);
    }
}
