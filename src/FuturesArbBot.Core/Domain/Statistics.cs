namespace FuturesArbBot.Core.Domain;

/// <summary>Событие журнала.</summary>
public sealed record LogEvent(
    long Id,
    DateTimeOffset Time,
    AppLogLevel Level,
    string Message);

/// <summary>Агрегированная статистика по одной бирже за сеанс.</summary>
public sealed record ExchangeRuntimeStat(
    string Id,
    string DisplayName,
    NetworkMode Mode,
    int Markets,
    long Requests,
    long Errors,
    double AvgLatencyMs,
    decimal TakerPercent);

/// <summary>Запись о закрытой арбитражной позиции.</summary>
public sealed record ClosedPositionRecord(
    string Symbol,
    string LongExchangeId,
    string ShortExchangeId,
    decimal Size,
    decimal EntryLong,
    decimal EntryShort,
    decimal? ExitLong,
    decimal? ExitShort,
    decimal PnlUsd,
    decimal FeesUsd,
    CloseReason Reason,
    DateTimeOffset OpenedAt,
    DateTimeOffset ClosedAt,
    bool Simulated);

/// <summary>Снимок состояния для дашборда (обновляется каждый тик сканера).</summary>
public sealed record DashboardSnapshot(
    DateTimeOffset UpdatedAt,
    int OnlineExchanges,
    int TrackedSymbols,
    IReadOnlyList<SpreadEstimate> Top,
    long ScanTicks,
    long Opportunities,
    long Errors,
    decimal BestNetEverPercent,
    string? BestSymbol,
    int OpenPositions,
    decimal RealizedPnlUsd,
    bool PnlSimulated,
    bool TradingEnabled);

/// <summary>Итоговый отчёт за сеанс (печатается при выходе по Ctrl+C).</summary>
public sealed record SessionReport
{
    public required NetworkMode Mode { get; init; }

    public required DateTimeOffset StartedAt { get; init; }

    public required DateTimeOffset EndedAt { get; init; }

    public TimeSpan Duration => EndedAt - StartedAt;

    public required long ScanTicks { get; init; }

    public required long TickersProcessed { get; init; }

    public required long OpportunitiesDetected { get; init; }

    public double OpportunitiesPerHour => Duration.TotalHours > 0.005 ? OpportunitiesDetected / Duration.TotalHours : OpportunitiesDetected;

    public required SpreadEstimate? BestEver { get; init; }

    public required long OrdersOpened { get; init; }

    public required long OrdersFailed { get; init; }

    public required IReadOnlyList<ClosedPositionRecord> ClosedPositions { get; init; }

    public required IReadOnlyList<PositionPair> StillOpen { get; init; }

    public decimal RealizedPnlUsd => ClosedPositions.Sum(p => p.PnlUsd);

    public decimal FeesPaidUsd => ClosedPositions.Sum(p => p.FeesUsd);

    public required IReadOnlyList<ExchangeRuntimeStat> Exchanges { get; init; }

    public required IReadOnlyList<KeyValuePair<string, int>> TopSymbols { get; init; }

    public required bool TradingWasEnabled { get; init; }
}
