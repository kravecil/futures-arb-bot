namespace FuturesArbBot.Core.Domain;

/// <summary>
/// Что биржа умеет по части заявок. Заполняется один раз при старте
/// (см. <see cref="ExchangeCapabilityMap"/>) и участвует в развёртывании
/// политики исполнения: неподдерживаемые настройки понижаются до безопасного
/// варианта с пояснением в <see cref="OrderPolicy.Notes"/>.
/// </summary>
public sealed record ExchangeCapabilities(
    string Id,
    bool SupportsLimit,
    bool SupportsCancel,
    bool SupportsFetchOrder,
    // Значение timeInForce для post-only (null — биржа его не понимает).
    string? PostOnlyToken,
    bool SupportsIoc,
    bool SupportsFok,
    bool SupportsNativeChase,
    // Параметры нативного chase по умолчанию (пусто — требуется явная настройка в конфиге).
    IReadOnlyDictionary<string, string>? NativeChaseDefaults = null)
{
    public bool SupportsPostOnly => PostOnlyToken is not null;

    /// <summary>Локальное догонание требует лимитных заявок, отмены и опроса состояния.</summary>
    public bool SupportsSimulatedChase => SupportsLimit && SupportsCancel && SupportsFetchOrder;
}

/// <summary>
/// Карта возможностей бирж — совпадает со списком <c>CcxtExchangeFactory</c>.
/// Значения взяты из единого интерфейса CCXT (параметры timeInForce/postOnly/reduceOnly):
/// всё, что не подтверждено, помечено как неподдерживаемое, чтобы политика не отправила
/// заявку с параметром, который биржа отвергнет. Таблицу надо сверять при апгрейде CCXT.
/// </summary>
public static class ExchangeCapabilityMap
{
    /// <summary>Неизвестная биржа: базовые возможности есть, экзотические — нет.</summary>
    private static ExchangeCapabilities Generic(string id) => new(
        id,
        SupportsLimit: true,
        SupportsCancel: true,
        SupportsFetchOrder: true,
        PostOnlyToken: null,
        SupportsIoc: false,
        SupportsFok: false,
        SupportsNativeChase: false);

    private static readonly Dictionary<string, ExchangeCapabilities> Map = new(StringComparer.OrdinalIgnoreCase)
    {
        // Binance USDM: timeInForce = GTC|IOC|FOK, post-only в фьючерсном API нет
        ["binanceusdm"] = new("binanceusdm", true, true, true, null, true, true, false),
        ["binance"] = new("binance", true, true, true, null, true, true, false),
        ["bybit"] = new("bybit", true, true, true, "PostOnly", true, true, false),
        ["okx"] = new("okx", true, true, true, "PO", true, true, false),
        ["gate"] = new("gate", true, true, true, "post_only", true, false, false),
        // MEXC Futures ждёт числовые коды timeInForce — строковые токены не отправляем
        ["mexc"] = new("mexc", true, true, true, null, false, false, false),
        // KuCoin Futures: timeInForce = GTC|IOC; chase там есть, но отдельным неявным методом
        // CCXT (contractPrivatePostOrderChaseLimitOrder) — Native требует своих Params и проверки на testnet
        ["kucoinfutures"] = new("kucoinfutures", true, true, true, null, true, false, true),
        ["bitget"] = new("bitget", true, true, true, "post_only", true, true, false),
        ["htx"] = new("htx", true, true, true, null, true, true, false),
        ["coinex"] = new("coinex", true, true, true, null, false, false, false),
        ["phemex"] = new("phemex", true, true, true, null, false, false, false),
        ["krakenfutures"] = new("krakenfutures", true, true, true, null, false, false, false),
    };

    /// <summary>Возможности биржи по идентификатору (неизвестная биржа — базовый набор).</summary>
    public static ExchangeCapabilities For(string id) =>
        Map.TryGetValue(id, out var caps) ? caps : Generic(id);

