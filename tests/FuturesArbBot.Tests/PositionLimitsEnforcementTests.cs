using FuturesArbBot.Core.Domain;
using FuturesArbBot.Core.Engine;
using Microsoft.Extensions.Logging.Abstractions;

namespace FuturesArbBot.Tests;

/// <summary>
/// Регрессия принуждения лимитов позиций: MaxOpenPositions/MaxPositionsPerExchange
/// считаются по фактической экспозиции бирж (сверка через FetchPositionsAsync), а не только
/// по памяти сеанса; market-закрытие без подтверждения биржи не снимает позицию с учёта;
/// заявка, не отменённая при откате открытия, не теряется, а доснимается циклом ведения.
/// </summary>
public class PositionLimitsEnforcementTests
{
    private const string Btc = "BTC/USDT:USDT";
    private const string Eth = "ETH/USDT:USDT";

    private static readonly DateTimeOffset Now = new(2026, 1, 1, 12, 0, 0, TimeSpan.Zero);

    private static (ArbTradeExecutor Executor, FakeConnector Long, FakeConnector Short, StatisticsCollector Stats) CreateTestnet(
        int maxOpenPositions = 4,
        int maxPositionsPerExchange = 4)
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
        config.Current.Arbitrage.MaxOpenPositions = maxOpenPositions;
        config.Current.Arbitrage.MaxPositionsPerExchange = maxPositionsPerExchange;

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

    // ------------------------- item 1: лимиты внутри процесса -------------------------

    [Fact]
    public async Task MaxOpenPositions_1_blocks_the_second_arbitrage()
    {
        var (executor, longConnector, shortConnector, _) = CreateTestnet(maxOpenPositions: 1);

        await executor.ProcessOpportunitiesAsync([Estimate(Btc, 100m, 101m), Estimate(Eth, 50m, 51m)], CancellationToken.None);

        Assert.True(executor.HasOpenPositions);
        Assert.Single(longConnector.PlacedOrders);  // открыт только первый кандидат
        Assert.Single(shortConnector.PlacedOrders);
    }

    [Fact]
    public async Task MaxPositionsPerExchange_1_blocks_the_second_arbitrage_on_the_same_exchange()
    {
        var (executor, longConnector, shortConnector, _) = CreateTestnet(maxOpenPositions: 4, maxPositionsPerExchange: 1);

        await executor.ProcessOpportunitiesAsync([Estimate(Btc, 100m, 101m), Estimate(Eth, 50m, 51m)], CancellationToken.None);

        // обе биржи участвуют в каждом арбитраже: после BTC слоты исчерпаны
        Assert.Single(longConnector.PlacedOrders);
        Assert.Single(shortConnector.PlacedOrders);
    }

    // ------------------------- item 2: сверка с биржей -------------------------

    [Fact]
    public async Task Positions_left_on_the_exchanges_after_restart_consume_MaxOpenPositions()
    {
        var (executor, longConnector, shortConnector, _) = CreateTestnet(maxOpenPositions: 1);

        // «прошлый сеанс» оставил ETH-арбитраж на обеих биржах — этот процесс о нём не знает
        longConnector.OpenPositions.Add(new PositionSnapshot(Eth, OrderSide.Buy, 2m, 50m));
        shortConnector.OpenPositions.Add(new PositionSnapshot(Eth, OrderSide.Sell, 2m, 51m));

        await executor.ProcessOpportunitiesAsync([Estimate(Btc, 100m, 101m)], CancellationToken.None);

        // лимит 1 уже занят чужой парой позиций — нового арбитража быть не должно
        Assert.False(executor.HasOpenPositions);
        Assert.Empty(longConnector.PlacedOrders);
        Assert.Empty(shortConnector.PlacedOrders);
    }

    [Fact]
    public async Task Foreign_leg_on_one_exchange_consumes_its_MaxPositionsPerExchange_slot()
    {
        var (executor, longConnector, shortConnector, _) = CreateTestnet(maxOpenPositions: 4, maxPositionsPerExchange: 1);

        // на bybit живёт «чужой» шорт: арбитража (2+ биржи) нет, но слот биржи занят
        shortConnector.OpenPositions.Add(new PositionSnapshot(Eth, OrderSide.Sell, 2m, 51m));

        await executor.ProcessOpportunitiesAsync([Estimate(Btc, 100m, 101m)], CancellationToken.None);

        Assert.Empty(longConnector.PlacedOrders);
        Assert.Empty(shortConnector.PlacedOrders);
    }

    [Fact]
    public async Task Session_positions_are_not_double_counted_against_exchange_snapshot()
    {
        var (executor, longConnector, shortConnector, _) = CreateTestnet(maxOpenPositions: 4, maxPositionsPerExchange: 2);

        // биржи на запросе сверки отдают нашу же BTC-позицию — вторым слотом она не считается
        longConnector.OpenPositions.Add(new PositionSnapshot(Btc, OrderSide.Buy, 1m, 100m));
        shortConnector.OpenPositions.Add(new PositionSnapshot(Btc, OrderSide.Sell, 1m, 101m));

        await executor.ProcessOpportunitiesAsync([Estimate(Btc, 100m, 101m)], CancellationToken.None);
        Assert.True(executor.HasOpenPositions);

        await executor.ProcessOpportunitiesAsync([Estimate(Eth, 50m, 51m)], CancellationToken.None);

        // ETH открылся: 1 сеансовая позиция + 0 внешних (BTC — наша же, дедуплицирована)
        Assert.Equal(2, longConnector.PlacedOrders.Count);
        Assert.Equal(2, shortConnector.PlacedOrders.Count);
    }

