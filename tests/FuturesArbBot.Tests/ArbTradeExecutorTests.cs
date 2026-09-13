using FuturesArbBot.Core.Abstractions;
using FuturesArbBot.Core.Domain;
using FuturesArbBot.Core.Engine;
using Microsoft.Extensions.Logging.Abstractions;

namespace FuturesArbBot.Tests;

public class ArbTradeExecutorTests
{
    private static readonly DateTimeOffset Now = new(2026, 1, 1, 12, 0, 0, TimeSpan.Zero);

    private static (ArbTradeExecutor Executor, FakeConnector Long, FakeConnector Short, StatisticsCollector Stats) Create(decimal orderSizeUsd = 100m)
    {
        var config = new FakeConfigProvider();
        config.Current.General.NetworkMode = NetworkMode.DryRun;
        config.Current.Arbitrage.Enabled = true;
        config.Current.Arbitrage.OrderSizeUsd = orderSizeUsd;
        config.Current.Arbitrage.MinSpreadPercentUp = 0.4m;
        config.Current.Arbitrage.MinSpreadPercentDown = 0.15m;
        config.Current.Arbitrage.IncludeFees = true;
        config.Current.Arbitrage.SlippageBufferPercent = 0m;

        var longConnector = new FakeConnector("binanceusdm", takerPercent: 0.1m);
        var shortConnector = new FakeConnector("bybit", takerPercent: 0.1m);

        var registry = new ConnectorRegistry();
        registry.Replace([longConnector, shortConnector]);

        var time = new FakeTimeProvider(Now);
        var stats = new StatisticsCollector(config, time);
        var executor = new ArbTradeExecutor(
            config,
            registry,
            stats,
            new EventLog(config, time),
            time,
            NullLogger<ArbTradeExecutor>.Instance);

        return (executor, longConnector, shortConnector, stats);
    }

    private static SpreadEstimate Estimate(string symbol, decimal longPrice, decimal shortPrice, decimal volume = 10_000_000m) => new(
        symbol,
        new OpportunityLeg("binanceusdm", longPrice, 0.1m),
        new OpportunityLeg("bybit", shortPrice, 0.1m),
        Gross(longPrice, shortPrice),
        1m,
        volume,
        Now);

    private static decimal Gross(decimal longPrice, decimal shortPrice) => (shortPrice - longPrice) / longPrice * 100m;

    [Fact]
    public async Task Dry_run_opens_and_closes_with_take_profit_pnl()
    {
        var (executor, longConnector, shortConnector, stats) = Create();

        // вход: лонг по 100 (ask дешёвой), шорт по 101 (bid дорогой)
        var estimate = Estimate("BTC/USDT:USDT", longPrice: 100m, shortPrice: 101m);
        await executor.ProcessOpportunitiesAsync([estimate], CancellationToken.None);

        Assert.True(executor.HasOpenPositions);

        // следующий тик: спред сжался до ~0.1% ≤ порога закрытия 0.15%
        longConnector.Tickers = new Dictionary<string, TickerSnapshot>
        {
            ["BTC/USDT:USDT"] = TestTickers.Make("binanceusdm", "BTC/USDT:USDT", bid: 100.4m, ask: 100.5m),
        };
        shortConnector.Tickers = new Dictionary<string, TickerSnapshot>
        {
            ["BTC/USDT:USDT"] = TestTickers.Make("bybit", "BTC/USDT:USDT", bid: 100.6m, ask: 100.7m),
        };

        await executor.ManageOpenPositionsAsync(
            new Dictionary<string, IReadOnlyDictionary<string, TickerSnapshot>>
            {
                ["binanceusdm"] = longConnector.Tickers,
                ["bybit"] = shortConnector.Tickers,
            },
            CancellationToken.None);

        Assert.False(executor.HasOpenPositions);

        var report = stats.Snapshot(Now.AddMinutes(1));
        Assert.Equal(1, report.OrdersOpened);
        var trade = Assert.Single(report.ClosedPositions);
        Assert.Equal(CloseReason.TakeProfit, trade.Reason);

        // размер = 100 USD / 100 = 1 BTC
        Assert.Equal(1m, trade.Size);
        // PnL = 1 × ((100.4 − 100) + (101 − 100.7)) − комиссии (0.1 % × 4 сделки)
        var expectedPnl = (100.4m - 100m) + (101m - 100.7m)
                          - (100m + 101m + 100.4m + 100.7m) * 0.1m / 100m;
        Assert.Equal(expectedPnl, trade.PnlUsd, 6);
        Assert.True(trade.Simulated);
    }

    [Fact]
    public async Task Stop_loss_closes_when_spread_explodes()
    {
        var (executor, longConnector, shortConnector, stats) = Create();
        var estimate = Estimate("ETH/USDT:USDT", longPrice: 2000m, shortPrice: 2009m);
        await executor.ProcessOpportunitiesAsync([estimate], CancellationToken.None);

        longConnector.Tickers = new Dictionary<string, TickerSnapshot>
        {
            ["ETH/USDT:USDT"] = TestTickers.Make("binanceusdm", "ETH/USDT:USDT", bid: 1999m, ask: 2000m),
        };
        shortConnector.Tickers = new Dictionary<string, TickerSnapshot>
        {
            ["ETH/USDT:USDT"] = TestTickers.Make("bybit", "ETH/USDT:USDT", bid: 2060m, ask: 2061m),
        };

        await executor.ManageOpenPositionsAsync(
            new Dictionary<string, IReadOnlyDictionary<string, TickerSnapshot>>
            {
                ["binanceusdm"] = longConnector.Tickers,
                ["bybit"] = shortConnector.Tickers,
            },
            CancellationToken.None);

        var trade = Assert.Single(stats.Snapshot(Now.AddMinutes(1)).ClosedPositions);
        Assert.Equal(CloseReason.StopLoss, trade.Reason);
        Assert.True(trade.PnlUsd < 0m);
    }

    [Fact]
    public async Task Duplicate_symbol_is_not_opened_twice()
    {
        var (executor, _, _, stats) = Create();
        var estimate = Estimate("BTC/USDT:USDT", 100m, 101m);

        await executor.ProcessOpportunitiesAsync([estimate], CancellationToken.None);
        await executor.ProcessOpportunitiesAsync([estimate with { DetectedAt = Now.AddSeconds(5) }], CancellationToken.None);

        Assert.Equal(1, stats.Snapshot(Now.AddMinutes(1)).OrdersOpened);
    }
}
