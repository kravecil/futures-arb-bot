using FuturesArbBot.Core.Abstractions;
using FuturesArbBot.Core.Domain;
using FuturesArbBot.Core.Engine;
using Microsoft.Extensions.Logging.Abstractions;

namespace FuturesArbBot.Tests;

/// <summary>
/// Исполнение заявок по политике Execution: смещение цены, локальное догонание
/// (пере-выстав заявок), бюджет шагов и отклонения, market-добивка, нативный chase.
/// </summary>
public class ChaseOrderExecutorTests
{
    private static readonly DateTimeOffset Now = new(2026, 1, 1, 12, 0, 0, TimeSpan.Zero);

    private sealed record Harness(ArbTradeExecutor Executor, FakeConnector Long, FakeConnector Short, StatisticsCollector Stats, IEventLog Log);

    private static Harness Create(
        OrderExecutionOptions execution,
        string longId = "binanceusdm",
        string shortId = "bybit",
        int timeoutMs = 20_000,
        OrderExecutionOptions? shortOverride = null)
    {
        var config = new FakeConfigProvider();
        config.Current.General.NetworkMode = NetworkMode.Testnet;
        config.Current.Arbitrage.Enabled = true;
        config.Current.Arbitrage.OrderSizeUsd = 100m;
        config.Current.Arbitrage.MinSpreadPercentDown = 0.15m;
        config.Current.Arbitrage.StopLossSpreadPercent = 2.5m;
        config.Current.Arbitrage.OrderExecutionTimeoutMs = timeoutMs;
        config.Current.Arbitrage.OrderPollIntervalMs = 20;
        config.Current.Arbitrage.Execution = execution;

        if (shortOverride is not null)
        {
            // так переопределение задаётся в exchanges.json
            config.Current.Exchanges.Items.Add(new ExchangeConfigEntry { Id = shortId, Execution = shortOverride });
        }

        var longs = new FakeConnector(longId);
        var shorts = new FakeConnector(shortId);

        var registry = new ConnectorRegistry();
        registry.Replace([longs, shorts]);

        var time = new SteppingTimeProvider(Now, TimeSpan.FromMilliseconds(60));
        var stats = new StatisticsCollector(config, time);
        var log = new EventLog(config, time);
        var executor = new ArbTradeExecutor(config, registry, stats, log, time, NullLogger<ArbTradeExecutor>.Instance);

        return new Harness(executor, longs, shorts, stats, log);
    }

    private static SpreadEstimate Estimate(string longId, string shortId) => new(
        "BTC/USDT:USDT",
        new OpportunityLeg(longId, 100m, 0.1m),
        new OpportunityLeg(shortId, 101m, 0.1m),
        1m,
        1m,
        10_000_000m,
        Now);

    private static Task OpenAsync(Harness harness, string longId = "binanceusdm", string shortId = "bybit") =>
        harness.Executor.ProcessOpportunitiesAsync([Estimate(longId, shortId)], CancellationToken.None);

    /// <summary>Заявка не исполняется вовсе (биржа держит её в стакане с нулевым объёмом).</summary>
    private static OrderResult NeverFilled(OrderRequest request, int index) => OrderResult.Ok($"o{index}", request.Price, 0m);

    [Fact]
    public async Task Default_policy_keeps_marketable_limit_behaviour()
    {
        var harness = Create(new OrderExecutionOptions());

        await OpenAsync(harness);

        var longOrder = Assert.Single(harness.Long.PlacedOrders.Values);
        Assert.Equal(OrderType.Limit, longOrder.Type);
        Assert.Equal(100m, longOrder.Price);
        Assert.Equal(TimeInForce.Gtc, longOrder.TimeInForce);
        Assert.Null(longOrder.ExchangeParams);
        Assert.Empty(harness.Long.CancelledOrders);
        Assert.True(harness.Executor.HasOpenPositions);
    }

    [Fact]
    public async Task Limit_offset_moves_entry_prices_apart()
    {
        var harness = Create(new OrderExecutionOptions { Type = OrderType.Limit, LimitOffsetBps = 10m });

        await OpenAsync(harness);

        var longOrder = Assert.Single(harness.Long.PlacedOrders.Values);
        var shortOrder = Assert.Single(harness.Short.PlacedOrders.Values);

        // BUY агрессивнее (выше), SELL — ниже; объём ноги: 100 USDT / 100 = 1 BTC
        Assert.Equal(100.1m, longOrder.Price);
        Assert.Equal(100.899m, shortOrder.Price);
        Assert.True(harness.Executor.HasOpenPositions);
    }

    [Fact]
    public async Task Chase_requotes_order_by_step_until_filled()
    {
        var harness = Create(new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions { Mode = ChaseMode.Simulated, MaxSteps = 5, StepIntervalMs = 1, StepBps = 10m, MaxDeviationBps = 1_000m },
        });

