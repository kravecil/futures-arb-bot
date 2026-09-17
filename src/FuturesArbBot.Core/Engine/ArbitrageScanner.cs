namespace FuturesArbBot.Core.Engine;

/// <summary>
/// Сердце робота: опрашивает все включённые биржи, строит «книгу» котировок
/// по символам, ищет межбиржевые расхождения и передаёт сигналы исполнителю.
/// </summary>
public sealed class ArbitrageScanner(
    IConfigProvider config,
    ConnectorRegistry registry,
    ISpreadCalculator calculator,
    ISymbolFilter filter,
    ITradeExecutor executor,
    IStatisticsCollector stats,
    IEventLog log,
    TimeProvider time,
    ILogger<ArbitrageScanner> logger) : IArbitrageScanner
{
    private Dictionary<string, IExchangeConnector> _connectors = new(StringComparer.Ordinal);
    private Dictionary<string, HashSet<string>> _allowedSymbols = new(StringComparer.Ordinal);
    private readonly Dictionary<string, DateTimeOffset> _lastSignalLog = new(StringComparer.Ordinal);
    private readonly Dictionary<string, DateTimeOffset> _lastFundingSkipLog = new(StringComparer.Ordinal);
    private DashboardSnapshot? _snapshot;
    private bool _prepared;

    public DashboardSnapshot? Snapshot => Volatile.Read(ref _snapshot);

    public async Task RunAsync(CancellationToken ct)
    {
        EnsurePrepared();
        log.Info($"Сканер запущен: бирж {_connectors.Count}, пар после фильтра {_allowedSymbols.Values.Sum(s => s.Count)}");

        while (!ct.IsCancellationRequested)
        {
            try
            {
                await ScanOnceAsync(ct);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                log.Error($"Сбой цикла сканирования: {ex.Message}");
                logger.LogDebug(ex, "scan loop failure");
                try
                {
                    await Task.Delay(1_000, ct);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
            }
        }

        log.Info("Сканер остановлен.");
    }

    public async Task ScanOnceAsync(CancellationToken ct)
    {
        EnsurePrepared();
        var options = config.Current;
        var fetched = await FetchAllAsync(ct);
        var totalFetched = fetched.Sum(f => f.Result.Tickers.Count);
        var funding = await FetchFundingAsync(ct);

        var estimates = Evaluate(fetched, funding, options, out var trackedSymbols);

        stats.RecordScanTick(totalFetched);
        foreach (var estimate in estimates)
        {
            stats.RecordOpportunity(estimate);
        }

        var top = estimates.Take(options.General.ConsoleUi.TopRows).ToList();
        _snapshot = stats.BuildDashboard(top, _connectors.Count, trackedSymbols);

        var aboveSpread = estimates
            .Where(e => e.NetPercent >= options.Arbitrage.MinSpreadPercentUp)
            .ToList();

        // фильтр по фандингу: если хотя бы по одной ноге рейт строго ниже предела —
        // пропускаем сделку целиком (обе ноги открываются одной парой)
        var limit = options.Arbitrage.MinFundingRatePercent;
        var candidates = new List<SpreadEstimate>(aboveSpread.Count);
        foreach (var candidate in aboveSpread)
        {
            if (candidate.LongLeg.FundingPercent < limit || candidate.ShortLeg.FundingPercent < limit)
            {
                LogFundingSkip(candidate, limit);
                continue;
            }

            candidates.Add(candidate);
        }

        LogSignals(candidates);

        if (options.Arbitrage.Enabled && candidates.Count > 0)
        {
            await executor.ProcessOpportunitiesAsync(candidates, ct);
        }

        if (options.Arbitrage.Enabled || executor.HasOpenPositions)
        {
            var byExchange = new Dictionary<string, IReadOnlyDictionary<string, TickerSnapshot>>(fetched.Count, StringComparer.Ordinal);
            foreach (var (connector, result) in fetched)
            {
                byExchange[connector.Id] = result.Tickers;
            }

            await executor.ManageOpenPositionsAsync(byExchange, ct);
        }
    }

    /// <summary>Строит карту бирж и множества разрешённых символов (один раз за сеанс).</summary>
    private void EnsurePrepared()
    {
        if (_prepared)
        {
            return;
        }

        Prepare();
        _prepared = true;
    }

    private void Prepare()
    {
        var connectors = registry.Connectors;
        _connectors = connectors.ToDictionary(c => c.Id, StringComparer.Ordinal);
        _allowedSymbols = new Dictionary<string, HashSet<string>>(StringComparer.Ordinal);

        foreach (var connector in connectors)
        {
            var set = new HashSet<string>(StringComparer.Ordinal);
            foreach (var market in connector.PerpetualMarkets)
            {
                if (filter.IsAllowed(market, 0m))
                {
                    set.Add(market.Symbol);
                }
            }

            _allowedSymbols[connector.Id] = set;
            stats.RecordMarkets(connector.Id, connector.DisplayName, connector.MarketCount, connector.DefaultFees, connector.Mode);
            log.Info($"[{connector.DisplayName}] режим {connector.Mode}, рынков {connector.MarketCount}, после фильтра {set.Count}");
        }
    }

    private async Task<List<(IExchangeConnector Connector, FetchTickersResult Result)>> FetchAllAsync(CancellationToken ct)
    {
        var tasks = registry.Connectors.Select(async connector =>
        {
            try
            {
                var result = await connector.FetchTickersAsync(ct);
                stats.RecordRequest(connector.Id, result.Latency, success: true);
                return (Connector: connector, Result: (FetchTickersResult?)result);
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch (Exception ex)
            {
                stats.RecordRequest(connector.Id, TimeSpan.Zero, success: false);
                log.Error($"[{connector.DisplayName}] не удалось получить тикеры: {ex.Message}");
                logger.LogDebug(ex, "fetch tickers failed for {ExchangeId}", connector.Id);
                return (Connector: connector, Result: null);
            }
        });

        var results = await Task.WhenAll(tasks);
        return [.. results.Where(r => r.Result is not null).Select(r => (r.Connector, r.Result!))];
    }

    /// <summary>
    /// Собирает фандинг-рейты по всем биржам (коннекторы кэшируют ответы сами).
    /// Ошибка одной биржи не влияет на остальные: пустая карта = «данных нет» = fail-open.
    /// </summary>
    private async Task<Dictionary<string, IReadOnlyDictionary<string, decimal>>> FetchFundingAsync(CancellationToken ct)
    {
        var tasks = registry.Connectors.Select(async connector =>
        {
            try
            {
                return (Id: connector.Id, Rates: await connector.FetchFundingRatesPercentAsync(ct));
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch (Exception ex)
            {
                log.Debug($"[funding] {connector.Id}: {ex.Message}");
                return (connector.Id, (IReadOnlyDictionary<string, decimal>)new Dictionary<string, decimal>());
            }
        });

        var map = new Dictionary<string, IReadOnlyDictionary<string, decimal>>(StringComparer.Ordinal);
        foreach (var (id, rates) in await Task.WhenAll(tasks))
        {
            map[id] = rates;
        }

        return map;
    }

    /// <summary>Ищет лучшие пары «дешёвая/дорогая биржа» по каждому символу.</summary>
    private List<SpreadEstimate> Evaluate(
        List<(IExchangeConnector Connector, FetchTickersResult Result)> fetched,
        Dictionary<string, IReadOnlyDictionary<string, decimal>> funding,
        BotOptions options,
        out int trackedSymbols)
    {
        var symbolsOptions = options.Symbols;
        var volumeMinimum = symbolsOptions.MinQuoteVolume24hUsd;
        var maxAge = TimeSpan.FromSeconds(symbolsOptions.MaxTickerAgeSeconds);
        var now = time.GetUtcNow();

        var book = new Dictionary<string, Dictionary<string, TickerSnapshot>>(4096, StringComparer.Ordinal);
        foreach (var (connector, result) in fetched)
        {
            if (!_allowedSymbols.TryGetValue(connector.Id, out var allowed))
            {
                continue;
            }

            foreach (var (symbol, ticker) in result.Tickers)
            {
                if (!allowed.Contains(symbol))
                {
                    continue;
                }

                // объём = 0 означает «биржа не отдаёт оборот в тикере» — такие символы не отбрасываем
                if (ticker.QuoteVolume24h > 0m && ticker.QuoteVolume24h < volumeMinimum)
                {
                    continue;
                }

                if (ticker.Timestamp != default && ticker.Timestamp < now - maxAge)
                {
                    continue;
                }

                if (!book.TryGetValue(symbol, out var quotes))
                {
                    book[symbol] = quotes = new Dictionary<string, TickerSnapshot>(4, StringComparer.Ordinal);
                }

                quotes[connector.Id] = ticker;
            }
        }

        var crossExchange = book
            .Where(kv => kv.Value.Count >= 2)
            .Select(kv => (Symbol: kv.Key, Quotes: kv.Value, Volume: kv.Value.Values.Max(t => t.QuoteVolume24h)))
            .ToList();

        log.Debug($"[scan] тикеров после фильтра бирж: {fetched.Sum(f => f.Result.Tickers.Count)}, символов в книге: {book.Count}, на ≥2 биржах: {crossExchange.Count}");

        trackedSymbols = crossExchange.Count;

        // ограничение числа отслеживаемых пар — берём самые ликвидные
        if (symbolsOptions.MaxSymbols > 0 && crossExchange.Count > symbolsOptions.MaxSymbols)
        {
            crossExchange = [.. crossExchange
                .OrderByDescending(x => x.Volume)
                .Take(symbolsOptions.MaxSymbols)];
            trackedSymbols = crossExchange.Count;
        }

        List<SpreadEstimate> estimates = [];
        foreach (var (symbol, quotes, _) in crossExchange)
        {
            TickerSnapshot? cheaper = null;
            TickerSnapshot? richer = null;
            foreach (var ticker in quotes.Values)
            {
                if (cheaper is null || ticker.Ask < cheaper.Ask)
                {
                    cheaper = ticker;
                }

                if (richer is null || ticker.Bid > richer.Bid)
                {
                    richer = ticker;
                }
            }

            if (cheaper is null || richer is null || cheaper.ExchangeId == richer.ExchangeId)
            {
                continue;
            }

            var estimate = calculator.Calculate(cheaper, richer, FeesOf(cheaper.ExchangeId, symbol), FeesOf(richer.ExchangeId, symbol));
            if (estimate is not null)
            {
                var longFunding = FundingOf(funding, cheaper.ExchangeId, symbol);
                var shortFunding = FundingOf(funding, richer.ExchangeId, symbol);
                if (longFunding is not null || shortFunding is not null)
                {
                    estimate = estimate with
                    {
                        LongLeg = estimate.LongLeg with { FundingPercent = longFunding },
                        ShortLeg = estimate.ShortLeg with { FundingPercent = shortFunding },
                    };
                }

                estimates.Add(estimate);
            }
        }

        estimates.Sort((a, b) => b.NetPercent.CompareTo(a.NetPercent));
        return estimates;
    }

    private ExchangeFees FeesOf(string exchangeId, string symbol) => _connectors.TryGetValue(exchangeId, out var connector)
        ? (connector.TryGetFees(symbol, out var fees) ? fees : connector.DefaultFees)
        : new ExchangeFees(0.1m, 0.05m);

    /// <summary>Фандинг-рейт ноги в %; null — биржа не отдала данные (fail-open).</summary>
    private static decimal? FundingOf(Dictionary<string, IReadOnlyDictionary<string, decimal>> funding, string exchangeId, string symbol)
        => funding.TryGetValue(exchangeId, out var rates) && rates.TryGetValue(symbol, out var rate) ? rate : null;

    /// <summary>Отказ по фандингу пишется в журнал не чаще раза в минуту на символ.</summary>
    private void LogFundingSkip(SpreadEstimate candidate, decimal limit)
    {
        var now = time.GetUtcNow();
        if (_lastFundingSkipLog.TryGetValue(candidate.Symbol, out var last) && now - last < TimeSpan.FromMinutes(1))
        {
            return;
        }

        _lastFundingSkipLog[candidate.Symbol] = now;
        log.Info($"Пропуск {candidate.Symbol}: фандинг лонг {Formatting.OptionalPct(candidate.LongLeg.FundingPercent)} / " +
                 $"шорт {Formatting.OptionalPct(candidate.ShortLeg.FundingPercent)} — ниже предела {Formatting.Pct(limit)}, сделка не открывается");
    }

    /// <summary>Сигналы пишутся в журнал не чаще раза в минуту на символ.</summary>
    private void LogSignals(IReadOnlyList<SpreadEstimate> candidates)
    {
        var now = time.GetUtcNow();
        foreach (var candidate in candidates)
        {
            if (_lastSignalLog.TryGetValue(candidate.Symbol, out var last) && now - last < TimeSpan.FromMinutes(1))
            {
                continue;
            }

            _lastSignalLog[candidate.Symbol] = now;
            log.Success($"Сигнал {candidate.Symbol}: лонг {candidate.LongLeg.ExchangeId} @ {Formatting.Price(candidate.LongLeg.Price)} → шорт {candidate.ShortLeg.ExchangeId} @ {Formatting.Price(candidate.ShortLeg.Price)}, нетто {Formatting.Pct(candidate.NetPercent)}");
        }
    }
}
