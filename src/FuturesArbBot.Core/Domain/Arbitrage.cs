namespace FuturesArbBot.Core.Domain;

/// <summary>Одна «нога» арбитражной позиции.</summary>
public sealed record OpportunityLeg(
    string ExchangeId,
    decimal Price,
    decimal FeePercent);

/// <summary>
/// Оценка арбитражной возможности: купить дешевле на одной бирже и продать дороже
/// на другой, обе ноги — линейные бессрочные фьючерсы.
/// </summary>
public sealed record SpreadEstimate(
    string Symbol,
    OpportunityLeg LongLeg,
    OpportunityLeg ShortLeg,
    decimal GrossPercent,
    decimal NetPercent,
    decimal QuoteVolumeUsd,
    DateTimeOffset DetectedAt);

/// <summary>Пара встречных фьючерсных позиций (лонг + шорт), дельта-нейтральная связка.</summary>
public sealed class PositionPair
{
    public required Guid Id { get; init; }

    public required string Symbol { get; init; }

    public required string LongExchangeId { get; init; }

    public required string ShortExchangeId { get; init; }

    /// <summary>Фактически набранный объём лонга в базовой валюте (монетах).</summary>
    public required decimal LongSize { get; set; }

    /// <summary>Фактически набранный объём шорта в базовой валюте (монетах).</summary>
    public required decimal ShortSize { get; set; }

    /// <summary>Согласованный (встречный) объём позиции — минимум из двух ног.</summary>
    public decimal MatchedSize => Math.Min(LongSize, ShortSize);

    /// <summary>Перекос ног: положителен — больше лонг, отрицателен — больше шорт.</summary>
    public decimal Imbalance => LongSize - ShortSize;

    /// <summary>Суммарно закрыто (закрытыми ногами/урезано) по лонгу за время жизни позиции.</summary>
    public decimal ClosedLongVolume { get; set; }

    /// <summary>Суммарно закрыто (закрытыми ногами/урезано) по шорту за время жизни позиции.</summary>
    public decimal ClosedShortVolume { get; set; }

    /// <summary>Закрытый встречный объём позиции — для отчётов после обнуления ног.</summary>
    public decimal ClosedMatchedVolume => Math.Min(ClosedLongVolume, ClosedShortVolume);

    public required decimal EntryLong { get; set; }

    public required decimal EntryShort { get; set; }

    public decimal FeesEntryUsd { get; set; }

    public required DateTimeOffset OpenedAt { get; init; }

    public PositionStatus Status { get; set; } = PositionStatus.Open;

    /// <summary>Лонговая нога закрыта (промежуточное состояние многошагового закрытия).</summary>
    public bool LongClosed { get; set; }

    /// <summary>Шортовая нога закрыта (промежуточное состояние многошагового закрытия).</summary>
    public bool ShortClosed { get; set; }

    public decimal? ExitLong { get; set; }

    public decimal? ExitShort { get; set; }

    public decimal FeesExitUsd { get; set; }

    public DateTimeOffset? ClosedAt { get; set; }

    public CloseReason? Reason { get; set; }

    public decimal? RealizedPnlUsd { get; set; }

    /// <summary>Позиция создана в режиме симуляции.</summary>
    public bool Simulated { get; init; }
}
