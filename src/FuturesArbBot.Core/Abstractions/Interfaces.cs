namespace FuturesArbBot.Core.Abstractions;

/// <summary>Доступ к конфигурации (горячая перечитка JSON при изменении).</summary>
public interface IConfigProvider
{
    BotOptions Current { get; }

    /// <summary>Каталог, из которого загружены файлы конфигурации.</summary>
    string ConfigDirectory { get; }
}

/// <summary>Журнал событий: кольцевой буфер в памяти + опциональный файл.</summary>
public interface IEventLog
{
    void Log(AppLogLevel level, string message);

    /// <summary>Последние события (для дашборда).</summary>
    IReadOnlyList<LogEvent> Latest(int count);

    /// <summary>События с номером больше afterId (для консольного потока).</summary>
    IReadOnlyList<LogEvent> Tail(long afterId, int max);

    /// <summary>Последние ошибки.</summary>
    IReadOnlyList<LogEvent> Errors(int count);
}

/// <summary>Расчёт нетто-спреда между двумя биржами с учётом комиссий.</summary>
public interface ISpreadCalculator
{
    SpreadEstimate? Calculate(TickerSnapshot cheaper, TickerSnapshot richer, ExchangeFees cheapFees, ExchangeFees richFees);
}

/// <summary>Фильтр торговых пар (квотиры, объём, include/exclude).</summary>
public interface ISymbolFilter
{
    bool IsAllowed(MarketInfo market, decimal quoteVolume24h);
}

/// <summary>Абстракция над конкретной биржей (в проекте реализована через CCXT).</summary>
public interface IExchangeConnector : IAsyncDisposable
{
    string Id { get; }

    string DisplayName { get; }

    NetworkMode Mode { get; }

    /// <summary>Комиссии по умолчанию (если по символу не найдены).</summary>
    ExchangeFees DefaultFees { get; }

    int MarketCount { get; }

    /// <summary>Все линейные бессрочные фьючерсы биржи (до фильтрации).</summary>
    IReadOnlyList<MarketInfo> PerpetualMarkets { get; }

    Task ConnectAsync(CancellationToken ct = default);

    Task<FetchTickersResult> FetchTickersAsync(CancellationToken ct = default);

    /// <summary>Выставить ордер (market или limit — по <see cref="OrderRequest.Type"/>).</summary>
    Task<OrderResult> PlaceOrderAsync(OrderRequest request, CancellationToken ct = default);

    /// <summary>Актуальное состояние ордера; null — ордер не найден.</summary>
    Task<OrderUpdate?> FetchOrderAsync(string orderId, string symbol, CancellationToken ct = default);

    /// <summary>Фактические открытые позиции биржи — для сверки лимитов с реальностью.</summary>
    Task<IReadOnlyList<PositionSnapshot>> FetchPositionsAsync(CancellationToken ct = default);

    /// <summary>
    /// Актуальные фандинг-рейты биржи: symbol → ставка в % за период (отрицательная — лонги получают).
    /// Пустой словарь — биржа не отдаёт данные; реализация кэширует ответ (рейты меняются редко).
    /// </summary>
    Task<IReadOnlyDictionary<string, decimal>> FetchFundingRatesPercentAsync(CancellationToken ct = default);

    /// <summary>Отменить ордер. false — отмена не прошла (например, уже исполнен).</summary>
    Task<bool> CancelOrderAsync(string orderId, string symbol, CancellationToken ct = default);

    Task SetLeverageAsync(int leverage, string symbol, CancellationToken ct = default);

    /// <summary>Проверка торгового доступа (валидность ключей) через приватный запрос.</summary>
    Task<bool> VerifyAccessAsync(CancellationToken ct = default);

    bool TryGetFees(string symbol, out ExchangeFees fees);
}

/// <summary>Фабрика биржевых коннекторов.</summary>
public interface IExchangeFactory
{
    /// <summary>Идентификаторы бирж, встроенные в фабрику.</summary>
    IReadOnlyList<string> SupportedIds { get; }

    IExchangeConnector Create(ExchangeConfigEntry entry, NetworkMode mode);
}

/// <summary>Исполнитель арбитражных сделок (открытие/ведение/закрытие позиций).</summary>
public interface ITradeExecutor
{
    bool HasOpenPositions { get; }

    Task ProcessOpportunitiesAsync(IReadOnlyList<SpreadEstimate> candidates, CancellationToken ct);

    Task ManageOpenPositionsAsync(IReadOnlyDictionary<string, IReadOnlyDictionary<string, TickerSnapshot>> tickersByExchange, CancellationToken ct);

    Task CloseAllAsync(CloseReason reason, CancellationToken ct);
}

/// <summary>Сборщик статистики сеанса.</summary>
public interface IStatisticsCollector
{
    void RecordScanTick(int tickersFetched);

    void RecordOpportunity(SpreadEstimate estimate);

    void RecordRequest(string exchangeId, TimeSpan latency, bool success);

    void RecordMarkets(string exchangeId, string displayName, int count, ExchangeFees fees, NetworkMode mode);

    void RecordOpened(PositionPair position);

    void RecordOpenFailed(string symbol, string errorMessage);

    void RecordClosed(PositionPair position);

    SessionReport Snapshot(DateTimeOffset endedAt);

    DashboardSnapshot BuildDashboard(IReadOnlyList<SpreadEstimate> top, int onlineExchanges, int trackedSymbols);
}

/// <summary>Цикл сканирования возможностей.</summary>
public interface IArbitrageScanner
{
    Task RunAsync(CancellationToken ct);

    /// <summary>Одна итерация сканирования (используется режимом --once).</summary>
    Task ScanOnceAsync(CancellationToken ct);

    DashboardSnapshot? Snapshot { get; }
}
