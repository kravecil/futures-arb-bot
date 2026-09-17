using System.Collections.Concurrent;
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

    /// <summary>Фандинг-рейты «биржи»: symbol → ставка в % (пусто — данных нет).</summary>
    public Dictionary<string, decimal> FundingRatesPercent { get; set; } = [];

    /// <summary>Если true — запрос фандинг-рейтов падает (имитация недоступного эндпоинта).</summary>
    public bool FailFetchFundingRates { get; set; }

    public Task<IReadOnlyDictionary<string, decimal>> FetchFundingRatesPercentAsync(CancellationToken ct = default)
    {
        if (FailFetchFundingRates)
        {
            throw new InvalidOperationException("фандинг-рейты недоступны");
        }

        return Task.FromResult<IReadOnlyDictionary<string, decimal>>(FundingRatesPercent);
    }

    /// <summary>Все ордера, выставленные через фейк: orderId → запрос.</summary>
    public Dictionary<string, OrderRequest> PlacedOrders { get; } = new(StringComparer.Ordinal);

    /// <summary>Состояния фейковых ордеров: orderId → снимок (тесты могут менять до опроса).</summary>
    public Dictionary<string, OrderUpdate> OrderStates { get; } = new(StringComparer.Ordinal);

    /// <summary>Переопределение поведения размещения: вернуть Fail/Ok — тестовая проводка сбоев; null — дефолт (полное исполнение).</summary>
    public Func<OrderRequest, int, OrderResult?>? PlaceOrderOverride { get; set; }

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

    /// <summary>Отменённые через фейк заявки (orderId).</summary>
    public List<string> CancelledOrders { get; } = [];

    /// <summary>
    /// Перекрыть поведение отмены: false — отмена «не проходит», заявка остаётся в стакане;
    /// null/true — дефолтное поведение фейка.
    /// </summary>
    public Func<string, bool>? CancelOrderOverride { get; set; }

    /// <summary>Фактические позиции «биржи» для тестов сверки лимитов (пусто — внешних позиций нет).</summary>
    public List<PositionSnapshot> OpenPositions { get; } = [];

    /// <summary>Если true — запрос списка позиций падает (имитация недоступной сверки).</summary>
    public bool FailFetchPositions { get; set; }

    public Task<IReadOnlyList<PositionSnapshot>> FetchPositionsAsync(CancellationToken ct = default)
    {
        if (FailFetchPositions)
        {
            throw new InvalidOperationException("сверка позиций недоступна");
        }

        return Task.FromResult<IReadOnlyList<PositionSnapshot>>([.. OpenPositions]);
    }

    public Task<bool> CancelOrderAsync(string orderId, string symbol, CancellationToken ct = default)
    {
        if (CancelOrderOverride?.Invoke(orderId) == false)
        {
            return Task.FromResult(false);
        }

        if (!OrderStates.TryGetValue(orderId, out var state) || state.Status != OrderStatus.Open)
        {
            return Task.FromResult(false);
        }

        OrderStates[orderId] = state with { Status = OrderStatus.Canceled };
        CancelledOrders.Add(orderId);
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

/// <summary>Отправитель-регистратор: собирает сигналы, переданные в очередь уведомлений.</summary>
public sealed class FakeNotifier : ISpreadNotifier
{
    /// <summary>Все сигналы, поставленные в очередь (по одному вызову Notify).</summary>
    public List<SpreadEstimate> Notified { get; } = [];

    public void Notify(SpreadEstimate estimate) => Notified.Add(estimate);
}

/// <summary>
/// Транспорт уведомлений: вместо HTTP записывает тексты и отдаёт заготовленные исходы.
/// i-я отправка получает <see cref="Outcomes"/>[i] (последний элемент повторяется), null = успешно.
/// </summary>
public sealed class FakeTransport : INotificationTransport
{
    private readonly ConcurrentQueue<string> _sent = new();
    private int _calls;

    /// <summary>Исходы по порядку вызовов; null в списке = доставка успешна.</summary>
    public List<NotificationDelivery?> Outcomes { get; } = [];

    /// <summary>Задан — бросается при отправке (имитация падения транспорта).</summary>
    public Exception? ThrowOnSend { get; set; }

    /// <summary>Задан — ожидание перед возвратом (имитация долгой сети).</summary>
    public TimeSpan Delay { get; set; }

    /// <summary>Снимок отправленных текстов (копия — очередь живёт в фоне).</summary>
    public IReadOnlyList<string> Sent => [.. _sent];

    /// <summary>Сколько раз обратились к транспорту.</summary>
    public int CallCount => Volatile.Read(ref _calls);

    public async Task<NotificationDelivery> SendAsync(string text, CancellationToken ct = default)
    {
        var index = Interlocked.Increment(ref _calls) - 1;
        _sent.Enqueue(text);

        if (ThrowOnSend is { } fault)
        {
            throw fault;
        }

        if (Delay > TimeSpan.Zero)
        {
            await Task.Delay(Delay, ct);
        }

        if (Outcomes.Count == 0)
        {
            return NotificationDelivery.Delivered;
        }

        return Outcomes[Math.Min(index, Outcomes.Count - 1)] ?? NotificationDelivery.Delivered;
    }
}

/// <summary>Исполнитель-регистратор: вместо торговли фиксирует полученные кандидаты.</summary>
public sealed class FakeExecutor : ITradeExecutor
{
    /// <summary>Все списки кандидатов, переданные в ProcessOpportunitiesAsync.</summary>
    public List<List<SpreadEstimate>> ProcessedBatches { get; } = [];

    /// <summary>Все кандидаты всех batches одним списком.</summary>
    public List<SpreadEstimate> Processed => [.. ProcessedBatches.SelectMany(b => b)];

    public bool HasOpenPositions => false;

    public Task ProcessOpportunitiesAsync(IReadOnlyList<SpreadEstimate> candidates, CancellationToken ct)
    {
        ProcessedBatches.Add([.. candidates]);
        return Task.CompletedTask;
    }

    public Task ManageOpenPositionsAsync(IReadOnlyDictionary<string, IReadOnlyDictionary<string, TickerSnapshot>> tickersByExchange, CancellationToken ct) =>
        Task.CompletedTask;

    public Task CloseAllAsync(CloseReason reason, CancellationToken ct) => Task.CompletedTask;
}

/// <summary>Часы, которые тест сдвигает вручную (кулдаун, окно лимита в минуту).</summary>
public sealed class ManualTimeProvider(DateTimeOffset start) : TimeProvider
{
    private DateTimeOffset _now = start;

    public override DateTimeOffset GetUtcNow() => _now;

    public void Advance(TimeSpan by) => _now += by;
}

/// <summary>Часы, шагающие вперёд при каждом чтении — чтобы циклы ожидания не зависали.</summary>
public sealed class SteppingTimeProvider(DateTimeOffset start, TimeSpan step) : TimeProvider
{
    private long _reads;

    public override DateTimeOffset GetUtcNow()
    {
        var n = Interlocked.Increment(ref _reads) - 1;
        return start + TimeSpan.FromTicks(step.Ticks * n);
    }
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