    /// <summary>Идентификаторы, для которых возможности описаны явно.</summary>
    public static IReadOnlyCollection<string> KnownIds => Map.Keys;
}

/// <summary>
/// Развёрнутый план догонания цены. <see cref="ChaseMode.Simulated"/> — шаги выполняет
/// исполнитель (<c>ArbTradeExecutor</c>), <see cref="ChaseMode.Native"/> — сама биржа.
/// </summary>
public sealed record ChasePlan(
    ChaseMode Mode,
    int MaxSteps,
    int StepIntervalMs,
    decimal StepBps,
    decimal MaxDeviationBps,
    bool FallbackToMarket);

/// <summary>
/// Итоговая политика исполнения заявки на бирже: тип, условия действия, смещение цены
/// и (опционально) план догонания. Строится <see cref="OrderPolicyResolver"/> из
/// конфигурации и возможностей конкретной биржи, поэтому на рынок уходит только то,
/// что биржа точно примет.
/// </summary>
public sealed record OrderPolicy(
    OrderType Type,
    TimeInForce TimeInForce,
    decimal LimitOffsetBps,
    ChasePlan? Chase,
    IReadOnlyDictionary<string, string>? ExchangeParams,
    IReadOnlyList<string> Notes)
{
    /// <summary>Marketable limit — историческое поведение входа по умолчанию.</summary>
    public static OrderPolicy MarketableLimit { get; } = new(OrderType.Limit, TimeInForce.Gtc, 0m, null, null, []);

    /// <summary>Рыночная заявка — поведение закрытия по умолчанию.</summary>
    public static OrderPolicy MarketOrder { get; } = new(OrderType.Market, TimeInForce.Gtc, 0m, null, null, []);

    /// <summary>Нужно ли догонять цену локально (робот переставляет заявку).</summary>
    public bool ChasesLocally => Chase is { Mode: ChaseMode.Simulated };

    /// <summary>Догоняет ли цену биржа (одна заявка с нативными параметрами).</summary>
    public bool ChasesNatively => Chase is { Mode: ChaseMode.Native };

    /// <summary>Заявка с ценой (limit либо chase-limit).</summary>
    public bool RequiresPrice => Type is OrderType.Limit or OrderType.ChaseLimit;
}

/// <summary>
/// Развёртывание настроек <see cref="OrderExecutionOptions"/> в <see cref="OrderPolicy"/>:
/// глобальный раздел → переопределение биржи → возможности биржи (безопасное понижение).
/// Понижение считается один раз здесь, а не в момент выставления заявки.
/// </summary>
public static class OrderPolicyResolver
{
    /// <summary>Значения по умолчанию для ChaseLimit, если поля Execution:Chase не заданы.</summary>
    public const int DefaultMaxSteps = 5;
    public const int DefaultStepIntervalMs = 400;
    public const decimal DefaultStepBps = 5m;
    public const decimal DefaultMaxDeviationBps = 50m;
    public const bool DefaultFallbackToMarket = true;

    /// <summary>Политика входной заявки ноги (по умолчанию — marketable limit, как раньше).</summary>
    public static OrderPolicy ResolveEntry(OrderExecutionOptions? global, OrderExecutionOptions? perExchange, ExchangeCapabilities caps) =>
        Resolve(global, perExchange, caps, isClose: false);

    /// <summary>
    /// Политика закрытия ноги. Наследуется только из секций Close (глобальной и биржевой);
    /// по умолчанию — market, чтобы выход не зависел от готовности биржи дать цену.
    /// </summary>
    public static OrderPolicy ResolveClose(OrderExecutionOptions? global, OrderExecutionOptions? perExchange, ExchangeCapabilities caps) =>
        Resolve(global?.Close, perExchange?.Close, caps, isClose: true);