    [Fact]
    public async Task Reconcile_failure_does_not_block_trading()
    {
        var (executor, longConnector, shortConnector, _) = CreateTestnet(maxOpenPositions: 1);
        longConnector.FailFetchPositions = true;
        shortConnector.FailFetchPositions = true;

        await executor.ProcessOpportunitiesAsync([Estimate(Btc, 100m, 101m)], CancellationToken.None);

        // сверка упала — работаем по позициям сеанса, торговля не встала
        Assert.True(executor.HasOpenPositions);
        Assert.Single(longConnector.PlacedOrders);
    }

    // ------------------------- item 3: честное market-закрытие -------------------------

    [Fact]
    public async Task Market_close_without_confirmation_keeps_position_in_tracking()
    {
        var (executor, longConnector, shortConnector, stats) = CreateTestnet(maxOpenPositions: 1);

        await executor.ProcessOpportunitiesAsync([Estimate(Btc, 100m, 101m)], CancellationToken.None);
        Assert.True(executor.HasOpenPositions);

        // стоп-лосс: спред расширился выше порога
        longConnector.Tickers = new Dictionary<string, TickerSnapshot>
        {
            [Btc] = TestTickers.Make("binanceusdm", Btc, bid: 104.9m, ask: 105m),
        };
        shortConnector.Tickers = new Dictionary<string, TickerSnapshot>
        {
            [Btc] = TestTickers.Make("bybit", Btc, bid: 111m, ask: 111.2m),
        };

        // закрытие лонга «принято, но без объёма», и на бирже лонг по-прежнему живёт
        longConnector.PlaceOrderOverride = (request, index) => request.ReduceOnly
            ? OrderResult.Ok($"unfilled-{index}", null, 0m)
            : null;
        longConnector.OpenPositions.Add(new PositionSnapshot(Btc, OrderSide.Buy, 1m, 100m));

        await executor.ManageOpenPositionsAsync(Tickers(longConnector, shortConnector), CancellationToken.None);

        // прежнее поведение считало такую ногу закрытой целиком и снимало позицию с учёта,
        // освобождая слот лимита при живой экспозиции на бирже
        Assert.True(executor.HasOpenPositions);
        Assert.Empty(stats.Snapshot(Now.AddMinutes(1)).ClosedPositions);

        // биржа подтвердила отсутствие позиции — закрытие довершается на следующем тике
        longConnector.OpenPositions.Clear();
        await executor.ManageOpenPositionsAsync(Tickers(longConnector, shortConnector), CancellationToken.None);

        Assert.False(executor.HasOpenPositions);
        var trade = Assert.Single(stats.Snapshot(Now.AddMinutes(1)).ClosedPositions);
        Assert.Equal(CloseReason.StopLoss, trade.Reason);
    }

    // ------------------------- item 4: неотменённые заявки отката -------------------------

    [Fact]
    public async Task Uncancelled_leg_is_not_forgotten_and_keeps_being_cancelled()
    {
        var (executor, longConnector, shortConnector, _) = CreateTestnet();

        // лонг выставлен и живёт в стакане, шорт отклонён → откат открытия,
        // но отмена лонга первые попытки не проходит
        longConnector.PlaceOrderOverride = (request, index) => request.ReduceOnly
            ? null
            : OrderResult.Ok($"stuck-{index}", request.Price, 0m);
        shortConnector.PlaceOrderOverride = (_, _) => OrderResult.Fail("нет ликвидности");
        longConnector.CancelOrderOverride = _ => false;

        await executor.ProcessOpportunitiesAsync([Estimate(Btc, 100m, 101m)], CancellationToken.None);

        // позиция не открыта — но и заявка не «забыта»: она всё ещё в стакане
        Assert.False(executor.HasOpenPositions);
        Assert.Equal(OrderStatus.Open, longConnector.OrderStates["stuck-1"].Status);
        Assert.Empty(longConnector.CancelledOrders);

        var tickers = Tickers(longConnector, shortConnector);
        await executor.ManageOpenPositionsAsync(tickers, CancellationToken.None);
        await executor.ManageOpenPositionsAsync(tickers, CancellationToken.None);

        // цикл ведения не бросает заявку: пока отмена не проходит, она помнится и опрашивается
        Assert.Equal(OrderStatus.Open, longConnector.OrderStates["stuck-1"].Status);
        Assert.Empty(longConnector.CancelledOrders);

        // отмена прошла — заявка снята, цикл ведения её отпускает
        longConnector.CancelOrderOverride = null;
        await executor.ManageOpenPositionsAsync(tickers, CancellationToken.None);

        Assert.Equal(OrderStatus.Canceled, longConnector.OrderStates["stuck-1"].Status);
        Assert.Single(longConnector.CancelledOrders);
    }
}
