using System.ComponentModel.DataAnnotations;

namespace FuturesArbBot.Core.Domain;

/// <summary>Корневой контейнер всех настроек робота.</summary>
public sealed class BotOptions
{
    [Required]
    public GeneralOptions General { get; set; } = new();

    [Required]
    public ArbitrageOptions Arbitrage { get; set; } = new();

    [Required]
    public SymbolsOptions Symbols { get; set; } = new();

    [Required]
    public ExchangesOptions Exchanges { get; set; } = new();
}

/// <summary>Общие настройки приложения (appsettings.json → General).</summary>
public sealed class GeneralOptions
{
    /// <summary>DryRun | Testnet | Live.</summary>
    [EnumDataType(typeof(NetworkMode))]
    public NetworkMode NetworkMode { get; set; } = NetworkMode.DryRun;

    /// <summary>Период опроса бирж, мс.</summary>
    [Range(500, 60_000)]
    public int RefreshIntervalMs { get; set; } = 2_000;

    /// <summary>Писать журнал сеанса в файл каталога logs/.</summary>
    public bool WriteLogFile { get; set; } = true;

    public UiOptions ConsoleUi { get; set; } = new();
}

/// <summary>Настройки консольного интерфейса.</summary>
public sealed class UiOptions
{
    /// <summary>Период перерисовки дашборда, мс.</summary>
    [Range(100, 5_000)]
    public int RefreshMs { get; set; } = 400;

    /// <summary>Строк журнала в панели дашборда.</summary>
    public int LogLines
    {
        get => field;
        set => field = value is > 0 and <= 50 ? value : 9;
    } = 9;

    /// <summary>Сколько лучших возможностей показывать в таблице.</summary>
    [Range(3, 50)]
    public int TopRows { get; set; } = 12;
}

/// <summary>Настройки арбитража (appsettings.json → Arbitrage).</summary>
public sealed class ArbitrageOptions
{
    /// <summary>Включить исполнение сделок. false — режим чистого мониторинга.</summary>
    public bool Enabled { get; set; } = false;

    /// <summary>Страховка: реальная торговля в режиме Live требует и Enabled, и AllowLive.</summary>
    public bool AllowLive { get; set; } = false;

    /// <summary>
    /// Порог входа «вверх»: минимальный нетто-спред (в %), при превышении которого
    /// открывается арбитражная позиция (лонг на дешёвой бирже, шорт на дорогой).
    /// </summary>
    [Range(0, 100)]
    public decimal MinSpreadPercentUp { get; set; } = 0.4m;

    /// <summary>
    /// Порог закрытия «вниз»: спред (в %), при сжатии до которого позиция
    /// закрывается с фиксацией прибыли. Должен быть меньше порога входа.
    /// </summary>
    [Range(0, 100)]
    public decimal MinSpreadPercentDown { get; set; } = 0.15m;

    /// <summary>Стоп-лосс: спред (в %), при расширении до которого позиция закрывается в убыток.</summary>
    [Range(0, 100)]
    public decimal StopLossSpreadPercent { get; set; } = 2.5m;

    /// <summary>Учитывать комиссии бирж (taker) в расчёте нетто-спреда.</summary>
    public bool IncludeFees { get; set; } = true;

    /// <summary>Защитный буфер на проскальзывание, % от оборота.</summary>
    [Range(0, 10)]
    public decimal SlippageBufferPercent { get; set; } = 0.03m;

    /// <summary>
    /// Санитарный потолок спреда, %: расхождения выше него считаются аномалией
    /// (новые листинги, преп маркеты, разные индексы цены) и игнорируются.
    /// </summary>
    [Range(0.5, 1000)]
    public decimal MaxSpreadPercent { get; set; } = 5m;

    /// <summary>
    /// Таймаут (мс) ожидания полного исполнения limit-ног при открытии.
    /// По истечении — неисполненные ноги отменяются, набранный объём откатывается.
    /// </summary>
    [Range(200, 600_000)]
    public int OrderExecutionTimeoutMs { get; set; } = 3_000;

    /// <summary>
    /// Допустимый перекос объёмов ног (% от большего объёма), при котором позиция
    /// считается сбалансированной. Сверх порога — бóльшая нога урезается reduceOnly limit-ордером.
    /// Фактический порог не меньше минимального лота биржи.
    /// </summary>
    [Range(0.05, 25)]
    public decimal RebalanceTolerancePercent { get; set; } = 1.0m;