    private static OrderPolicy Resolve(OrderExecutionOptions? baseSection, OrderExecutionOptions? overSection, ExchangeCapabilities caps, bool isClose)
    {
        var eff = Merge(baseSection, overSection);
        List<string> notes = [];
        var type = eff.Type ?? (isClose ? OrderType.Market : OrderType.Limit);
        var offsetBps = eff.LimitOffsetBps ?? 0m;

        if (type is OrderType.Limit or OrderType.ChaseLimit && !caps.SupportsLimit)
        {
            notes.Add($"биржа {caps.Id} не принимает лимитные заявки — исполнение market");
            type = OrderType.Market;
        }

        // для market условия действия не имеют смысла — не тащим их в params биржи
        var timeInForce = type == OrderType.Market
            ? TimeInForce.Gtc
            : DegradeTimeInForce(eff.TimeInForce ?? TimeInForce.Gtc, caps, notes);

        ChasePlan? chase = null;
        IReadOnlyDictionary<string, string>? exchangeParams = null;

        if (type == OrderType.ChaseLimit)
        {
            if (isClose)
            {
                notes.Add("догонание цены на закрытии позиции не допускается — закрытие market");
                type = OrderType.Market;
            }
            else
            {
                var mode = ResolveChaseMode(eff, caps, notes);

                if (mode == ChaseMode.None)
                {
                    type = OrderType.Limit;
                }
                else
                {
                    var step = eff.StepBps ?? DefaultStepBps;
                    var maxDeviation = eff.MaxDeviationBps ?? DefaultMaxDeviationBps;
                    if (maxDeviation < step)
                    {
                        maxDeviation = step; // бюджет меньше одного шага — догонание выродится в одну перестановку
                    }

                    chase = new ChasePlan(
                        mode,
                        eff.MaxSteps ?? DefaultMaxSteps,
                        eff.StepIntervalMs ?? DefaultStepIntervalMs,
                        step,
                        maxDeviation,
                        eff.FallbackToMarket ?? DefaultFallbackToMarket);

                    if (mode == ChaseMode.Native)
                    {
                        exchangeParams = eff.Params ?? caps.NativeChaseDefaults;
                        var keys = string.Join(", ", exchangeParams?.Keys ?? []);
                        notes.Add($"нативный chase на {caps.Id}: заявка уйдёт как limit с параметрами [{keys}] — режим требует проверки на testnet");
                    }
                }
            }
        }

        if (type == OrderType.Market)
        {
            offsetBps = 0m; // смещение цены к market не применяется
        }

        return new OrderPolicy(type, timeInForce, offsetBps, chase, exchangeParams, notes);
    }

    private static ChaseMode ResolveChaseMode(Effective eff, ExchangeCapabilities caps, List<string> notes)
    {
        var mode = eff.Mode ?? ChaseMode.Simulated; // ChaseLimit без явного режима — локальное догонание

        if (mode == ChaseMode.None)
        {
            notes.Add("режим догонания отключён (Chase:Mode = None) — обычная лимитная заявка");
            return ChaseMode.None;
        }

        if (mode == ChaseMode.Simulated && eff.TimeInForce is TimeInForce.Ioc or TimeInForce.Fok)
        {
            // биржа снимает остаток сама — переставлять заявку нечего, догонание бессмысленно
            notes.Add($"догонание невозможно с {eff.TimeInForce} (остаток снимается сразу) — обычная лимитная заявка");
            return ChaseMode.None;
        }

        if (mode == ChaseMode.Native && !caps.SupportsNativeChase)
        {
            notes.Add($"нативный chase на {caps.Id} недоступен — используется локальное догонание (Simulated)");
            mode = ChaseMode.Simulated;
        }

        if (mode == ChaseMode.Native && (eff.Params?.Count ?? 0) == 0 && caps.NativeChaseDefaults is null)
        {
            notes.Add($"параметры нативного chase (Execution:Chase:Params) не заданы — используется локальное догонание (Simulated)");
            mode = ChaseMode.Simulated;
        }

        if (mode == ChaseMode.Simulated && !caps.SupportsSimulatedChase)
        {
            notes.Add($"локальное догонание на {caps.Id} недоступно (нет отмены/опроса заявок) — обычная лимитная заявка");
            return ChaseMode.None;
        }

        return mode;
    }

