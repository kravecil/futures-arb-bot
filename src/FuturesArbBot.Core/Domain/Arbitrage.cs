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

    /// <summary>Размер в базовой валюте (монетах).</summary>
    public required decimal Size { get; set; }

    public required decimal EntryLong { get; set; }

    public required decimal EntryShort { get; set; }

    public decimal FeesEntryUsd { get; set; }

    public required DateTimeOffset OpenedAt { get; init; }

    public PositionStatus Status { get; set; } = PositionStatus.Open;

    public decimal? ExitLong { get; set; }

    public decimal? ExitShort { get; set; }

    public decimal FeesExitUsd { get; set; }

    public DateTimeOffset? ClosedAt { get; set; }

    public CloseReason? Reason { get; set; }

    public decimal? RealizedPnlUsd { get; set; }

    /// <summary>Позиция создана в режиме симуляции.</summary>
    public bool Simulated { get; init; }
}