        // лонг: первые две заявки не исполняются, третья — да; шорт исполняется сразу
        harness.Long.PlaceOrderOverride = (request, index) => index < 3 ? NeverFilled(request, index) : null;

        await OpenAsync(harness);

        var prices = harness.Long.PlacedOrders.Values.Select(o => o.Price!.Value).ToList();
        Assert.Equal(3, prices.Count);
        Assert.Equal(100m, prices[0]);
        Assert.Equal(100.1m, prices[1]);
        Assert.Equal(100.2m, prices[2]);
        Assert.Equal(2, harness.Long.CancelledOrders.Count);
        Assert.True(harness.Executor.HasOpenPositions);
    }

    [Fact]
    public async Task Chase_falls_back_to_market_when_steps_are_exhausted()
    {
        var harness = Create(new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions
            {
                Mode = ChaseMode.Simulated,
                MaxSteps = 2,
                StepIntervalMs = 1,
                StepBps = 10m,
                MaxDeviationBps = 1_000m,
                FallbackToMarket = true,
            },
        });

        // limit-заявки не исполняются, market-добивка — да
        harness.Long.PlaceOrderOverride = (request, index) => request.Type == OrderType.Market
            ? OrderResult.Ok($"m{index}", 100.3m, request.Amount)
            : OrderResult.Ok($"o{index}", request.Price, 0m);

        await OpenAsync(harness);

        var orders = harness.Long.PlacedOrders.Values.ToList();

        // начальная заявка + 2 шага догонания + market
        Assert.Equal(4, orders.Count);
        Assert.Equal(new[] { 100m, 100.1m, 100.2m }, orders.Take(3).Select(o => o.Price!.Value));
        Assert.All(orders.Take(3), o => Assert.Equal(OrderType.Limit, o.Type));

        var fallback = orders[^1];
        Assert.Equal(OrderType.Market, fallback.Type);
        Assert.Null(fallback.Price);
        Assert.Equal(1m, fallback.Amount);  // добивается остаток ноги, а не исходный объём
        Assert.False(fallback.ReduceOnly);
        Assert.Equal(3, harness.Long.CancelledOrders.Count);
        Assert.True(harness.Executor.HasOpenPositions);
    }

    [Fact]
    public async Task Partial_fill_is_not_ordered_twice_by_chase_and_fallback()
    {
        var harness = Create(new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions { MaxSteps = 1, StepIntervalMs = 1, StepBps = 10m, MaxDeviationBps = 1_000m, FallbackToMarket = true },
        });

        // первая заявка набрала половину, вторая — ничего, market добирает остаток
        harness.Long.PlaceOrderOverride = (request, index) => request.Type switch
        {
            OrderType.Market => OrderResult.Ok($"m{index}", 100.2m, request.Amount),
            _ => index == 1 ? OrderResult.Ok($"o{index}", request.Price, 0.5m) : OrderResult.Ok($"o{index}", request.Price, 0m),
        };

        await OpenAsync(harness);

        var orders = harness.Long.PlacedOrders.Values.ToList();
        Assert.Equal(3, orders.Count);
        Assert.Equal(0.5m, orders[1].Amount);      // вторая заявка — только остаток
        Assert.Equal(100.1m, orders[1].Price);
        Assert.Equal(OrderType.Market, orders[^1].Type);
        Assert.Equal(0.5m, orders[^1].Amount);     // market добивает ненабранное
        Assert.True(harness.Executor.HasOpenPositions);
    }

    [Fact]
    public async Task Without_fallback_exhausted_chase_ends_in_rollback()
    {
        var harness = Create(new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions { MaxSteps = 1, StepIntervalMs = 1, StepBps = 10m, MaxDeviationBps = 1_000m, FallbackToMarket = false },
        }, timeoutMs: 3_000);

        harness.Long.PlaceOrderOverride = (_, index) => OrderResult.Ok($"o{index}", null, 0m);

        await OpenAsync(harness);

        Assert.Equal(2, harness.Long.PlacedOrders.Count);   // начальная заявка + один шаг
        Assert.False(harness.Executor.HasOpenPositions);    // объёма нет — позиция не регистрируется
        Assert.Equal(1, harness.Stats.Snapshot(Now.AddMinutes(1)).OrdersFailed);
    }

    [Fact]
    public async Task Chase_stops_when_next_step_would_break_deviation_budget()
    {
        var harness = Create(new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions { MaxSteps = 10, StepIntervalMs = 1, StepBps = 20m, MaxDeviationBps = 25m, FallbackToMarket = false },
        }, timeoutMs: 3_000);

        harness.Long.PlaceOrderOverride = (_, index) => OrderResult.Ok($"o{index}", null, 0m);

        await OpenAsync(harness);

        var orders = harness.Long.PlacedOrders.Values.ToList();

        // шаг 20 bps ещё в бюджете (25), шаг 40 bps — уже нет: перестановок больше не было
        Assert.Equal(2, orders.Count);
        Assert.Equal(100.2m, orders[1].Price);
    }

    [Fact]
    public async Task Native_chase_sends_a_single_order_with_exchange_params()
    {
        var harness = Create(new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions
            {
                Mode = ChaseMode.Native,
                MaxSteps = 4,
                StepIntervalMs = 1,
                Params = new Dictionary<string, string> { ["chaseType"] = "priceChase" },
            },
        }, longId: "kucoinfutures", timeoutMs: 1_500);

        harness.Long.PlaceOrderOverride = (request, index) => OrderResult.Ok($"o{index}", request.Price, 0m);

        await OpenAsync(harness, longId: "kucoinfutures");

        var order = Assert.Single(harness.Long.PlacedOrders.Values);
        Assert.Equal(OrderType.ChaseLimit, order.Type);
        Assert.Equal(100m, order.Price);
        Assert.Equal("priceChase", order.ExchangeParams!["chaseType"]);

        // догоняет биржа: локальных перестановок нет, по таймауту заявка просто снимается
        Assert.False(harness.Executor.HasOpenPositions);
    }

    [Fact]
    public async Task Unsupported_native_chase_is_downgraded_to_local_requotes()
    {
        // у binanceusdm нативного chase нет → исполнитель догоняет сам, нативные параметры не шлёт
        var harness = Create(new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions
            {
                Mode = ChaseMode.Native,
                MaxSteps = 2,
                StepIntervalMs = 1,
                StepBps = 10m,
                MaxDeviationBps = 1_000m,
                FallbackToMarket = false,
                Params = new Dictionary<string, string> { ["chaseType"] = "priceChase" },
            },
        }, timeoutMs: 3_000);

        harness.Long.PlaceOrderOverride = (request, index) => OrderResult.Ok($"o{index}", request.Price, 0m);

        await OpenAsync(harness);

        Assert.DoesNotContain(harness.Long.PlacedOrders.Values, o => o.ExchangeParams is not null);
        Assert.Equal(3, harness.Long.PlacedOrders.Count);   // начальная заявка + 2 шага

        // понижение зафиксировано в журнале — один раз, а не на каждой сделке
        Assert.Contains(harness.Log.Latest(50), e => e.Message.Contains("нативный chase") && e.Level == AppLogLevel.Warning);
    }

    [Fact]
    public async Task Close_follows_its_own_execution_policy()
    {
        var harness = Create(new OrderExecutionOptions
        {
            Type = OrderType.Limit,
            Close = new OrderExecutionOptions { Type = OrderType.Limit, LimitOffsetBps = 10m },
        });

        await OpenAsync(harness);

        // закрытие по Execution:Close — limit reduceOnly SELL по bid со смещением (100.4 − 10 bps)
        harness.Long.Tickers = new Dictionary<string, TickerSnapshot>
        {
            ["BTC/USDT:USDT"] = TestTickers.Make("binanceusdm", "BTC/USDT:USDT", bid: 100.4m, ask: 100.5m),
        };
        harness.Short.Tickers = new Dictionary<string, TickerSnapshot>
        {
            ["BTC/USDT:USDT"] = TestTickers.Make("bybit", "BTC/USDT:USDT", bid: 100.6m, ask: 100.7m),
        };

        await harness.Executor.ManageOpenPositionsAsync(
            new Dictionary<string, IReadOnlyDictionary<string, TickerSnapshot>>
            {
                ["binanceusdm"] = harness.Long.Tickers,
                ["bybit"] = harness.Short.Tickers,
            },
            CancellationToken.None);

        var longClose = harness.Long.PlacedOrders.Values.Last();
        Assert.Equal(OrderType.Limit, longClose.Type);
        Assert.True(longClose.ReduceOnly);
        Assert.Equal(100.2996m, longClose.Price);   // 100.4 − 10 bps: агрессивнее для SELL
        Assert.False(harness.Executor.HasOpenPositions);
    }

    [Fact]
    public async Task Per_exchange_override_replaces_only_that_leg()
    {
        var harness = Create(
            new OrderExecutionOptions { Type = OrderType.Limit },
            shortOverride: new OrderExecutionOptions { LimitOffsetBps = 30m, TimeInForce = TimeInForce.Ioc });

        await OpenAsync(harness);

        var longOrder = Assert.Single(harness.Long.PlacedOrders.Values);
        var shortOrder = Assert.Single(harness.Short.PlacedOrders.Values);

        Assert.Equal(100m, longOrder.Price);          // глобальный offset = 0
        Assert.Equal(TimeInForce.Gtc, longOrder.TimeInForce);
        Assert.Equal(100.697m, shortOrder.Price);     // переопределение bybit: −30 bps от 101
        Assert.Equal(TimeInForce.Ioc, shortOrder.TimeInForce);
    }
}