    private static TimeInForce DegradeTimeInForce(TimeInForce requested, ExchangeCapabilities caps, List<string> notes) => requested switch
    {
        TimeInForce.PostOnly when !caps.SupportsPostOnly => Downgrade(notes, "Post-only", caps.Id),
        TimeInForce.Ioc when !caps.SupportsIoc => Downgrade(notes, "IOC", caps.Id),
        TimeInForce.Fok when !caps.SupportsFok => Downgrade(notes, "FOK", caps.Id),
        _ => requested,
    };

    private static TimeInForce Downgrade(List<string> notes, string name, string exchangeId)
    {
        notes.Add($"{name} на {exchangeId} не поддерживается — заявка с GTC");
        return TimeInForce.Gtc;
    }

    /// <summary>Слои «глобальные настройки + переопределение биржи» в плоский набор значений.</summary>
    private static Effective Merge(OrderExecutionOptions? baseSection, OrderExecutionOptions? overSection)
    {
        var chaseBase = baseSection?.Chase;
        var chaseOver = overSection?.Chase;

        return new Effective(
            overSection?.Type ?? baseSection?.Type,
            overSection?.LimitOffsetBps ?? baseSection?.LimitOffsetBps,
            overSection?.TimeInForce ?? baseSection?.TimeInForce,
            chaseOver?.Mode ?? chaseBase?.Mode,
            chaseOver?.MaxSteps ?? chaseBase?.MaxSteps,
            chaseOver?.StepIntervalMs ?? chaseBase?.StepIntervalMs,
            chaseOver?.StepBps ?? chaseBase?.StepBps,
            chaseOver?.MaxDeviationBps ?? chaseBase?.MaxDeviationBps,
            chaseOver?.FallbackToMarket ?? chaseBase?.FallbackToMarket,
            chaseOver?.Params is { Count: > 0 } overParams ? overParams : chaseBase?.Params);
    }

    private sealed record Effective(
        OrderType? Type,
        decimal? LimitOffsetBps,
        TimeInForce? TimeInForce,
        ChaseMode? Mode,
        int? MaxSteps,
        int? StepIntervalMs,
        decimal? StepBps,
        decimal? MaxDeviationBps,
        bool? FallbackToMarket,
        IReadOnlyDictionary<string, string>? Params);
}

/// <summary>
/// Перевод <see cref="OrderRequest"/> в params-словарь CCXT. Инфраструктура
/// (CcxtExchangeConnector) лишь передаёт результат в CreateOrder — имена и условия
/// живут здесь, чтобы их можно было проверить без реальных бирж.
/// </summary>
public static class OrderParamsBuilder
{
    /// <summary>Параметры заявки для CCXT: reduceOnly, timeInForce/postOnly, сырые параметры биржи.</summary>
    public static Dictionary<string, object> Build(OrderRequest request, ExchangeCapabilities caps)
    {
        Dictionary<string, object> parameters = [];

        if (request.ReduceOnly)
        {
            parameters["reduceOnly"] = true;
        }

        if (request.Type is OrderType.Limit or OrderType.ChaseLimit && request.TimeInForce != TimeInForce.Gtc)
        {
            if (request.TimeInForce == TimeInForce.PostOnly)
            {
                // понижение до GTC уже сделала политика; если пришли напрямую — страховка
                if (caps.PostOnlyToken is { } token)
                {
                    parameters["timeInForce"] = token;
                }
                else
                {
                    parameters["postOnly"] = true;
                }
            }
            else
            {
                parameters["timeInForce"] = request.TimeInForce.ToString().ToUpperInvariant();
            }
        }

        if (request.ExchangeParams is { Count: > 0 } raw)
        {
            // сырые параметры биржи перекрывают остальное: это осознанный ручной выбор
            foreach (var (key, value) in raw)
            {
                parameters[key] = value;
            }
        }

        return parameters;
    }
}

