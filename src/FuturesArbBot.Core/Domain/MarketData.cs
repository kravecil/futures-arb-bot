namespace FuturesArbBot.Core.Domain;

/// <summary>Комиссии биржи в процентах от оборота.</summary>
public readonly record struct ExchangeFees(decimal TakerPercent, decimal MakerPercent);

/// <summary>Описание торгового инструмента (линейный бессрочный фьючерс).</summary>
public sealed record MarketInfo(
    string Symbol,
    bool IsPerpetual,
    bool IsLinear,
    string QuoteCurrency,
    decimal? MinAmount,
    decimal TakerPercent,
    decimal MakerPercent);

/// <summary>Снимок тикера конкретной биржи на момент опроса.</summary>
public sealed record TickerSnapshot(
    string ExchangeId,
    string Symbol,
    decimal Bid,
    decimal Ask,
    decimal Last,
    decimal QuoteVolume24h,
    DateTimeOffset Timestamp)
{
    /// <summary>Средняя цена bid/ask.</summary>
    public decimal Mid => (Bid + Ask) / 2m;

    /// <summary>Котировки пригодны для расчёта спреда.</summary>
    public bool IsTradable => Bid > 0m && Ask > 0m;
}

/// <summary>Жизненный статус ордера на бирже.</summary>
public enum OrderStatus
{
    /// <summary>Ордер активен (в стакане или ожидает исполнения).</summary>
    Open,

    /// <summary>Полностью исполнен.</summary>
    Filled,

    /// <summary>Отменён.</summary>
    Canceled,

    /// <summary>Срок действия истёк.</summary>
    Expired,

    /// <summary>Отклонён биржей.</summary>
    Rejected,

    /// <summary>Статус не распознан — трактовать как «ещё жив».</summary>
    Unknown,
}

/// <summary>Снимок состояния ордера для опроса исполнения лимитных заявок.</summary>
public sealed record OrderUpdate(
    string OrderId,
    OrderStatus Status,
    decimal FilledAmount,
    decimal? AveragePrice)
{
    /// <summary>Ордер больше не может исполниться (отменён/истёк/отклонён).</summary>
    public bool IsDead => Status is OrderStatus.Canceled or OrderStatus.Expired or OrderStatus.Rejected;

    /// <summary>Ордер полностью исполнен.</summary>
    public bool IsFilled => Status == OrderStatus.Filled;
}

/// <summary>
/// Запрос на ордер. Для лимитных типов (Limit, ChaseLimit) обязана быть задана цена.
/// TimeInForce и ExchangeParams заполняются политикой исполнения (<see cref="OrderPolicy"/>)
/// транслируются в параметры CCXT через <see cref="OrderParamsBuilder"/>.
/// </summary>
public sealed record OrderRequest(
    string Symbol,
    OrderSide Side,
    decimal Amount,
    bool ReduceOnly = false,
    OrderType Type = OrderType.Market,
    decimal? Price = null,
    TimeInForce TimeInForce = TimeInForce.Gtc,
    IReadOnlyDictionary<string, string>? ExchangeParams = null);

/// <summary>Результат исполнения ордера.</summary>
public sealed record OrderResult
{
    public required bool Success { get; init; }

    public string? OrderId { get; init; }

    /// <summary>Средняя цена исполнения (если известна бирже).</summary>
    public decimal? AveragePrice { get; init; }

    public decimal FilledAmount { get; init; }

    public string? Error { get; init; }

    public static OrderResult Ok(string? orderId, decimal? averagePrice, decimal filled) => new()
    {
        Success = true,
        OrderId = orderId,
        AveragePrice = averagePrice,
        FilledAmount = filled,
    };

    public static OrderResult Fail(string error) => new()
    {
        Success = false,
        Error = error,
    };
}

/// <summary>Результат опроса тикеров биржи.</summary>
public sealed record FetchTickersResult(
    IReadOnlyDictionary<string, TickerSnapshot> Tickers,
    TimeSpan Latency);
