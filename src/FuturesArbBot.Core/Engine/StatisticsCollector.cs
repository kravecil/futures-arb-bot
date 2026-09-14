namespace FuturesArbBot.Core.Engine;

/// <summary>Накопитель статистики по одной бирже.</summary>
internal sealed class ExchangeAccumulator
{
    public string DisplayName { get; set; } = string.Empty;

    public NetworkMode Mode { get; set; }

    public int Markets { get; set; }

    public decimal TakerPercent { get; set; }

    public long Requests { get; set; }

    public long Errors { get; set; }

    public double LatencySumMs { get; set; }

    public double AvgLatencyMs => Requests > 0 ? LatencySumMs / Requests : 0.0;
}

/// <summary>
/// Потокобезопасный сборщик статистики сеанса: счётчики через Interlocked,
/// агрегаты под <see cref="Lock"/> (C# 13+: System.Threading.Lock).
/// </summary>
public sealed class StatisticsCollector(IConfigProvider config, TimeProvider time) : IStatisticsCollector
{
    private readonly DateTimeOffset _startedAt = time.GetUtcNow();

    private long _scanTicks;
    private long _tickersProcessed;
    private long _opportunities;
    private long _ordersOpened;
    private long _ordersFailed;

    private readonly Lock _gate = new();
    private readonly Dictionary<string, ExchangeAccumulator> _exchanges = new(StringComparer.Ordinal);
    private readonly Dictionary<string, int> _symbolHits = new(StringComparer.Ordinal);
    private readonly List<ClosedPositionRecord> _closed = [];
    private readonly List<PositionPair> _open = [];
    private SpreadEstimate? _bestEver;

    public void RecordScanTick(int tickersFetched)
    {
        Interlocked.Increment(ref _scanTicks);
        Interlocked.Add(ref _tickersProcessed, tickersFetched);
    }

    public void RecordOpportunity(SpreadEstimate estimate)
    {
        Interlocked.Increment(ref _opportunities);

        lock (_gate)
        {
            _symbolHits[estimate.Symbol] = _symbolHits.GetValueOrDefault(estimate.Symbol) + 1;
            if (_bestEver is null || estimate.NetPercent > _bestEver.NetPercent)
            {
                _bestEver = estimate;
            }
        }
    }

    public void RecordRequest(string exchangeId, TimeSpan latency, bool success)
    {
        lock (_gate)
        {
            var acc = Accumulator(exchangeId);
            acc.Requests++;
            acc.LatencySumMs += latency.TotalMilliseconds;
            if (!success)
            {
                acc.Errors++;
            }
        }
    }

    public void RecordMarkets(string exchangeId, string displayName, int count, ExchangeFees fees, NetworkMode mode)
    {
        lock (_gate)
        {
            var acc = Accumulator(exchangeId);
            acc.DisplayName = displayName;
            acc.Markets = count;
            acc.TakerPercent = fees.TakerPercent;
            acc.Mode = mode;
        }
    }

    public void RecordOpened(PositionPair position)
    {
        Interlocked.Increment(ref _ordersOpened);

        lock (_gate)
        {
            _open.Add(position);
        }
    }

    public void RecordOpenFailed(string symbol, string errorMessage)
    {
        Interlocked.Increment(ref _ordersFailed);
    }

    public void RecordClosed(PositionPair position)
    {
        var record = new ClosedPositionRecord(
            position.Symbol,
            position.LongExchangeId,
            position.ShortExchangeId,
            position.ClosedMatchedVolume,
            position.EntryLong,
            position.EntryShort,
            position.ExitLong,
            position.ExitShort,
            position.RealizedPnlUsd ?? 0m,
            position.FeesEntryUsd + position.FeesExitUsd,
            position.Reason ?? CloseReason.SessionEnd,
            position.OpenedAt,
            position.ClosedAt ?? time.GetUtcNow(),
            position.Simulated);

        lock (_gate)
        {
            _open.Remove(position);
            _closed.Add(record);
        }
    }

    public SessionReport Snapshot(DateTimeOffset endedAt)
    {
        lock (_gate)
        {
            return new SessionReport
            {
                Mode = config.Current.General.NetworkMode,
                StartedAt = _startedAt,
                EndedAt = endedAt,
                ScanTicks = Interlocked.Read(ref _scanTicks),
                TickersProcessed = Interlocked.Read(ref _tickersProcessed),
                OpportunitiesDetected = Interlocked.Read(ref _opportunities),
                BestEver = _bestEver,
                OrdersOpened = Interlocked.Read(ref _ordersOpened),
                OrdersFailed = Interlocked.Read(ref _ordersFailed),
                ClosedPositions = [.. _closed],
                StillOpen = [.. _open],
                Exchanges = [.. _exchanges.Select(kv => new ExchangeRuntimeStat(
                    kv.Key,
                    kv.Value.DisplayName,
                    kv.Value.Mode,
                    kv.Value.Markets,
                    kv.Value.Requests,
                    kv.Value.Errors,
                    Math.Round(kv.Value.AvgLatencyMs, 1),
                    kv.Value.TakerPercent)).OrderBy(s => s.Id, StringComparer.OrdinalIgnoreCase)],
                TopSymbols = [.. _symbolHits.OrderByDescending(kv => kv.Value).ThenBy(kv => kv.Key, StringComparer.Ordinal).Take(10)],
                TradingWasEnabled = config.Current.Arbitrage.Enabled,
            };
        }
    }

    public DashboardSnapshot BuildDashboard(IReadOnlyList<SpreadEstimate> top, int onlineExchanges, int trackedSymbols)
    {
        long ticks, opportunities;
        lock (_gate)
        {
            ticks = Interlocked.Read(ref _scanTicks);
            opportunities = Interlocked.Read(ref _opportunities);
        }

        var errors = _exchanges.Values.Sum(a => a.Errors);
        var (pnl, openCount) = CurrentPnl();
        var best = _bestEver;

        return new DashboardSnapshot(
            time.GetUtcNow(),
            onlineExchanges,
            trackedSymbols,
            top,
            ticks,
            opportunities,
            errors,
            best?.NetPercent ?? 0m,
            best?.Symbol,
            openCount,
            pnl,
            config.Current.General.NetworkMode == NetworkMode.DryRun,
            config.Current.Arbitrage.Enabled);
    }

    private (decimal Pnl, int OpenCount) CurrentPnl()
    {
        lock (_gate)
        {
            return (_closed.Sum(p => p.PnlUsd), _open.Count);
        }
    }

    private ExchangeAccumulator Accumulator(string exchangeId)
    {
        if (!_exchanges.TryGetValue(exchangeId, out var acc))
        {
            acc = new ExchangeAccumulator { DisplayName = exchangeId };
            _exchanges[exchangeId] = acc;
        }

        return acc;
    }
}
