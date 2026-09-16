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

    /// <summary>
    /// Уже показанные пояснения политики исполнения (exchangeId|заметка): понижение
    /// unsupported-возможности сообщается один раз, а не на каждой сделке.
    /// </summary>
    private readonly HashSet<string> _policyNotes = new(StringComparer.Ordinal);

    /// <summary>
    /// Экспозиция бирж вне учёта сеанса (результат сверки <see cref="ReconcileExternalExposureAsync"/>):
    /// exchangeId → символы с ненулевой позицией. Лимиты MaxOpenPositions/MaxPositionsPerExchange
    /// считаются вместе с ней — иначе после рестарта бот открывал «ещё одну» поверх забытой на бирже.
    /// </summary>
    private readonly Dictionary<string, HashSet<string>> _externalExposure = new(StringComparer.Ordinal);

    /// <summary>Заявки, не снятые при откате открытия: цикл ведения продолжит их отменять каждый тик.</summary>
    private readonly List<StrayOrder> _strayOrders = [];

    /// <summary>Биржи, о сбое сверки на которых уже сообщали (не спамить в журнал каждый тик).</summary>
    private readonly HashSet<string> _reconcileFailWarned = new(StringComparer.Ordinal);

    private DateTimeOffset _lastReconcileAt;

    /// <summary>Период сверки фактической экспозиции с биржами (fetchPositions — приватный запрос).</summary>
    private static readonly TimeSpan ReconcileInterval = TimeSpan.FromSeconds(60);

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

            // лимиты обязаны считать фактическую экспозицию бирж, а не только память сеанса
            await ReconcileExternalExposureAsync(ct);

            foreach (var candidate in candidates)
            {
                var externalArbitrages = CountExternalArbitrages();
                lock (_positions)
                {
                    if (_positions.ContainsKey(candidate.Symbol))
                    {
                        continue; // на символ — одна арбитражная позиция
                    }

                    if (_positions.Count + externalArbitrages >= options.MaxOpenPositions)
                    {
                        return; // лимит сеансовых и найденных на биржах арбитражей исчерпан
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

        // заявки, не снятые при откате открытия, живы в стакане и могут исполниться —
        // не забываем их: каждый тик пробуем отменить снова
        await SweepStrayOrdersAsync(ct);

        var options = config.Current.Arbitrage;
        var mode = config.Current.General.NetworkMode;
        var now = time.GetUtcNow();
        List<(PositionPair Position, CloseReason Reason)> toClose = [];
        List<PositionPair> toRebalance = [];

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
                    continue;
                }

                // закрытие ног не завершено — сначала добьём закрытие, не ребалансируя наполовину голые ноги
                if (position.LongClosed != position.ShortClosed)
                {
                    continue;
                }

                if (mode != NetworkMode.DryRun
                    && Math.Abs(position.Imbalance) > RebalanceTolerance(position, options))
                {
                    toRebalance.Add(position);
                }
            }
        }

        if (toClose.Count == 0 && toRebalance.Count == 0)
        {
            return;
        }

        await _gate.WaitAsync(ct);
        try
        {
            var closingSymbols = new HashSet<string>(toClose.Select(x => x.Position.Symbol), StringComparer.Ordinal);

            foreach (var (position, reason) in toClose)
            {
                await CloseAsync(position, reason, ct);
            }

            foreach (var position in toRebalance)
            {
                if (closingSymbols.Contains(position.Symbol))
                {
                    continue; // позиция закрывается — перекос закроется вместе с ней
                }

                await RebalanceAsync(position, options, ct);
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

    /// <summary>
    /// Состояние исполнения одной ноги: одна активная заявка плюс накопленный итог
    /// предыдущих заявок (нужно для догонания цены, когда заявок у ноги несколько).
    /// </summary>
    private sealed class LegState
    {
        public required IExchangeConnector Connector { get; init; }

        /// <summary>Текущая (последняя выставленная) заявка ноги — меняется при догонании.</summary>
        public required OrderRequest Request { get; set; }

        /// <summary>Развёрнутая политика исполнения на бирже этой ноги.</summary>
        public required OrderPolicy Policy { get; set; }

        /// <summary>Полный объём ноги, который нужно набрать (не меняется при перестановках).</summary>
        public required decimal TargetAmount { get; init; }

        /// <summary>Цена первой заявки — от неё считается бюджет отклонения догонания.</summary>
        public required decimal StartPrice { get; init; }

        public string? OrderId { get; set; }

        /// <summary>Цена последней заявки ноги (для смещения следующего шага и оценки исполнений без цены).</summary>
        public decimal QuotePrice { get; set; }

        /// <summary>Суммарное исполнение всех снятых/заменённых заявок ноги.</summary>
        public decimal FilledBase { get; set; }

        /// <summary>Исполнение текущей заявки.</summary>
        public decimal CurrentFilled { get; set; }

        /// <summary>Σ (объём × цена) по всем исполнениям ноги — для средней цены входа.</summary>
        public decimal CostQuote { get; set; }

        /// <summary>Оставшееся количество шагов догонания.</summary>
        public int StepsLeft { get; set; }

        /// <summary>Полный бюджет шагов догонания (для нумерации шагов и оценки отклонения).</summary>
        public int StepsTotal { get; set; }

        /// <summary>Догонание завершено (бюджет исчерпан): повторять fallback не нужно.</summary>
        public bool ChaseFinished { get; set; }

        /// <summary>Момент последней перестановки заявки (для StepIntervalMs).</summary>
        public DateTimeOffset LastQuoteAt { get; set; }

        /// <summary>Накопленное по ноге исполнение (по всем заявкам).</summary>
        public decimal Filled => FilledBase + CurrentFilled;

        /// <summary>Средневзвешенная цена исполнения ноги (null — пока ничего не набрано).</summary>
        public decimal? AveragePrice => Filled > 0m
            ? Math.Round(CostQuote / Filled, 8, MidpointRounding.AwayFromZero)
            : null;

        public string? Error { get; set; }

        /// <summary>Нога исполнена полностью.</summary>
        public bool Done { get; set; }

        /// <summary>Нога мертва: отклонена, отменена, истекла или пропала.</summary>
        public bool Dead { get; set; }
    }

    /// <summary>Заявка, которую не удалось снять при откате открытия (может исполниться сама).</summary>
    private sealed class StrayOrder(IExchangeConnector connector, string orderId, string symbol, OrderSide side)
    {
        public IExchangeConnector Connector { get; } = connector;

        public string OrderId { get; } = orderId;

        public string Symbol { get; } = symbol;

        public OrderSide Side { get; } = side;
    }

    /// <summary>
    /// Доснимает «потерянные» заявки, не отменённые при откате открытия: живая лимитка в стакане
    /// может исполниться и создать экспозицию вне всякого учёта. Каждый тик пробуем отменить
    /// снова; если успела исполниться — берём символ во внешнюю экспозицию для расчёта лимитов.
    /// </summary>
    private async Task SweepStrayOrdersAsync(CancellationToken ct)
    {
        StrayOrder[] strays;
        lock (_strayOrders)
        {
            if (_strayOrders.Count == 0)
            {
                return;
            }

            strays = [.. _strayOrders];
        }

        foreach (var stray in strays)
        {
            var update = await stray.Connector.FetchOrderAsync(stray.OrderId, stray.Symbol, ct);
            if (update is null || update.IsDead)
            {
                lock (_strayOrders)
                {
                    _strayOrders.Remove(stray);
                }

                log.Warning($"[{stray.Connector.DisplayName}] «потерянная» заявка {stray.Symbol} {stray.Side} больше не живёт на бирже");
                continue;
            }

            if (update.IsFilled)
            {
                lock (_strayOrders)
                {
                    _strayOrders.Remove(stray);
                }

                RecordExternalExposure(stray.Connector.Id, stray.Symbol);
                log.Error($"[{stray.Connector.DisplayName}] «потерянная» заявка {stray.Symbol} {stray.Side} исполнилась — позиция вне учёта, учитываю её в лимитах");
                continue;
            }

            await stray.Connector.CancelOrderAsync(stray.OrderId, stray.Symbol, ct);
        }
    }

    /// <summary>Помечает символ как имеющий экспозицию на бирже вне учёта сеанса (для расчёта лимитов).</summary>
    private void RecordExternalExposure(string exchangeId, string symbol)
    {
        lock (_externalExposure)
        {
            if (!_externalExposure.TryGetValue(exchangeId, out var symbols))
            {
                _externalExposure[exchangeId] = symbols = new HashSet<string>(StringComparer.Ordinal);
            }

            symbols.Add(symbol);
        }
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

        // политики ног: конфигурация (глобальная + переопределение биржи) и возможности биржи
        var longPolicy = ResolvePolicy(longConnector, options, close: false);
        var shortPolicy = ResolvePolicy(shortConnector, options, close: false);
        var longPrice = longPolicy.RequiresPrice ? OffsetPrice(estimate.LongLeg.Price, OrderSide.Buy, longPolicy.LimitOffsetBps) : estimate.LongLeg.Price;
        var shortPrice = shortPolicy.RequiresPrice ? OffsetPrice(estimate.ShortLeg.Price, OrderSide.Sell, shortPolicy.LimitOffsetBps) : estimate.ShortLeg.Price;

        log.Info($"Открываю арбитраж {estimate.Symbol}: {EntryDescription(longPolicy, OrderSide.Buy, longPrice, amount.Value)} на {longConnector.DisplayName}, " +
                 $"{EntryDescription(shortPolicy, OrderSide.Sell, shortPrice, amount.Value)} на {shortConnector.DisplayName} (нетто {Formatting.Pct(estimate.NetPercent)})");

        await longConnector.SetLeverageAsync(options.Leverage, estimate.Symbol, ct);
        await shortConnector.SetLeverageAsync(options.Leverage, estimate.Symbol, ct);

        // По умолчанию это marketable limit: покупка по ask дешёвой биржи, продажа по bid дорогой —
        // исполнение сразу при наличии ликвидности, но не хуже указанной цены.
        // Тип заявки, смещение цены и догонание задаёт раздел Execution (OrderPolicyResolver).
        LegState longLeg = BuildLeg(longConnector, longPolicy, estimate.Symbol, OrderSide.Buy, estimate.LongLeg.Price, amount.Value);
        LegState shortLeg = BuildLeg(shortConnector, shortPolicy, estimate.Symbol, OrderSide.Sell, estimate.ShortLeg.Price, amount.Value);
        LegState[] legs = [longLeg, shortLeg];

        // 1) выставляем ноги последовательно: отказ первой — вторая не выставляется вообще
        foreach (var leg in legs)
        {
            if (!await PlaceAsync(leg, ct))
            {
                var failReason = $"{DescribeLeg(leg.Policy.Type, leg.Request.Side)} на {leg.Connector.DisplayName} отклонён: {leg.Error}";
                log.Error($"{estimate.Symbol}: {failReason}; вторая нога не выставляется");
                var (residualLong, residualShort) = await AbortLegsAsync(legs, ct);
                stats.RecordOpenFailed(estimate.Symbol, failReason);

                if (residualLong > 0m || residualShort > 0m)
                {
                    // откат набранного не прошёл — позиция остаётся в учёте для цикла ведения
                    RegisterPartialPosition(estimate, longLeg, shortLeg, residualLong, residualShort);
                }

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

    /// <summary>Выставить заявку ноги. false — биржа отказала (нога мертва).</summary>
    private async Task<bool> PlaceAsync(LegState leg, CancellationToken ct)
    {
        var result = await leg.Connector.PlaceOrderAsync(leg.Request, ct);
        if (!result.Success)
        {
            leg.Error = result.Error ?? "ордер отклонён";
            leg.Dead = true;
            log.Error($"[{leg.Connector.DisplayName}] {DescribeLeg(leg.Policy.Type, leg.Request.Side)} {leg.Request.Symbol} не выставлен: {leg.Error}");
            return false;
        }

        leg.OrderId = result.OrderId;
        RecordFill(leg, result.FilledAmount, result.AveragePrice);
        leg.Done = leg.Filled >= leg.TargetAmount;
        return true;
    }

    /// <summary>
    /// Учитывает исполнение текущей заявки ноги: прирост объёма идёт в накопленный итог,
    /// цена — в средневзвешенную цену входа (без цены биржи берётся цена самой заявки).
    /// </summary>
    private static void RecordFill(LegState leg, decimal currentOrderFilled, decimal? price)
    {
        var delta = currentOrderFilled - leg.CurrentFilled;
        if (delta <= 0m)
        {
            leg.CurrentFilled = Math.Max(leg.CurrentFilled, currentOrderFilled);
            return;
        }

        leg.CurrentFilled += delta;
        leg.CostQuote += delta * (price ?? leg.QuotePrice);
    }

    /// <summary>Переносит исполнение снятой/заменённой заявки в накопленный итог ноги.</summary>
    private static void FoldCurrentFill(LegState leg)
    {
        leg.FilledBase += leg.CurrentFilled;
        leg.CurrentFilled = 0m;
    }

    /// <summary>
    /// Опрашивает ноги до полного исполнения, смерти заявки или истечения таймаута.
    /// Если политике ноги положено локальное догонание цены (Execution:Type = ChaseLimit,
    /// Chase:Mode = Simulated), неисполненная заявка снимается и переставляется ближе к рынку
    /// в пределах бюджета шагов и отклонения.
    /// </summary>
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

                RecordFill(leg, update.FilledAmount, update.AveragePrice);
                if (update.IsFilled)
                {
                    leg.Done = true;
                }
                else if (update.IsDead)
                {
                    leg.Dead = true;
                }
                else if (leg.Filled >= leg.TargetAmount)
                {
                    // объём добран (возможен перелив) — снимаем живую заявку, чтобы не набрать лишнего
                    leg.Done = true;
                    if (!update.IsFilled && !update.IsDead && leg.OrderId is not null)
                    {
                        await leg.Connector.CancelOrderAsync(leg.OrderId, leg.Request.Symbol, CancellationToken.None);
                    }
                }
                else
                {
                    await ChaseStepAsync(leg, ct);
                }
            }

            if (time.GetUtcNow() >= deadline)
            {
                foreach (var leg in legs.Where(l => !l.Done && !l.Dead))
                {
                    leg.Error ??= $"не исполнена за {options.OrderExecutionTimeoutMs} мс, набрано {Formatting.Volume(leg.Filled)}";
                }

                break;
            }

            await Task.Delay(options.OrderPollIntervalMs, ct);
        }
    }

    // ------------------------- догонание цены -------------------------

    /// <summary>
    /// Один такт локального догонания цены: переставить заявку по следующему шагу,
    /// а когда бюджет (MaxSteps либо MaxDeviationBps) исчерпан — добить остаток
    /// market-заявкой (FallbackToMarket) либо остановиться и позволить стандартному
    /// откату снять заявку и зафиксировать набранное.
    /// </summary>
    private async Task ChaseStepAsync(LegState leg, CancellationToken ct)
    {
        // нативный chase: догоняет биржа, локальных перестановок нет; догонание выключено — тоже нет
        if (!leg.Policy.ChasesLocally || leg.ChaseFinished || leg.Policy.Chase is not { } chase)
        {
            return;
        }

        if (leg.StepsLeft <= 0 || !HasChaseBudget(leg, chase))
        {
            leg.ChaseFinished = true;
            if (chase.FallbackToMarket)
            {
                await FillRemainderByMarketAsync(leg, ct);
            }
            else
            {
                leg.Error = $"догонание исчерпано ({chase.MaxSteps} шагов по {chase.StepBps} bps), набрано {Formatting.Volume(leg.Filled)}";
                log.Warning($"[{leg.Connector.DisplayName}] {leg.Request.Symbol} {leg.Request.Side}: {leg.Error}");
            }

            return;
        }

        if (time.GetUtcNow() - leg.LastQuoteAt < TimeSpan.FromMilliseconds(chase.StepIntervalMs))
        {
            return; // ещё не время переставлять заявку
        }

        await RequoteAsync(leg, ChasePrice(leg, chase), ct);
    }

    /// <summary>Снять текущую заявку ноги и переставить неисполненный остаток по новой цене.</summary>
    private async Task RequoteAsync(LegState leg, decimal price, CancellationToken ct)
    {
        var symbol = leg.Request.Symbol;
        var stepNumber = leg.StepsTotal - leg.StepsLeft + 1;

        if (leg.OrderId is not null)
        {
            // отмена идёт вне ct: снять заявку важнее, чем выйти из процесса
            await leg.Connector.CancelOrderAsync(leg.OrderId, symbol, CancellationToken.None);
            var final = await leg.Connector.FetchOrderAsync(leg.OrderId, symbol, CancellationToken.None);
            if (final is not null)
            {
                RecordFill(leg, final.FilledAmount, final.AveragePrice);
            }

            if (final is { IsDead: false, IsFilled: false })
            {
                // заявку снять не удалось — не дублируем её, попробуем на следующем тике
                log.Warning($"[{leg.Connector.DisplayName}] {symbol} {leg.Request.Side}: заявка {leg.OrderId} не снята — догонание откладывается");
                return;
            }

            FoldCurrentFill(leg);
        }

        var remaining = leg.TargetAmount - leg.Filled;
        if (remaining <= 0m)
        {
            leg.Done = true;
            return;
        }

        leg.StepsLeft--;
        leg.LastQuoteAt = time.GetUtcNow();
        leg.QuotePrice = price;
        leg.Request = leg.Request with { Amount = remaining, Price = price };

        if (logger.IsEnabled(LogLevel.Debug))
        {
            logger.LogDebug(
                "Chase {Symbol} {Side} on {Exchange}: step {Step}/{Steps} → {Price} (remaining {Remaining})",
                symbol, leg.Request.Side, leg.Connector.Id, stepNumber, leg.StepsTotal, price, remaining);
        }

        log.Info($"[{leg.Connector.DisplayName}] догоняю {symbol} {leg.Request.Side}: шаг {stepNumber}/{leg.StepsTotal} по {Formatting.Price(price)}, остаток {Formatting.Volume(remaining)}");
        await PlaceAsync(leg, ct);
    }

    /// <summary>Добить неисполненный остаток ноги market-заявкой (Chase:FallbackToMarket).</summary>
    private async Task FillRemainderByMarketAsync(LegState leg, CancellationToken ct)
    {
        var symbol = leg.Request.Symbol;

        if (leg.OrderId is not null && !leg.Dead)
        {
            await leg.Connector.CancelOrderAsync(leg.OrderId, symbol, CancellationToken.None);
            var final = await leg.Connector.FetchOrderAsync(leg.OrderId, symbol, CancellationToken.None);
            if (final is not null)
            {
                RecordFill(leg, final.FilledAmount, final.AveragePrice);
            }

            if (final is { IsDead: false, IsFilled: false })
            {
                log.Warning($"[{leg.Connector.DisplayName}] {symbol} {leg.Request.Side}: заявка {leg.OrderId} не снята — market-добивка откладывается");
                return;
            }

            FoldCurrentFill(leg);
        }

        var remaining = leg.TargetAmount - leg.Filled;
        if (remaining <= 0m)
        {
            leg.Done = true;
            return;
        }

        log.Warning($"[{leg.Connector.DisplayName}] {symbol} {leg.Request.Side}: бюджет догонания исчерпан, добиваю {Formatting.Volume(remaining)} market");

        // дальше нога живёт как market: перестановки больше не нужны
        leg.Policy = leg.Policy with { Type = OrderType.Market, Chase = null };
        leg.Request = leg.Request with { Amount = remaining, Type = OrderType.Market, Price = null, TimeInForce = TimeInForce.Gtc, ExchangeParams = null };
        await PlaceAsync(leg, ct);
    }

    /// <summary>
    /// Цена следующего шага догонания: смещение от стартовой цены на (сделано шагов + 1) × StepBps
    /// в сторону агрессии (BUY — вверх, SELL — вниз), но не дальше бюджета MaxDeviationBps.
    /// </summary>
    private static decimal ChasePrice(LegState leg, ChasePlan chase)
    {
        var bps = Math.Min(NextStepBps(leg, chase), chase.MaxDeviationBps);
        return OffsetPrice(leg.StartPrice, leg.Request.Side, bps);
    }

    /// <summary>Отклонение от стартовой цены (bps), которое даст следующий шаг догонания.</summary>
    private static decimal NextStepBps(LegState leg, ChasePlan chase) =>
        chase.StepBps * (leg.StepsTotal - leg.StepsLeft + 1);

    /// <summary>Есть ли бюджет отклонения хотя бы ещё на один шаг догонания.</summary>
    private static bool HasChaseBudget(LegState leg, ChasePlan chase) =>
        leg.StepsLeft > 0 && NextStepBps(leg, chase) <= chase.MaxDeviationBps;

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
                        RecordFill(leg, update?.FilledAmount ?? leg.CurrentFilled, update?.AveragePrice);
                        FoldCurrentFill(leg);
                        leg.Dead = true;
                    }
                    else if (update.IsFilled)
                    {
                        RecordFill(leg, update.FilledAmount, update.AveragePrice);
                        FoldCurrentFill(leg);
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

            if (!leg.Dead && !leg.Done && leg.OrderId is not null)
            {
                // заявку так и не сняли: она может исполниться сама и создать экспозицию вне
                // всякого учёта — не теряем её, цикл ведения продолжит отмену каждый тик
                lock (_strayOrders)
                {
                    _strayOrders.Add(new StrayOrder(leg.Connector, leg.OrderId, leg.Request.Symbol, leg.Request.Side));
                }

                log.Error($"[{leg.Connector.DisplayName}] {leg.Request.Symbol} {leg.Request.Side} × {Formatting.Volume(leg.Request.Amount)}: заявку снять не удалось — она остаётся в стакане, её отменой займётся цикл ведения");
            }
        }

        _ = ct; // откат выполняется по CancellationToken.None: он важнее выхода из процесса
        return (residuals[0], residuals[1]);
    }

    // ------------------------- политика исполнения -------------------------

    /// <summary>
    /// Развернуть политику исполнения для биржи: глобальный Arbitrage:Execution,
    /// поверх — переопределение из exchanges.json, поверх — возможности биржи
    /// (неподдерживаемое понижается с пояснением, которое показывается один раз).
    /// </summary>
    private OrderPolicy ResolvePolicy(IExchangeConnector connector, ArbitrageOptions options, bool close)
    {
        var entry = config.Current.Exchanges.Items.FirstOrDefault(
            e => string.Equals(e.Id, connector.Id, StringComparison.OrdinalIgnoreCase));
        var caps = ExchangeCapabilityMap.For(connector.Id);

        var policy = close
            ? OrderPolicyResolver.ResolveClose(options.Execution, entry?.Execution, caps)
            : OrderPolicyResolver.ResolveEntry(options.Execution, entry?.Execution, caps);

        foreach (var note in policy.Notes)
        {
            if (_policyNotes.Add($"{connector.Id}|{note}"))
            {
                log.Warning($"[{connector.DisplayName}] {note}");
            }
        }

        return policy;
    }

    /// <summary>
    /// Собрать состояние ноги: цена со смещением LimitOffsetBps от котировки, тип заявки
    /// (ChaseLimit с локальным догонанием на бирже выглядит как обычный limit) и бюджет шагов.
    /// </summary>
    private LegState BuildLeg(IExchangeConnector connector, OrderPolicy policy, string symbol, OrderSide side, decimal referencePrice, decimal amount, bool reduceOnly = false)
    {
        var price = policy.RequiresPrice ? OffsetPrice(referencePrice, side, policy.LimitOffsetBps) : (decimal?)null;

        // на биржу уходит то, что она исполняет: локальное догонание — это обычные limit-заявки,
        // нативный chase остаётся ChaseLimit (коннектор добавит к ним параметры биржи)
        var requestType = policy.ChasesLocally ? OrderType.Limit : policy.Type;

        return new LegState
        {
            Connector = connector,
            Policy = policy,
            TargetAmount = amount,
            StartPrice = price ?? referencePrice,
            QuotePrice = price ?? referencePrice,
            Request = new OrderRequest(
                symbol,
                side,
                amount,
                ReduceOnly: reduceOnly,
                Type: requestType,
                Price: price,
                TimeInForce: policy.TimeInForce,
                ExchangeParams: policy.ExchangeParams),
            StepsTotal = policy.ChasesLocally && policy.Chase is { } plan ? plan.MaxSteps : 0,
            StepsLeft = policy.ChasesLocally && policy.Chase is { } budget ? budget.MaxSteps : 0,
            LastQuoteAt = time.GetUtcNow(),
        };
    }

    /// <summary>
    /// Цена заявки со смещением от котировки: BUY — выше (агрессивнее, исполняется вероятнее),
    /// SELL — ниже. Смещение в bps (1 bps = 0.01 %).
    /// </summary>
    private static decimal OffsetPrice(decimal reference, OrderSide side, decimal offsetBps)
    {
        if (offsetBps == 0m)
        {
            return reference;
        }

        var sign = side == OrderSide.Buy ? 1m : -1m;
        return Math.Round(reference * (1m + sign * offsetBps / 10_000m), 8, MidpointRounding.AwayFromZero);
    }

    /// <summary>Человекочитаемое описание заявки ноги для журнала.</summary>
    private static string DescribeLeg(OrderType type, OrderSide side) => type switch
    {
        OrderType.Market => $"MARKET {side}",
        OrderType.ChaseLimit => $"CHASE-LIMIT {side}",
        _ => $"LIMIT {side}",
    };

    /// <summary>Описание заявки ноги с объёмом и ценой для строки журнала.</summary>
    private static string EntryDescription(OrderPolicy policy, OrderSide side, decimal price, decimal amount) =>
        policy.RequiresPrice
            ? $"{DescribeLeg(policy.Type, side)} {Formatting.Volume(amount)} @ {Formatting.Price(price)}"
            : $"{DescribeLeg(policy.Type, side)} {Formatting.Volume(amount)}";

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

    // ------------------------- ребалансировка -------------------------

    /// <summary>
    /// Порог допустимого перекоса ног: максимум из настроенного процента от большего
    /// объёма и минимального лота биржи, которую придётся урезать (меньше лота ордер не встанет).
    /// </summary>
    private decimal RebalanceTolerance(PositionPair position, ArbitrageOptions options)
    {
        var cutLong = position.Imbalance > 0m;
        var exchangeId = cutLong ? position.LongExchangeId : position.ShortExchangeId;
        var connector = registry.Connectors.FirstOrDefault(c => c.Id == exchangeId);
        var minLot = connector is null ? 0m : FindMarket(connector, position.Symbol)?.MinAmount ?? 0m;
        var percent = Math.Max(position.LongSize, position.ShortSize) * options.RebalanceTolerancePercent / 100m;
        return Math.Max(percent, minLot);
    }

    /// <summary>
    /// Урезает бóльшую ногу reduceOnly-заявкой по текущему bid/ask до выравнивания ног.
    /// Тип заявки, смещение цены и догонание — те же, что для входа (Execution): позиция
    /// здесь не закрывается, а перебалансировывается, поэтому политика входная.
    /// Частичное исполнение фиксируется; остаток добирается на следующем тике.
    /// </summary>
    private async Task RebalanceAsync(PositionPair position, ArbitrageOptions options, CancellationToken ct)
    {
        var cutLong = position.Imbalance > 0m;
        var exchangeId = cutLong ? position.LongExchangeId : position.ShortExchangeId;
        var connector = registry.Connectors.FirstOrDefault(c => c.Id == exchangeId);
        var ticker = FindTicker(exchangeId, position.Symbol);
        if (connector is null || ticker is null || !ticker.IsTradable)
        {
            return;
        }

        var delta = Math.Abs(position.Imbalance);
        var price = cutLong ? ticker.Bid : ticker.Ask;
        if (price <= 0m)
        {
            return;
        }

        var side = cutLong ? OrderSide.Sell : OrderSide.Buy;
        var policy = ResolvePolicy(connector, options, close: false);
        var leg = BuildLeg(connector, policy, position.Symbol, side, price, delta, reduceOnly: true);
        var quote = leg.Request.Price ?? price;

        log.Info($"{position.Symbol}: ребаланс — урезаю {(cutLong ? "лонг" : "шорт")} на {Formatting.Volume(delta)} @ {Formatting.Price(quote)} (ноги {Formatting.Volume(position.LongSize)}/{Formatting.Volume(position.ShortSize)})");
        if (!await PlaceAsync(leg, ct))
        {
            return; // отказ — повторим на следующем тике
        }

        await WaitForLegsAsync([leg], options, ct);

        // по таймауту снимаем заявку и перечитываем финальное исполнение (гонка отмены и исполнения)
        if (!leg.Done && !leg.Dead && leg.OrderId is not null)
        {
            await connector.CancelOrderAsync(leg.OrderId, position.Symbol, CancellationToken.None);
            var update = await connector.FetchOrderAsync(leg.OrderId, position.Symbol, CancellationToken.None);
            if (update is not null)
            {
                RecordFill(leg, update.FilledAmount, update.AveragePrice);
            }
        }

        var cut = Math.Min(leg.Filled, delta);
        if (cut <= 0m)
        {
            log.Warning($"{position.Symbol}: ребаланс не исполнился{(leg.Error is null ? string.Empty : $" ({leg.Error})")} — повторю на следующем тике");
            return;
        }

        if (cutLong)
        {
            position.LongSize = Math.Max(0m, position.LongSize - cut);
        }
        else
        {
            position.ShortSize = Math.Max(0m, position.ShortSize - cut);
        }

        AccruedClosedLeg(position, connector, isLong: cutLong, cut, leg.AveragePrice ?? price);
        log.Success($"{position.Symbol}: ребаланс — урезано {(cutLong ? "лонг" : "шорт")} на {Formatting.Volume(cut)}, ноги {Formatting.Volume(position.LongSize)}/{Formatting.Volume(position.ShortSize)}");

        if (position.LongSize <= 0m && position.ShortSize <= 0m)
        {
            // обрезали остатки откатов до нуля — позиция фактически закрыта
            await CloseAsync(position, CloseReason.Rollback, ct);
        }
    }

    /// <summary>
    /// Начисляет реализованный PnL и комиссию закрытия на урезанный/закрытый фрагмент ноги.
    /// </summary>
    private static void AccruedClosedLeg(PositionPair position, IExchangeConnector connector, bool isLong, decimal quantity, decimal exitPrice)
    {
        if (quantity <= 0m)
        {
            return;
        }

        var entry = isLong ? position.EntryLong : position.EntryShort;
        var pnl = quantity * (isLong ? exitPrice - entry : entry - exitPrice);
        position.RealizedPnlUsd = (position.RealizedPnlUsd ?? 0m) + pnl;
        position.FeesExitUsd += quantity * exitPrice * TakerPercent(connector, position.Symbol) / 100m;

        if (isLong)
        {
            position.ClosedLongVolume += quantity;
        }
        else
        {
            position.ClosedShortVolume += quantity;
        }
    }

    /// <summary>Taker-комиссия биржи по символу (процентом).</summary>
    private static decimal TakerPercent(IExchangeConnector connector, string symbol) =>
        connector.TryGetFees(symbol, out var fees) ? fees.TakerPercent : connector.DefaultFees.TakerPercent;

    // ------------------------- закрытие -------------------------

    /// <summary>
    /// Закрыть ногу по политике Execution.Close: market — сразу, limit — заявка с ожиданием
    /// и снятием остатка по таймауту (остаток закрывается на следующем тике цикла ведения).
    /// Возвращает (закрытый объём, средняя цена, ошибка); объём 0 — нога не закрыта.
    /// </summary>
    private async Task<(decimal Closed, decimal? AveragePrice, string? Error)> CloseLegAsync(
        PositionPair position, IExchangeConnector connector, bool isLong, decimal size, decimal? reference, ArbitrageOptions options, CancellationToken ct)
    {
        var side = isLong ? OrderSide.Sell : OrderSide.Buy;
        var policy = ResolvePolicy(connector, options, close: true);

        if (policy.RequiresPrice && reference is not > 0m)
        {
            // лимитную заявку выставить некуда (нет котировки) — страховка: закрываем market
            log.Warning($"[{connector.DisplayName}] {position.Symbol}: нет котировки для лимитного закрытия — закрываю market");
            policy = OrderPolicy.MarketOrder;
        }

        var leg = BuildLeg(connector, policy, position.Symbol, side, reference ?? 0m, size, reduceOnly: true);
        if (!await PlaceAsync(leg, ct))
        {
            return (0m, null, leg.Error ?? "заявка отклонена");
        }

        // market-заявку не опрашиваем: она обязана исполниться сразу (как и раньше в закрытии)
        if (!leg.Done && policy.Type != OrderType.Market)
        {
            await WaitForLegsAsync([leg], options, ct);

            if (!leg.Done && !leg.Dead && leg.OrderId is not null)
            {
                await connector.CancelOrderAsync(leg.OrderId, position.Symbol, CancellationToken.None);
                var update = await connector.FetchOrderAsync(leg.OrderId, position.Symbol, CancellationToken.None);
                if (update is not null)
                {
                    RecordFill(leg, update.FilledAmount, update.AveragePrice);
                }
            }
        }

        var closed = Math.Min(leg.Filled, size);
        if (closed <= 0m && policy.Type == OrderType.Market)
        {
            // Раньше «market без объёма в ответе» засчитывался исполненным целиком: если заявка
            // на деле не прошла, позиция слетала с учёта, а на бирже оставалась живая нога —
            // и слот лимита освобождался под новый арбитраж поверх старого. Теперь исполнение
            // подтверждается статусом заявки, а при нуле — отсутствием позиции на бирже.
            var update = leg.OrderId is not null
                ? await connector.FetchOrderAsync(leg.OrderId, position.Symbol, ct)
                : null;
            if (update is not null)
            {
                RecordFill(leg, update.FilledAmount, update.AveragePrice);
                closed = Math.Min(leg.Filled, size);
            }

            if (closed <= 0m && update is { IsFilled: true })
            {
                closed = size; // статус «исполнен», но объёма биржа не вернула — доверяем статусу
            }

            if (closed <= 0m)
            {
                var exposureSide = side == OrderSide.Sell ? OrderSide.Buy : OrderSide.Sell;
                if (await HasExchangePositionAsync(connector, exposureSide, position.Symbol, ct))
                {
                    return (0m, leg.AveragePrice, update is null
                        ? "рыночная заявка не подтверждена биржей"
                        : $"рыночная заявка не исполнена (статус: {update.Status})");
                }

                closed = size; // позиции на бирже нет — нога действительно закрыта
            }
        }

        return (closed, leg.AveragePrice, leg.Error);
    }

    /// <summary>
    /// Закрывает каждую ногу по её фактическому объёму (по умолчанию — market reduceOnly,
    /// настраивается разделом Execution:Close). Если нога не закрылась — позиция остаётся
    /// под символом, незакрытая нога будет повторена на следующем тике; «голой» экспозиции
    /// бот не теряет.
    /// </summary>
    private async Task CloseAsync(PositionPair position, CloseReason reason, CancellationToken ct)
    {
        var options = config.Current.Arbitrage;
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
            var simExitLong = longTicker?.Bid ?? position.EntryLong;
            var simExitShort = shortTicker?.Ask ?? position.EntryShort;
            position.ExitLong = simExitLong;
            position.ExitShort = simExitShort;
            AccruedClosedLeg(position, longConnector, isLong: true, position.LongSize, simExitLong);
            AccruedClosedLeg(position, shortConnector, isLong: false, position.ShortSize, simExitShort);
            position.LongClosed = true;
            position.ShortClosed = true;
        }
        else
        {
            if (!position.LongClosed)
            {
                if (position.LongSize <= 0m)
                {
                    position.LongClosed = true;
                }
                else
                {
                    var (closedQty, exitPrice, closeError) = await CloseLegAsync(
                        position, longConnector, isLong: true, position.LongSize, longTicker?.Bid, options, ct);
                    if (closedQty <= 0m)
                    {
                        log.Error($"[{longConnector.DisplayName}] закрытие лонга {position.Symbol} не удалось: {closeError ?? "заявка не исполнена"} — повторю на следующем тике");
                        return; // позиция остаётся в учёте, закроем позже
                    }

                    var exitLong = exitPrice ?? longTicker?.Bid ?? position.EntryLong;
                    AccruedClosedLeg(position, longConnector, isLong: true, closedQty, exitLong);
                    position.LongSize = Math.Max(0m, position.LongSize - closedQty);
                    position.ExitLong = exitLong;
                    position.LongClosed = position.LongSize <= 0m;
                    if (!position.LongClosed)
                    {
                        log.Warning($"{position.Symbol}: лонг закрыт частично, остаток {Formatting.Volume(position.LongSize)} — добью на следующем тике");
                    }
                }
            }

            if (!position.ShortClosed)
            {
                if (position.ShortSize <= 0m)
                {
                    position.ShortClosed = true;
                }
                else
                {
                    var (closedQty, exitPrice, closeError) = await CloseLegAsync(
                        position, shortConnector, isLong: false, position.ShortSize, shortTicker?.Ask, options, ct);
                    if (closedQty <= 0m)
                    {
                        // лонг уже закрыт — шорт остаётся голым, но позиция не теряется:
                        // цикл ведения повторит закрытие на каждом следующем тике
                        log.Error($"[{shortConnector.DisplayName}] закрытие шорта {position.Symbol} не удалось: {closeError ?? "заявка не исполнена"} — позиция остаётся в учёте, повторю на следующем тике");
                        return;
                    }

                    var exitShort = exitPrice ?? shortTicker?.Ask ?? position.EntryShort;
                    AccruedClosedLeg(position, shortConnector, isLong: false, closedQty, exitShort);
                    position.ShortSize = Math.Max(0m, position.ShortSize - closedQty);
                    position.ExitShort = exitShort;
                    position.ShortClosed = position.ShortSize <= 0m;
                    if (!position.ShortClosed)
                    {
                        log.Warning($"{position.Symbol}: шорт закрыт частично, остаток {Formatting.Volume(position.ShortSize)} — добью на следующем тике");
                    }
                }
            }

            if (!position.LongClosed || !position.ShortClosed)
            {
                return; // одна нога ещё жива — финализируем, когда закроются обе
            }
        }

        position.Reason = reason;
        position.ClosedAt = time.GetUtcNow();
        position.Status = PositionStatus.Closed;

        // накопленный по ногам PnL минус комиссии входа и выхода
        position.RealizedPnlUsd = (position.RealizedPnlUsd ?? 0m) - position.FeesEntryUsd - position.FeesExitUsd;

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
        => size * (longPrice * TakerPercent(longConnector, symbol) + shortPrice * TakerPercent(shortConnector, symbol)) / 100m;

    private Dictionary<string, int> CountPerExchange()
    {
        Dictionary<string, int> counts = new(StringComparer.Ordinal);
        HashSet<string> trackedSymbols;
        lock (_positions)
        {
            trackedSymbols = new HashSet<string>(_positions.Keys, StringComparer.Ordinal);
            foreach (var position in _positions.Values)
            {
                counts[position.LongExchangeId] = counts.GetValueOrDefault(position.LongExchangeId) + 1;
                counts[position.ShortExchangeId] = counts.GetValueOrDefault(position.ShortExchangeId) + 1;
            }
        }

        // экспозиция вне сеанса тоже занимает слоты биржи; символы своих позиций не считаем дважды
        lock (_externalExposure)
        {
            foreach (var (exchangeId, symbols) in _externalExposure)
            {
                var extra = symbols.Count(symbol => !trackedSymbols.Contains(symbol));
                if (extra > 0)
                {
                    counts[exchangeId] = counts.GetValueOrDefault(exchangeId) + extra;
                }
            }
        }

        return counts;
    }

    /// <summary>Число «внешних» арбитражей: символ с ненулевой экспозицией сразу на 2+ биржах.</summary>
    private int CountExternalArbitrages()
    {
        HashSet<string> trackedSymbols;
        lock (_positions)
        {
            trackedSymbols = new HashSet<string>(_positions.Keys, StringComparer.Ordinal);
        }

        lock (_externalExposure)
        {
            var legsPerSymbol = new Dictionary<string, int>(StringComparer.Ordinal);
            foreach (var symbols in _externalExposure.Values)
            {
                foreach (var symbol in symbols)
                {
                    if (trackedSymbols.Contains(symbol))
                    {
                        continue; // наша же позиция: её слот уже учтён как сеансовая
                    }

                    legsPerSymbol[symbol] = legsPerSymbol.GetValueOrDefault(symbol) + 1;
                }
            }

            return legsPerSymbol.Values.Count(legs => legs >= 2);
        }
    }

    /// <summary>
    /// Сверка с биржами: лимиты MaxOpenPositions/MaxPositionsPerExchange должны учитывать
    /// фактическую экспозицию, а не только позиции текущего сеанса — иначе после рестарта
    /// (или потерянной позиции) бот при лимите 1 открывал «ещё одну» поверх живой.
    /// Символы позиций под ведением исключаются, чтобы не считать их дважды.
    /// </summary>
    private async Task ReconcileExternalExposureAsync(CancellationToken ct)
    {
        if (config.Current.General.NetworkMode == NetworkMode.DryRun)
        {
            return; // симуляция не выставляет заявок на биржу — сверяться не с чем
        }

        var now = time.GetUtcNow();
        if (_lastReconcileAt != default && now - _lastReconcileAt < ReconcileInterval)
        {
            return; // не чаще раза в минуту: fetchPositions — приватный запрос на биржу
        }

        _lastReconcileAt = now;

        // снимок прошлой сверки: при сбое запроса сохраняем известную экспозицию —
        // слоты лимитов не должны «освобождаться» из-за сетевой недоступности биржи
        Dictionary<string, HashSet<string>> previous;
        lock (_externalExposure)
        {
            previous = _externalExposure.ToDictionary(
                kv => kv.Key,
                kv => new HashSet<string>(kv.Value, StringComparer.Ordinal),
                StringComparer.Ordinal);
        }

        var fresh = new Dictionary<string, HashSet<string>>(StringComparer.Ordinal);
        foreach (var connector in registry.Connectors)
        {
            try
            {
                var positions = await connector.FetchPositionsAsync(ct);
                var symbols = positions
                    .Where(p => p.Amount > 0m)
                    .Select(p => p.Symbol)
                    .ToHashSet(StringComparer.Ordinal);

                if (symbols.Count > 0)
                {
                    fresh[connector.Id] = symbols;
                }
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch (Exception ex)
            {
                if (_reconcileFailWarned.Add(connector.Id))
                {
                    log.Warning($"[{connector.DisplayName}] сверка позиций недоступна: {ex.Message} — лимиты считаются только по позициям сеанса");
                }

                logger.LogDebug(ex, "FetchPositions reconcile failed on {ExchangeId}", connector.Id);
                if (previous.TryGetValue(connector.Id, out var stale))
                {
                    fresh[connector.Id] = stale; // консервативно: помним прошлый результат
                }
            }
        }

        lock (_positions)
        {
            foreach (var position in _positions.Values)
            {
                if (fresh.TryGetValue(position.LongExchangeId, out var longs))
                {
                    longs.Remove(position.Symbol);
                }

                if (fresh.TryGetValue(position.ShortExchangeId, out var shorts))
                {
                    shorts.Remove(position.Symbol);
                }
            }
        }

        bool report;
        lock (_externalExposure)
        {
            report = _externalExposure.Count == 0 && fresh.Any(kv => kv.Value.Count > 0);
            _externalExposure.Clear();
            foreach (var (exchangeId, symbols) in fresh)
            {
                if (symbols.Count > 0)
                {
                    _externalExposure[exchangeId] = symbols;
                }
            }
        }

        if (report)
        {
            var description = string.Join("; ", fresh
                .Where(kv => kv.Value.Count > 0)
                .OrderBy(kv => kv.Key, StringComparer.Ordinal)
                .Select(kv => $"{kv.Key}: {string.Join(", ", kv.Value.OrderBy(s => s, StringComparer.Ordinal))}"));
            log.Warning($"На биржах найдены позиции вне учёта этого сеанса ({description}) — они занимают слоты лимитов MaxOpenPositions/MaxPositionsPerExchange");
        }
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

    /// <summary>
    /// Есть ли у биржи фактическая позиция ноги (сторона экспозиции — противоположная закрытию).
    /// Сбой сверки трактуем как «позиция есть»: пусть лучше закрытие повторится на следующем
    /// тике, чем живая позиция потеряется из учёта.
    /// </summary>
    private static async Task<bool> HasExchangePositionAsync(IExchangeConnector connector, OrderSide exposureSide, string symbol, CancellationToken ct)
    {
        try
        {
            var positions = await connector.FetchPositionsAsync(ct);
            return positions.Any(p => p.Symbol == symbol && p.Side == exposureSide && p.Amount > 0m);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception)
        {
            return true;
        }
    }

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