    /// <summary>Период (мс) опроса статусов limit-ордеров при ожидании исполнения.</summary>
    [Range(50, 10_000)]
    public int OrderPollIntervalMs { get; set; } = 200;

    /// <summary>Номинал одного арбитража в USDT (на каждую ногу).</summary>
    [Range(1, 10_000_000)]
    public decimal OrderSizeUsd { get; set; } = 100m;

    /// <summary>Плечо (пытается выставиться на бирже; 1 — без плеча).</summary>
    [Range(1, 50)]
    public int Leverage { get; set; } = 1;

    /// <summary>Максимум одновременно открытых арбитражей.</summary>
    [Range(1, 100)]
    public int MaxOpenPositions { get; set; } = 4;

    /// <summary>Максимум позиций на одной бирже.</summary>
    [Range(1, 100)]
    public int MaxPositionsPerExchange { get; set; } = 4;

    /// <summary>Максимальный возраст позиции, минуты (после — принудительное закрытие).</summary>
    [Range(5, 100_000)]
    public int MaxPositionAgeMinutes { get; set; } = 720;

    /// <summary>Закрывать открытые позиции при остановке робота (Ctrl+C).</summary>
    public bool ClosePositionsOnExit { get; set; } = true;
}

/// <summary>Настройки торговых пар (symbols.json → Symbols).</summary>
public sealed class SymbolsOptions
{
    /// <summary>Котируемые валюты (обычно USDT).</summary>
    public List<string> QuoteCurrencies { get; set; } = ["USDT"];

    /// <summary>Минимальный оборот за 24 часа в USDT — фильтр неликвидных пар.</summary>
    [Range(0, 1e12)]
    public decimal MinQuoteVolume24hUsd { get; set; } = 5_000_000m;

    /// <summary>Белый список шаблонов (если не пуст — торгуем только их). Wildcard: * и ?.</summary>
    public List<string> Include { get; set; } = [];

    /// <summary>Чёрный список шаблонов. Wildcard: * и ?.</summary>
    public List<string> Exclude { get; set; } = [];

    /// <summary>Ограничение количества пар (берутся самые ликвидные). 0 — без лимита.</summary>
    [Range(0, 100_000)]
    public int MaxSymbols { get; set; } = 400;

    /// <summary>Тикер старше этого возраста (сек) считается устаревшим.</summary>
    [Range(1, 600)]
    public int MaxTickerAgeSeconds { get; set; } = 15;
}

/// <summary>Список бирж (exchanges.json → Exchanges).</summary>
public sealed class ExchangesOptions
{
    public List<ExchangeConfigEntry> Items { get; set; } = [];
}

/// <summary>Описание одной биржи в конфигурации.</summary>
public sealed class ExchangeConfigEntry
{
    /// <summary>Идентификатор CCXT: binanceusdm, bybit, okx, gate, mexc, kucoinfutures, bitget, htx…</summary>
    [Required]
    [MinLength(2)]
    public string Id { get; set; } = string.Empty;

    /// <summary>Отображаемое имя (для консоли).</summary>
    public string Name { get; set; } = string.Empty;

    /// <summary>Подключать ли биржу. Чтобы отключить — просто false.</summary>
    public bool Enabled { get; set; } = true;

    /// <summary>API-ключ (для торговли). Можно не задавать — тогда используются переменные окружения ARB_&lt;ID&gt;_API_KEY.</summary>
    public string ApiKey { get; set; } = string.Empty;

    /// <summary>Секрет (для торговли). Аналогично: ARB_&lt;ID&gt;_SECRET.</summary>
    public string Secret { get; set; } = string.Empty;

    /// <summary>Пароль/фраза (нужна, например, OKX). ARB_&lt;ID&gt;_PASSWORD.</summary>
    public string Password { get; set; } = string.Empty;

    /// <summary>Переопределение комиссии taker в % (иначе берётся с биржи).</summary>
    [Range(0, 10)]
    public decimal? TakerFeePercent { get; set; }

    /// <summary>Переопределение комиссии maker в %.</summary>
    [Range(0, 10)]
    public decimal? MakerFeePercent { get; set; }

    /// <summary>Дополнительные опции CCXT для конкретной биржи.</summary>
    public Dictionary<string, object>? Options { get; set; }
}
