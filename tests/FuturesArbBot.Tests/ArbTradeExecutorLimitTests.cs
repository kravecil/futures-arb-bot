using FuturesArbBot.Core.Domain;
using FuturesArbBot.Core.Engine;
using Microsoft.Extensions.Logging.Abstractions;

namespace FuturesArbBot.Tests;

/// <summary>
/// Тесты limit-исполнения: отказ/частичное исполнение/таймаут ног при открытии,
/// ребалансировка перекоса и поноговое закрытие (режим Testnet на фейковых биржах).
/// </summary>
public class ArbTradeExecutorLimitTests
{
    private static readonly DateTimeOffset Now = new(2026, 1, 1, 12, 0, 0, TimeSpan.Zero);

    private static (ArbTradeExecutor Executor, FakeConnector Long, FakeConnector Short, StatisticsCollector Stats) CreateTestnet()
    {
        var config = new FakeConfigProvider();
        config.Current.General.NetworkMode = NetworkMode.Testnet;
        config.Current.Arbitrage.Enabled = true;
        config.Current.Arbitrage.OrderSizeUsd = 100m;
        config.Current.Arbitrage.MinSpreadPercentDown = 0.15m;
        config.Current.Arbitrage.StopLossSpreadPercent = 2.5m;
        config.Current.Arbitrage.OrderExecutionTimeoutMs = 250;
        config.Current.Arbitrage.OrderPollIntervalMs = 50;
        config.Current.Arbitrage.RebalanceTolerancePercent = 1.0m;

        var longConnector = new FakeConnector("binanceusdm", takerPercent: 0.1m);
        var shortConnector = new FakeConnector("bybit", takerPercent: 0.1m);

        var registry = new ConnectorRegistry();
        registry.Replace([longConnector, shortConnector]);

        var time = new SteppingTimeProvider(Now, TimeSpan.FromMilliseconds(60));
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

    private static SpreadEstimate Estimate(string symbol, decimal longPrice, decimal shortPrice) => new(
        symbol,
        new OpportunityLeg("binanceusdm", longPrice, 0.1m),
        new OpportunityLeg("bybit", shortPrice, 0.1m),
        (shortPrice - longPrice) / longPrice * 100m,
        1m,
        10_000_000m,
        Now);

    private static Dictionary<string, IReadOnlyDictionary<string, TickerSnapshot>> Tickers(
        FakeConnector longs, FakeConnector shorts) => new(StringComparer.Ordinal)
        {
            ["binanceusdm"] = longs.Tickers,
            ["bybit"] = shorts.Tickers,
        };

    private static void SetNeutralTickers(FakeConnector longs, FakeConnector shorts)
    {
        // спред ~1%: не тейк (порог 0.15% снизу) и не стоп (порог 2.5% сверху)
        longs.Tickers = new Dictionary<string, TickerSnapshot>
        {
            ["BTC/USDT:USDT"] = TestTickers.Make("binanceusdm", "BTC/USDT:USDT", bid: 99.9m, ask: 100m),
        };
        shorts.Tickers = new Dictionary<string, TickerSnapshot>
        {
            ["BTC/USDT:USDT"] = TestTickers.Make("bybit", "BTC/USDT:USDT", bid: 101m, ask: 101.1m),
        };
    }

    [Fact]
    public async Task First_leg_rejection_prevents_second_leg_placement()
    {
        var (executor, longConnector, shortConnector, stats) = CreateTestnet();
        longConnector.PlaceOrderOverride = (_, _) => OrderResult.Fail("отказ биржи");

        await executor.ProcessOpportunitiesAsync([Estimate("BTC/USDT:USDT", 100m, 101m)], CancellationToken.None);

        Assert.False(executor.HasOpenPositions);
        Assert.Empty(shortConnector.PlacedOrders); // вторая нога не выставлялась вообще
        var report = stats.Snapshot(Now.AddMinutes(1));
        Assert.Equal(0, report.OrdersOpened);
        Assert.Equal(1, report.OrdersFailed);
    }

    [Fact]
    public async Task Second_leg_rejection_rolls_back_full_first_leg()
    {
        var (executor, longConnector, shortConnector, stats) = CreateTestnet();
        shortConnector.PlaceOrderOverride = (_, _) => OrderResult.Fail("нет маржи");

        await executor.ProcessOpportunitiesAsync([Estimate("BTC/USDT:USDT", 100m, 101m)], CancellationToken.None);

        Assert.False(executor.HasOpenPositions);
        var longOrders = longConnector.PlacedOrders.Values.ToList();
        Assert.Equal(2, longOrders.Count);
        Assert.Equal(OrderType.Limit, longOrders[0].Type); // исходный BUY
        Assert.Equal(OrderSide.Buy, longOrders[0].Side);
        Assert.False(longOrders[0].ReduceOnly);

        var rollback = longOrders[^1]; // откат
        Assert.Equal(OrderSide.Sell, rollback.Side);
        Assert.Equal(OrderType.Market, rollback.Type);
        Assert.True(rollback.ReduceOnly);
        Assert.Equal(1m, rollback.Amount);

        var report = stats.Snapshot(Now.AddMinutes(1));
        Assert.Equal(0, report.OrdersOpened);
        Assert.Equal(1, report.OrdersFailed);
    }

    [Fact]
    public async Task Partial_first_leg_is_rolled_back_only_to_filled_size()
    {
        var (executor, longConnector, shortConnector, _) = CreateTestnet();
        longConnector.PlaceOrderOverride = (request, index) => request.ReduceOnly
            ? OrderResult.Ok($"rollback-{index}", null, request.Amount)            // откат проходит целиком
            : OrderResult.Ok($"open-{index}", request.Price, request.Amount / 2m); // набрана половина
        shortConnector.PlaceOrderOverride = (_, _) => OrderResult.Fail("нет ликвидности");

        await executor.ProcessOpportunitiesAsync([Estimate("BTC/USDT:USDT", 100m, 101m)], CancellationToken.None);

        Assert.False(executor.HasOpenPositions);
        var rollback = longConnector.PlacedOrders.Values.Last();
        Assert.True(rollback.ReduceOnly);
        Assert.Equal(OrderType.Market, rollback.Type);
        Assert.Equal(0.5m, rollback.Amount); // откатывается только набранный объём, не весь запрос
    }

    [Fact]
    public async Task Limit_timeout_cancels_both_legs_and_rolls_back_fills()
    {
        var (executor, longConnector, shortConnector, stats) = CreateTestnet();
        longConnector.PlaceOrderOverride = (request, index) => request.ReduceOnly
            ? OrderResult.Ok($"rollback-{index}", null, request.Amount)
            : OrderResult.Ok($"open-{index}", request.Price, 0.4m); // принято, но не доисполняется
        shortConnector.PlaceOrderOverride = (request, index) => OrderResult.Ok($"open-{index}", request.Price, 0m);

        await executor.ProcessOpportunitiesAsync([Estimate("BTC/USDT:USDT", 100m, 101m)], CancellationToken.None);

        Assert.False(executor.HasOpenPositions);
        Assert.Single(longConnector.CancelledOrders);  // обе неисполненные заявки…
        Assert.Single(shortConnector.CancelledOrders); // …сняты по таймауту

        var rollback = longConnector.PlacedOrders.Values.Last();
        Assert.True(rollback.ReduceOnly);
        Assert.Equal(0.4m, rollback.Amount); // набранный объём откатан

        var report = stats.Snapshot(Now.AddMinutes(1));
        Assert.Equal(0, report.OrdersOpened);
        Assert.Equal(1, report.OrdersFailed);
    }

    [Fact]
    public async Task Failed_rollback_leaves_position_tracked_for_maintenance()
    {
        var (executor, longConnector, shortConnector, stats) = CreateTestnet();
        longConnector.PlaceOrderOverride = (request, index) => request.ReduceOnly
            ? OrderResult.Fail("биржа не отдаёт объём")                       // откат не проходит
            : OrderResult.Ok($"open-{index}", request.Price, request.Amount); // лонг набран полностью
        shortConnector.PlaceOrderOverride = (_, _) => OrderResult.Fail("нет ликвидности");

        await executor.ProcessOpportunitiesAsync([Estimate("BTC/USDT:USDT", 100m, 101m)], CancellationToken.None);

        // нога не должна «теряться»: позиция остаётся в учёте для цикла ведения
        Assert.True(executor.HasOpenPositions);
        var report = stats.Snapshot(Now.AddMinutes(1));
        Assert.Single(report.StillOpen);
        Assert.Equal(1m, report.StillOpen[0].LongSize);
        Assert.Equal(0m, report.StillOpen[0].ShortSize);
    }

    [Fact]
    public async Task Rebalance_cuts_larger_leg_with_reduce_only_limit()
    {
        var (executor, longConnector, shortConnector, _) = CreateTestnet();
        // «сбой биржи»: шортовая нога набрала вдвое больше запрошенного
        shortConnector.PlaceOrderOverride = (request, index) => request.ReduceOnly
            ? null
            : OrderResult.Ok($"short-{index}", request.Price, request.Amount * 2m);

        await executor.ProcessOpportunitiesAsync([Estimate("BTC/USDT:USDT", 100m, 101m)], CancellationToken.None);
        Assert.True(executor.HasOpenPositions);

        SetNeutralTickers(longConnector, shortConnector);

        var shortsBefore = shortConnector.PlacedOrders.Count;
        await executor.ManageOpenPositionsAsync(Tickers(longConnector, shortConnector), CancellationToken.None);

        var cut = shortConnector.PlacedOrders.Values.Last();
        Assert.Equal(shortsBefore + 1, shortConnector.PlacedOrders.Count);
        Assert.Equal(OrderSide.Buy, cut.Side);   // урезаем шорт — выкупаем
        Assert.Equal(OrderType.Limit, cut.Type); // ребаланс — лимиткой
        Assert.True(cut.ReduceOnly);
        Assert.Equal(1m, cut.Amount);            // перекос 2 − 1 = 1 BTC
        Assert.Equal(101.1m, cut.Price);         // по ask биржи, где выкупаем

        // второй тик: позиция сбалансирована — лишних ордеров нет
        await executor.ManageOpenPositionsAsync(Tickers(longConnector, shortConnector), CancellationToken.None);
        Assert.Equal(shortsBefore + 1, shortConnector.PlacedOrders.Count);
    }

    [Fact]
    public async Task Balanced_position_is_not_rebalanced()
    {
        var (executor, longConnector, shortConnector, _) = CreateTestnet();

        await executor.ProcessOpportunitiesAsync([Estimate("BTC/USDT:USDT", 100m, 101m)], CancellationToken.None);
        SetNeutralTickers(longConnector, shortConnector);

        await executor.ManageOpenPositionsAsync(Tickers(longConnector, shortConnector), CancellationToken.None);

        Assert.True(executor.HasOpenPositions);
        Assert.Single(longConnector.PlacedOrders);  // только открывающий BUY
        Assert.Single(shortConnector.PlacedOrders); // только открывающий SELL
    }

    [Fact]
    public async Task Close_handles_mismatched_leg_sizes_and_keeps_record()
    {
        var (executor, longConnector, shortConnector, stats) = CreateTestnet();
        // шорт набрал 2 BTC при запросе 1 — ноги рассогласованы
        shortConnector.PlaceOrderOverride = (request, index) => request.ReduceOnly
            ? null
            : OrderResult.Ok($"short-{index}", request.Price, request.Amount * 2m);

        await executor.ProcessOpportunitiesAsync([Estimate("BTC/USDT:USDT", 100m, 101m)], CancellationToken.None);

        // стоп-лосс: спред расширился до ~11% ≥ 2.5%
        longConnector.Tickers = new Dictionary<string, TickerSnapshot>
        {
            ["BTC/USDT:USDT"] = TestTickers.Make("binanceusdm", "BTC/USDT:USDT", bid: 104.9m, ask: 105m),
        };
        shortConnector.Tickers = new Dictionary<string, TickerSnapshot>
        {
            ["BTC/USDT:USDT"] = TestTickers.Make("bybit", "BTC/USDT:USDT", bid: 111m, ask: 111.2m),
        };

        await executor.ManageOpenPositionsAsync(Tickers(longConnector, shortConnector), CancellationToken.None);

        Assert.False(executor.HasOpenPositions);

        // каждая нога закрыта по своему фактическому объёму market reduceOnly
        var longClose = longConnector.PlacedOrders.Values.Last();
        Assert.True(longClose.ReduceOnly);
        Assert.Equal(OrderSide.Sell, longClose.Side);
        Assert.Equal(1m, longClose.Amount);

        var shortClose = shortConnector.PlacedOrders.Values.Last();
        Assert.True(shortClose.ReduceOnly);
        Assert.Equal(OrderSide.Buy, shortClose.Side);
        Assert.Equal(2m, shortClose.Amount);

        var trade = Assert.Single(stats.Snapshot(Now.AddMinutes(1)).ClosedPositions);
        Assert.Equal(CloseReason.StopLoss, trade.Reason);
        Assert.Equal(1m, trade.Size); // встречный закрытый объём — min(1, 2)
    }
}
