using FuturesArbBot.Core.Abstractions;
using FuturesArbBot.Core.Domain;
using FuturesArbBot.Core.Engine;

namespace FuturesArbBot.Tests;

/// <summary>Тестовый конфиг-провайдер без файловой системы.</summary>
public sealed class FakeConfigProvider(BotOptions options) : IConfigProvider
{
    public FakeConfigProvider()
        : this(new BotOptions())
    {
    }

    public BotOptions Current => options;

    public string ConfigDirectory => "test";
}

/// <summary>Фейковая биржа с настраиваемыми котировками и комиссиями.</summary>
public sealed class FakeConnector : IExchangeConnector
{
    public FakeConnector(string id, decimal takerPercent = 0.1m)
    {
        Id = id;
        DisplayName = $"Fake {id}";
        _markets =
        [
            new MarketInfo("BTC/USDT:USDT", true, true, "USDT", 0.0001m, takerPercent, 0.02m),
            new MarketInfo("ETH/USDT:USDT", true, true, "USDT", 0.001m, takerPercent, 0.02m),
        ];
        _fees = new ExchangeFees(takerPercent, 0.02m);
        _defaultFees = _fees;
    }

    public string Id { get; }

    public string DisplayName { get; }

    public NetworkMode Mode => NetworkMode.DryRun;

    public ExchangeFees DefaultFees => _defaultFees;

    public int MarketCount => _markets.Count;

    public IReadOnlyList<MarketInfo> PerpetualMarkets => _markets;

    public Dictionary<string, TickerSnapshot> Tickers { get; set; } = [];

    public bool Connected { get; private set; }

    private readonly List<MarketInfo> _markets;
    private readonly ExchangeFees _fees;
    private readonly ExchangeFees _defaultFees;

    public Task ConnectAsync(CancellationToken ct = default)
    {
        Connected = true;
        return Task.CompletedTask;
    }

    public Task<FetchTickersResult> FetchTickersAsync(CancellationToken ct = default) =>
        Task.FromResult(new FetchTickersResult(Tickers, TimeSpan.FromMilliseconds(12)));

    /// <summary>Все ордера, выставленные через фейк: orderId → запрос.</summary>
    public Dictionary<string, OrderRequest> PlacedOrders { get; } = new(StringComparer.Ordinal);

    /// <summary>Состояния фейковых ордеров: orderId → снимок (тесты могут менять до опроса).</summary>
    public Dictionary<string, OrderUpdate> OrderStates { get; } = new(StringComparer.Ordinal);

    /// <summary>Переопределение поведения размещения: вернуть Fail/Ok — тестовая проводка сбоев.</summary>
    public Func<OrderRequest, int, OrderResult>? PlaceOrderOverride { get; set; }

    /// <summary>Если true — фейковый limit исполняется «наполовину» при первом опросе статуса.</summary>
    public bool LimitPartiallyFills { get; set; }

    private int _orderCounter;

    public Task<OrderResult> PlaceOrderAsync(OrderRequest request, CancellationToken ct = default)
    {
        var index = Interlocked.Increment(ref _orderCounter);
        PlacedOrders[$"fake-order-{index}"] = request;

        var result = PlaceOrderOverride?.Invoke(request, index)
                     ?? OrderResult.Ok($"fake-order-{index}", request.Price, request.Amount);

        if (result.Success && result.OrderId is not null)
        {
            var fullyFilled = result.FilledAmount >= request.Amount;
            OrderStates[result.OrderId] = new OrderUpdate(
                result.OrderId,
                fullyFilled ? OrderStatus.Filled : OrderStatus.Open,
                result.FilledAmount,
                result.AveragePrice);
        }

        return Task.FromResult(result);
    }

    public Task<OrderUpdate?> FetchOrderAsync(string orderId, string symbol, CancellationToken ct = default)
    {
        if (!OrderStates.TryGetValue(orderId, out var state))
        {
            return Task.FromResult<OrderUpdate?>(null);
        }

        if (state.Status == OrderStatus.Open && LimitPartiallyFills && PlacedOrders.TryGetValue(orderId, out var request))
        {
            var half = request.Amount / 2m;
            state = new OrderUpdate(orderId, OrderStatus.Open, half, request.Price);
            OrderStates[orderId] = state;
        }

        return Task.FromResult<OrderUpdate?>(state);
    }

    public Task<bool> CancelOrderAsync(string orderId, string symbol, CancellationToken ct = default)
    {
        if (!OrderStates.TryGetValue(orderId, out var state) || state.Status != OrderStatus.Open)
        {
            return Task.FromResult(false);
        }

        OrderStates[orderId] = state with { Status = OrderStatus.Canceled };
        return Task.FromResult(true);
    }

    public Task SetLeverageAsync(int leverage, string symbol, CancellationToken ct = default) => Task.CompletedTask;

    public Task<bool> VerifyAccessAsync(CancellationToken ct = default) => Task.FromResult(true);

    public bool TryGetFees(string symbol, out ExchangeFees fees)
    {
        fees = _fees;
        return true;
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

/// <summary>Тикер-строитель для тестов.</summary>
public static class TestTickers
{
    public static TickerSnapshot Make(string exchange, string symbol, decimal bid, decimal ask, decimal volume = 10_000_000m, DateTimeOffset? ts = null) => new(
        exchange,
        symbol,
        bid,
        ask,
        (bid + ask) / 2m,
        volume,
        ts ?? new DateTimeOffset(2026, 1, 1, 0, 0, 0, TimeSpan.Zero));
}
