namespace FuturesArbBot.Core.Engine;

/// <summary>
/// Исполнитель арбитражных сделок: открывает встречные фьючерсные позиции
/// (лонг на дешёвой бирже + шорт на дорогой), ведёт их и закрывает
/// по порогам «вниз» (прибыль), стоп-лоссу или таймауту.
/// В режиме DryRun все сделки симулируются по текущим котировкам.
/// </summary>
public sealed class ArbTradeExecutor(
    IConfigProvider config,
    ConnectorRegistry registry,
    IStatisticsCollector stats,
    IEventLog log,
    TimeProvider time,
    ILogger<ArbTradeExecutor> logger) : ITradeExecutor
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly Dictionary<string, PositionPair> _positions = new(StringComparer.Ordinal);

    // кеш последних тикеров: exchangeId → symbol → ticker (используется при закрытии на выходе)
    private readonly Dictionary<string, Dictionary<string, TickerSnapshot>> _lastTickers = new(StringComparer.Ordinal);
    private Dictionary<string, Dictionary<string, MarketInfo>>? _marketsCache;

    public bool HasOpenPositions
    {
        get
        {
            lock (_positions)
            {
                return _positions.Count > 0;
            }
        }
    }

    public async Task ProcessOpportunitiesAsync(IReadOnlyList<SpreadEstimate> candidates, CancellationToken ct)
    {
        if (candidates.Count == 0)
        {
            return;
        }

        await _gate.WaitAsync(ct);
        try
        {
            var options = config.Current.Arbitrage;
            foreach (var candidate in candidates)
            {
                lock (_positions)
                {
                    if (_positions.ContainsKey(candidate.Symbol))
                    {
                        continue; // на символ — одна арбитражная позиция
                    }

                    if (_positions.Count >= options.MaxOpenPositions)
                    {
                        return; // лимит позиций исчерпан
                    }
                }

                var perExchange = CountPerExchange();
                if (perExchange.GetValueOrDefault(candidate.LongLeg.ExchangeId) >= options.MaxPositionsPerExchange
                    || perExchange.GetValueOrDefault(candidate.ShortLeg.ExchangeId) >= options.MaxPositionsPerExchange)
                {
                    continue;
                }

                await OpenAsync(candidate, options, ct);
            }
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task ManageOpenPositionsAsync(IReadOnlyDictionary<string, IReadOnlyDictionary<string, TickerSnapshot>> tickersByExchange, CancellationToken ct)
    {
        UpdateTickerCache(tickersByExchange);

        var options = config.Current.Arbitrage;
        var now = time.GetUtcNow();
        List<(PositionPair Position, CloseReason Reason)> toClose = [];

        lock (_positions)
        {
            foreach (var position in _positions.Values)
            {
                var longTicker = FindTicker(position.LongExchangeId, position.Symbol);
                var shortTicker = FindTicker(position.ShortExchangeId, position.Symbol);
                if (longTicker is null || shortTicker is null || !longTicker.IsTradable || !shortTicker.IsTradable)
                {
                    continue;
                }

                var currentGross = (shortTicker.Bid - longTicker.Ask) / longTicker.Ask * 100m;
                var reason = currentGross <= options.MinSpreadPercentDown ? CloseReason.TakeProfit
                    : currentGross >= options.StopLossSpreadPercent ? CloseReason.StopLoss
                    : now - position.OpenedAt >= TimeSpan.FromMinutes(options.MaxPositionAgeMinutes) ? CloseReason.Timeout
                    : (CloseReason?)null;

                if (reason is not null)
                {
                    toClose.Add((position, reason.Value));
                }
            }
        }

        if (toClose.Count == 0)
        {
            return;
        }

        await _gate.WaitAsync(ct);
        try
        {
            foreach (var (position, reason) in toClose)
            {
                await CloseAsync(position, reason, ct);
            }
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task CloseAllAsync(CloseReason reason, CancellationToken ct)
    {
        List<PositionPair> snapshot;
        lock (_positions)
        {
            snapshot = [.. _positions.Values];
        }

        if (snapshot.Count == 0)
        {
            return;
        }

        log.Warning($"Закрываю все позиции ({snapshot.Count}) перед выходом…");
        await _gate.WaitAsync(ct);
        try
        {
            foreach (var position in snapshot)
            {
                await CloseAsync(position, reason, ct);
            }
        }
        finally
        {
            _gate.Release();
        }
    }

    // ------------------------- открытие -------------------------

    private async Task OpenAsync(SpreadEstimate estimate, ArbitrageOptions options, CancellationToken ct)
    {
        var mode = config.Current.General.NetworkMode;
        var longConnector = registry.Connectors.FirstOrDefault(c => c.Id == estimate.LongLeg.ExchangeId);
        var shortConnector = registry.Connectors.FirstOrDefault(c => c.Id == estimate.ShortLeg.ExchangeId);
        if (longConnector is null || shortConnector is null)
        {
            return;
        }

        var amount = ResolveAmount(longConnector, shortConnector, estimate, options);
        if (amount is null)
        {
            return;
        }

        if (mode == NetworkMode.DryRun)
        {
            OpenSimulated(estimate, amount.Value, options);
            return;
        }

        log.Info($"Открываю арбитраж {estimate.Symbol}: BUY {Formatting.Volume(amount.Value)} на {longConnector.DisplayName}, SELL на {shortConnector.DisplayName} (нетто {Formatting.Pct(estimate.NetPercent)})");

        await longConnector.SetLeverageAsync(options.Leverage, estimate.Symbol, ct);
        await shortConnector.SetLeverageAsync(options.Leverage, estimate.Symbol, ct);

        var buy = await longConnector.PlaceOrderAsync(new OrderRequest(estimate.Symbol, OrderSide.Buy, amount.Value), ct);
        if (!buy.Success)
        {
            stats.RecordOpenFailed(estimate.Symbol, buy.Error ?? "неизвестная ошибка");
            return;
        }

        var sell = await shortConnector.PlaceOrderAsync(new OrderRequest(estimate.Symbol, OrderSide.Sell, amount.Value), ct);
        if (!sell.Success)
        {
            // вторая нога не прошла — срочно закатываем первую (reduceOnly)
            log.Error($"[{shortConnector.DisplayName}] шорт не открылся ({sell.Error}); откатываю лонг");
            await longConnector.PlaceOrderAsync(new OrderRequest(estimate.Symbol, OrderSide.Sell, amount.Value, ReduceOnly: true), CancellationToken.None);
            stats.RecordOpenFailed(estimate.Symbol, $"шорт не открыт: {sell.Error}");
            return;
        }

        var fees = EstimateFees(longConnector, shortConnector, estimate.Symbol, buy.AveragePrice ?? estimate.LongLeg.Price, sell.AveragePrice ?? estimate.ShortLeg.Price, amount.Value);
        var position = new PositionPair
        {
            Id = Guid.NewGuid(),
            Symbol = estimate.Symbol,
            LongExchangeId = longConnector.Id,
            ShortExchangeId = shortConnector.Id,
            LongSize = buy.FilledAmount,
            ShortSize = sell.FilledAmount,
            EntryLong = buy.AveragePrice ?? estimate.LongLeg.Price,
            EntryShort = sell.AveragePrice ?? estimate.ShortLeg.Price,
            FeesEntryUsd = fees,
            OpenedAt = time.GetUtcNow(),
            Simulated = false,
        };

        lock (_positions)
        {
            _positions[position.Symbol] = position;
        }

        stats.RecordOpened(position);
        log.Success($"Открыт арбитраж {position.Symbol}: {Formatting.Volume(position.MatchedSize)} × лонг {position.LongExchangeId} @ {Formatting.Price(position.EntryLong)} / шорт {position.ShortExchangeId} @ {Formatting.Price(position.EntryShort)}");
    }

    private void OpenSimulated(SpreadEstimate estimate, decimal amount, ArbitrageOptions options)
    {
        var longConnector = registry.Connectors.First(c => c.Id == estimate.LongLeg.ExchangeId);
        var shortConnector = registry.Connectors.First(c => c.Id == estimate.ShortLeg.ExchangeId);
        var fees = EstimateFees(longConnector, shortConnector, estimate.Symbol, estimate.LongLeg.Price, estimate.ShortLeg.Price, amount);

        var position = new PositionPair
        {
            Id = Guid.NewGuid(),
            Symbol = estimate.Symbol,
            LongExchangeId = estimate.LongLeg.ExchangeId,
            ShortExchangeId = estimate.ShortLeg.ExchangeId,
            LongSize = amount,
            ShortSize = amount,
            EntryLong = estimate.LongLeg.Price,
            EntryShort = estimate.ShortLeg.Price,
            FeesEntryUsd = fees,
            OpenedAt = time.GetUtcNow(),
            Simulated = true,
        };

        lock (_positions)
        {
            _positions[position.Symbol] = position;
        }

        stats.RecordOpened(position);
        log.Success($"[СИМУЛЯЦИЯ] Открыт арбитраж {position.Symbol}: {Formatting.Volume(amount)} × лонг {position.LongExchangeId} @ {Formatting.Price(position.EntryLong)} / шорт {position.ShortExchangeId} @ {Formatting.Price(position.EntryShort)} (нетто {Formatting.Pct(estimate.NetPercent)})");
    }

    // ------------------------- закрытие -------------------------

    private async Task CloseAsync(PositionPair position, CloseReason reason, CancellationToken ct)
    {
        var longConnector = registry.Connectors.FirstOrDefault(c => c.Id == position.LongExchangeId);
        var shortConnector = registry.Connectors.FirstOrDefault(c => c.Id == position.ShortExchangeId);
        if (longConnector is null || shortConnector is null)
        {
            return;
        }

        var longTicker = FindTicker(position.LongExchangeId, position.Symbol);
        var shortTicker = FindTicker(position.ShortExchangeId, position.Symbol);

        if (config.Current.General.NetworkMode == NetworkMode.DryRun)
        {
            // симуляция: закрытие по текущим bid/ask (или по входу, если котировок нет)
            position.ExitLong = longTicker?.Bid ?? position.EntryLong;
            position.ExitShort = shortTicker?.Ask ?? position.EntryShort;
        }
        else
        {
            var closeLong = await longConnector.PlaceOrderAsync(
                new OrderRequest(position.Symbol, OrderSide.Sell, position.MatchedSize, ReduceOnly: true), ct);
            if (!closeLong.Success)
            {
                log.Error($"[{longConnector.DisplayName}] закрытие лонга {position.Symbol} не удалось: {closeLong.Error}");
                return; // позиция остаётся открытой, попробуем на следующем тике
            }

            var closeShort = await shortConnector.PlaceOrderAsync(
                new OrderRequest(position.Symbol, OrderSide.Buy, position.MatchedSize, ReduceOnly: true), ct);
            if (!closeShort.Success)
            {
                log.Error($"[{shortConnector.DisplayName}] закрытие шорта {position.Symbol} не удалось: {closeShort.Error}");
                position.Status = PositionStatus.Open; // лонг закрыт — шорт остался «голым»
                log.Warning($"[{position.Symbol}] шорт остался без пары — требует ручного внимания!");
                return;
            }

            position.ExitLong = closeLong.AveragePrice ?? longTicker?.Bid;
            position.ExitShort = closeShort.AveragePrice ?? shortTicker?.Ask;
        }

        var exitLong = position.ExitLong ?? position.EntryLong;
        var exitShort = position.ExitShort ?? position.EntryShort;
        position.FeesExitUsd = EstimateFees(longConnector, shortConnector, position.Symbol, exitLong, exitShort, position.MatchedSize);
        position.Reason = reason;
        position.ClosedAt = time.GetUtcNow();
        position.Status = PositionStatus.Closed;

        // лонг: (exit − entry); шорт: (entry − exit); минус комиссии обеих сторон
        position.RealizedPnlUsd = position.MatchedSize * ((exitLong - position.EntryLong) + (position.EntryShort - exitShort))
                                  - position.FeesEntryUsd - position.FeesExitUsd;

        lock (_positions)
        {
            _positions.Remove(position.Symbol);
        }

        stats.RecordClosed(position);
        log.Success($"Закрыт арбитраж {position.Symbol} ({Describe(reason)}): PnL {Formatting.Usd(position.RealizedPnlUsd.Value)}");
    }

    // ------------------------- вспомогательное -------------------------

    private decimal? ResolveAmount(IExchangeConnector longConnector, IExchangeConnector shortConnector, SpreadEstimate estimate, ArbitrageOptions options)
    {
        var byPrice = options.OrderSizeUsd / estimate.LongLeg.Price;
        decimal? amount = byPrice;

        foreach (var (connector, price) in new[]
                 {
                     (longConnector, estimate.LongLeg.Price),
                     (shortConnector, estimate.ShortLeg.Price),
                 })
        {
            var market = FindMarket(connector, estimate.Symbol);
            if (market?.MinAmount is > 0m && amount < market.MinAmount.Value)
            {
                var requiredUsd = market.MinAmount.Value * price;
                if (requiredUsd > options.OrderSizeUsd * 5m)
                {
                    log.Warning($"{estimate.Symbol}: минимальный объём на {connector.DisplayName} ({market.MinAmount.Value}) требует ~{Formatting.Usd(requiredUsd)} — пропуск");
                    return null;
                }

                amount = market.MinAmount.Value;
            }
        }

        return amount is > 0m ? amount : null;
    }

    private MarketInfo? FindMarket(IExchangeConnector connector, string symbol)
    {
        if (_marketsCache is null || !_marketsCache.TryGetValue(connector.Id, out var bySymbol))
        {
            _marketsCache ??= new Dictionary<string, Dictionary<string, MarketInfo>>(StringComparer.Ordinal);
            bySymbol = connector.PerpetualMarkets.ToDictionary(m => m.Symbol, StringComparer.Ordinal);
            _marketsCache[connector.Id] = bySymbol;
        }

        return bySymbol.TryGetValue(symbol, out var market) ? market : null;
    }

    private static decimal EstimateFees(IExchangeConnector longConnector, IExchangeConnector shortConnector, string symbol, decimal longPrice, decimal shortPrice, decimal size)
    {
        var longFees = longConnector.TryGetFees(symbol, out var lf) ? lf.TakerPercent : longConnector.DefaultFees.TakerPercent;
        var shortFees = shortConnector.TryGetFees(symbol, out var sf) ? sf.TakerPercent : shortConnector.DefaultFees.TakerPercent;
        return size * (longPrice * longFees + shortPrice * shortFees) / 100m;
    }

    private Dictionary<string, int> CountPerExchange()
    {
        Dictionary<string, int> counts = new(StringComparer.Ordinal);
        lock (_positions)
        {
            foreach (var position in _positions.Values)
            {
                counts[position.LongExchangeId] = counts.GetValueOrDefault(position.LongExchangeId) + 1;
                counts[position.ShortExchangeId] = counts.GetValueOrDefault(position.ShortExchangeId) + 1;
            }
        }

        return counts;
    }

    private void UpdateTickerCache(IReadOnlyDictionary<string, IReadOnlyDictionary<string, TickerSnapshot>> tickersByExchange)
    {
        foreach (var (exchangeId, tickers) in tickersByExchange)
        {
            if (!_lastTickers.TryGetValue(exchangeId, out var bucket))
            {
                _lastTickers[exchangeId] = bucket = new Dictionary<string, TickerSnapshot>(1024, StringComparer.Ordinal);
            }

            foreach (var (symbol, ticker) in tickers)
            {
                bucket[symbol] = ticker;
            }
        }
    }

    private TickerSnapshot? FindTicker(string exchangeId, string symbol) => _lastTickers.TryGetValue(exchangeId, out var bucket)
        && bucket.TryGetValue(symbol, out var ticker) ? ticker : null;

    private static string Describe(CloseReason reason) => reason switch
    {
        CloseReason.TakeProfit => "тейк-профит",
        CloseReason.StopLoss => "стоп-лосс",
        CloseReason.Timeout => "таймаут позиции",
        CloseReason.SessionEnd => "завершение сеанса",
        CloseReason.Rollback => "откат сделки",
        _ => reason.ToString(),
    };
}
