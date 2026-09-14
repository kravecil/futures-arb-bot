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

    /// <summary>Состояние исполнения одной limit-ноги при открытии.</summary>
    private sealed class LegState
    {
        public required IExchangeConnector Connector { get; init; }

        public required OrderRequest Request { get; init; }

        public string? OrderId { get; set; }

        public decimal Filled { get; set; }

        public decimal? AveragePrice { get; set; }

        public string? Error { get; set; }

        /// <summary>Нога исполнена полностью.</summary>
        public bool Done { get; set; }

        /// <summary>Нога мертва: отклонена, отменена, истекла или пропала.</summary>
        public bool Dead { get; set; }
    }

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

        log.Info($"Открываю арбитраж {estimate.Symbol}: limit BUY {Formatting.Volume(amount.Value)} @ {Formatting.Price(estimate.LongLeg.Price)} на {longConnector.DisplayName}, limit SELL @ {Formatting.Price(estimate.ShortLeg.Price)} на {shortConnector.DisplayName} (нетто {Formatting.Pct(estimate.NetPercent)})");

        await longConnector.SetLeverageAsync(options.Leverage, estimate.Symbol, ct);
        await shortConnector.SetLeverageAsync(options.Leverage, estimate.Symbol, ct);

        // Marketable limit: покупка по ask дешёвой биржи, продажа по bid дорогой —
        // исполнение сразу при наличии ликвидности, но не хуже указанной цены.
        LegState longLeg = new()
        {
            Connector = longConnector,
            Request = new OrderRequest(estimate.Symbol, OrderSide.Buy, amount.Value, Type: OrderType.Limit, Price: estimate.LongLeg.Price),
        };
        LegState shortLeg = new()
        {
            Connector = shortConnector,
            Request = new OrderRequest(estimate.Symbol, OrderSide.Sell, amount.Value, Type: OrderType.Limit, Price: estimate.ShortLeg.Price),
        };
        LegState[] legs = [longLeg, shortLeg];

        // 1) выставляем ноги последовательно: отказ первой — вторая не выставляется вообще
        foreach (var leg in legs)
        {
            if (!await PlaceAsync(leg, ct))
            {
                var failReason = $"limit {leg.Request.Side} на {leg.Connector.DisplayName} отклонён: {leg.Error}";
                log.Error($"{estimate.Symbol}: {failReason}; вторая нога не выставляется");
                await AbortLegsAsync(legs, ct);
                stats.RecordOpenFailed(estimate.Symbol, failReason);
                return;
            }
        }

        // 2) ждём полного исполнения обеих ног (или смерти заявки/таймаута)
        await WaitForLegsAsync(legs, options, ct);

        // 3) если что-то не исполнено — отменяем живые заявки и откатываем набранное
        if (legs.Any(l => !l.Done))
        {
            var reasons = string.Join("; ", legs.Where(l => !l.Done)
                .Select(l => $"{l.Request.Side} на {l.Connector.DisplayName}: {l.Error ?? (l.Dead ? "заявка мертва" : "не исполнена вовремя")}"));
            log.Error($"{estimate.Symbol}: открытие не удалось ({reasons}); отменяю и откатываю ноги");
            var (residualLong, residualShort) = await AbortLegsAsync(legs, ct);
            stats.RecordOpenFailed(estimate.Symbol, reasons);

            if (residualLong > 0m || residualShort > 0m)
            {
                // откат прошёл не полностью — регистрируем «кривую» позицию,
                // чтобы цикл ведения и ребалансировки довёл её до конца
                RegisterPartialPosition(estimate, longLeg, shortLeg, residualLong, residualShort);
            }

            return;
        }

        // 4) успех — позиция с раздельными объёмами ног
        RegisterPosition(estimate, longLeg, shortLeg);
    }

    /// <summary>Выставить limit-ордер ноги. false — биржа отказала (нога мертва).</summary>
    private async Task<bool> PlaceAsync(LegState leg, CancellationToken ct)
    {
        var result = await leg.Connector.PlaceOrderAsync(leg.Request, ct);
        if (!result.Success)
        {
            leg.Error = result.Error ?? "ордер отклонён";
            leg.Dead = true;
            log.Error($"[{leg.Connector.DisplayName}] limit {leg.Request.Side} {leg.Request.Symbol} не выставлен: {leg.Error}");
            return false;
        }

        leg.OrderId = result.OrderId;
        leg.Filled = result.FilledAmount;
        leg.AveragePrice = result.AveragePrice;
        leg.Done = leg.Filled >= leg.Request.Amount;
        return true;
    }

    /// <summary>Опрашивает ноги до полного исполнения, смерти заявки или истечения таймаута.</summary>
    private async Task WaitForLegsAsync(LegState[] legs, ArbitrageOptions options, CancellationToken ct)
    {
        var deadline = time.GetUtcNow() + TimeSpan.FromMilliseconds(options.OrderExecutionTimeoutMs);
        while (true)
        {
            var pending = legs.Where(l => !l.Done && !l.Dead).ToList();
            if (pending.Count == 0)
            {
                break;
            }

            foreach (var leg in pending)
            {
                if (leg.OrderId is null)
                {
                    leg.Dead = true;
                    continue;
                }

                var update = await leg.Connector.FetchOrderAsync(leg.OrderId, leg.Request.Symbol, ct);
                if (update is null)
                {
                    leg.Dead = true;
                    leg.Error ??= "заявка не найдена при опросе";
                    continue;
                }

                leg.Filled = Math.Max(leg.Filled, update.FilledAmount);
                leg.AveragePrice = update.AveragePrice ?? leg.AveragePrice;
                if (update.IsFilled)
                {
                    leg.Done = true;
                }
                else if (update.IsDead)
                {
                    leg.Dead = true;
                }
            }

            if (time.GetUtcNow() >= deadline)
            {
                foreach (var leg in legs.Where(l => !l.Done && !l.Dead))
                {
                    leg.Error = $"не исполнена за {options.OrderExecutionTimeoutMs} мс, набрано {Formatting.Volume(leg.Filled)}";
                }

                break;
            }

            await Task.Delay(options.OrderPollIntervalMs, ct);
        }
    }

    /// <summary>
    /// Отменяет живые заявки обеих ног и откатывает набранный объём market reduceOnly (с ретраями).
    /// Возвращает остатки (long, short), которые не удалось откатить.
    /// </summary>
    private async Task<(decimal Long, decimal Short)> AbortLegsAsync(LegState[] legs, CancellationToken ct)
    {
        var residuals = new decimal[legs.Length];

        for (var i = 0; i < legs.Length; i++)
        {
            var leg = legs[i];

            // добиваемся отмены: после cancel перечитываем финальное состояние (гонка отмены и исполнения)
            if (!leg.Done && !leg.Dead && leg.OrderId is not null)
            {
                for (var attempt = 0; attempt < 3 && !leg.Dead; attempt++)
                {
                    await leg.Connector.CancelOrderAsync(leg.OrderId, leg.Request.Symbol, CancellationToken.None);
                    var update = await leg.Connector.FetchOrderAsync(leg.OrderId, leg.Request.Symbol, CancellationToken.None);
                    if (update is null || update.IsDead)
                    {
                        leg.Filled = update?.FilledAmount ?? leg.Filled;
                        leg.Dead = true;
                    }
                    else if (update.IsFilled)
                    {
                        leg.Filled = update.FilledAmount;
                        leg.AveragePrice = update.AveragePrice ?? leg.AveragePrice;
                        leg.Done = true;
                    }
                }
            }

            // откат набранного — market reduceOnly с ретраями
            var remaining = leg.Filled;
            var closeSide = leg.Request.Side == OrderSide.Buy ? OrderSide.Sell : OrderSide.Buy;
            for (var attempt = 0; attempt < 3 && remaining > 0m; attempt++)
            {
                var rollback = await leg.Connector.PlaceOrderAsync(
                    new OrderRequest(leg.Request.Symbol, closeSide, remaining, ReduceOnly: true), CancellationToken.None);
                if (rollback.Success)
                {
                    remaining -= rollback.FilledAmount > 0m ? rollback.FilledAmount : remaining;
                }
                else
                {
                    log.Error($"[{leg.Connector.DisplayName}] откат {closeSide} {leg.Request.Symbol} × {remaining} не прошёл (попытка {attempt + 1}): {rollback.Error}");
                    await Task.Delay(250, CancellationToken.None);
                }
            }

            residuals[i] = Math.Max(remaining, 0m);
            if (residuals[i] > 0m)
            {
                log.Error($"[{leg.Connector.DisplayName}] после откатов остаётся {leg.Request.Side} × {Formatting.Volume(residuals[i])} — передаю цикл ведения");
            }
        }

        _ = ct; // откат выполняется по CancellationToken.None: он важнее выхода из процесса
        return (residuals[0], residuals[1]);
    }

    /// <summary>Регистрирует полностью открытую позицию по факту исполнения обеих ног.</summary>
    private void RegisterPosition(SpreadEstimate estimate, LegState longLeg, LegState shortLeg)
    {
        var entryLong = longLeg.AveragePrice ?? estimate.LongLeg.Price;
        var entryShort = shortLeg.AveragePrice ?? estimate.ShortLeg.Price;
        var fees = EstimateFees(longLeg.Connector, shortLeg.Connector, estimate.Symbol, entryLong, entryShort, longLeg.Filled);

        var position = new PositionPair
        {
            Id = Guid.NewGuid(),
            Symbol = estimate.Symbol,
            LongExchangeId = longLeg.Connector.Id,
            ShortExchangeId = shortLeg.Connector.Id,
            LongSize = longLeg.Filled,
            ShortSize = shortLeg.Filled,
            EntryLong = entryLong,
            EntryShort = entryShort,
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

    /// <summary>
    /// Регистрирует позицию после неудачного открытия с остатками откатов:
    /// объёмы ног различаются — ребалансировка и закрытие доведут её до конца.
    /// </summary>
    private void RegisterPartialPosition(SpreadEstimate estimate, LegState longLeg, LegState shortLeg, decimal residualLong, decimal residualShort)
    {
        var entryLong = longLeg.AveragePrice ?? estimate.LongLeg.Price;
        var entryShort = shortLeg.AveragePrice ?? estimate.ShortLeg.Price;

        var position = new PositionPair
        {
            Id = Guid.NewGuid(),
            Symbol = estimate.Symbol,
            LongExchangeId = longLeg.Connector.Id,
            ShortExchangeId = shortLeg.Connector.Id,
            LongSize = residualLong,
            ShortSize = residualShort,
            EntryLong = entryLong,
            EntryShort = entryShort,
            FeesEntryUsd = EstimateFees(longLeg.Connector, shortLeg.Connector, estimate.Symbol, entryLong, entryShort, Math.Max(residualLong, residualShort)),
            OpenedAt = time.GetUtcNow(),
            Simulated = false,
        };

        lock (_positions)
        {
            _positions[position.Symbol] = position;
        }

        stats.RecordOpened(position);
        log.Warning($"{position.Symbol}: после отката остались ножки — лонг {Formatting.Volume(position.LongSize)} / шорт {Formatting.Volume(position.ShortSize)}, позиция передана циклу ведения");
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
