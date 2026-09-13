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

/// <summary>Запрос на рыночный ордер.</summary>
public sealed record OrderRequest(
    string Symbol,
    OrderSide Side,
    decimal Amount,
    bool ReduceOnly = false);

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
