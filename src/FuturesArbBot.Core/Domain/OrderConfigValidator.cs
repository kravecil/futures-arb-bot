namespace FuturesArbBot.Core.Domain;

/// <summary>
/// Проверка раздела Execution до подключения бирж: явные ошибки конфигурации блокируют
/// запуск, а всё, что политика исполнения умеет безопасно понизить, уходит в предупреждения
/// (чтобы понижение не стало сюрпризом во время первой сделки).
/// </summary>
public static class OrderConfigValidator
{
    /// <summary>Идентификаторы бирж с явно описанными возможностями.</summary>
    private static readonly HashSet<string> CapabilityIds = new(ExchangeCapabilityMap.KnownIds, StringComparer.OrdinalIgnoreCase);

    /// <summary>Проверить настройки исполнения (knownExchangeIds — идентификаторы фабрики, можно null).</summary>
    public static OrderConfigValidation Validate(BotOptions options, IReadOnlyCollection<string>? knownExchangeIds = null)
    {
        List<string> errors = [];
        List<string> warnings = [];
        var arbitrage = options.Arbitrage;

        CheckSection("Arbitrage.Execution", arbitrage.Execution, arbitrage.OrderExecutionTimeoutMs, errors, warnings);

        foreach (var entry in options.Exchanges.Items.Where(e => e.Execution is not null))
        {
            if (knownExchangeIds is { Count: > 0 } ids && !ids.Contains(entry.Id, StringComparer.OrdinalIgnoreCase))
            {
                errors.Add($"exchanges.json: биржа «{entry.Id}» не встроена в фабрику — раздел Execution у неё не имеет смысла.");
            }

            if (!CapabilityIds.Contains(entry.Id))
            {
                warnings.Add($"[{entry.Id}] возможности биржи не описаны в карте — берутся базовые (limit/cancel/fetch), " +
                             "post-only, IOC/FOK и нативный chase считаются недоступными.");
            }

            CheckSection($"exchanges.json: {entry.Id}.Execution", entry.Execution, arbitrage.OrderExecutionTimeoutMs, errors, warnings);
        }

        return new OrderConfigValidation(errors, warnings);
    }

    private static void CheckSection(string path, OrderExecutionOptions? section, int executionTimeoutMs, List<string> errors, List<string> warnings)
    {
        if (section is null)
        {
            return;
        }

        if (section.Close?.Close is not null)
        {
            errors.Add($"{path}: вложенная секция Close:Close не поддерживается — у Close может быть только своя политика.");
        }

        InRange($"{path}.LimitOffsetBps", section.LimitOffsetBps, -1000m, 1000m, errors);

        if (section.Type == OrderType.ChaseLimit && section.Chase?.Mode is null or ChaseMode.Simulated
            && section.TimeInForce is TimeInForce.Ioc or TimeInForce.Fok)
        {
            warnings.Add($"{path}: с TimeInForce = {section.TimeInForce} локальное догонание невозможно " +
                         "(остаток заявки снимается биржей сразу) — Type будет понижен до Limit.");
        }

        if (section.Type == OrderType.Market && section.TimeInForce is { } marketTif)
        {
            warnings.Add($"{path}: TimeInForce = {marketTif} игнорируется для market-ордера.");
        }

        if (section.Close is { } close)
        {
            if (close.Type == OrderType.ChaseLimit)
            {
                warnings.Add($"{path}.Close.Type: догонание цены на закрытии не допускается — закрытие будет market.");
            }

            if (close.Chase is not null)
            {
                warnings.Add($"{path}.Close.Chase: раздел Chase на закрытии игнорируется.");
            }

            CheckSection($"{path}.Close", close, executionTimeoutMs, errors, warnings);
        }

        if (section.Chase is { } chase)
        {
            CheckChase($"{path}.Chase", chase, section.Type, executionTimeoutMs, errors, warnings);
        }
    }

    private static void CheckChase(string path, ChaseOptions chase, OrderType? ownerType, int executionTimeoutMs, List<string> errors, List<string> warnings)
    {
        InRange($"{path}.MaxSteps", chase.MaxSteps, 1, 100, errors);
        InRange($"{path}.StepIntervalMs", chase.StepIntervalMs, 50, 60_000, errors);
        InRange($"{path}.StepBps", chase.StepBps, 0m, 1000m, errors);
        InRange($"{path}.MaxDeviationBps", chase.MaxDeviationBps, 0m, 10_000m, errors);

        if (ownerType is null or OrderType.Limit)
        {
            warnings.Add($"{path}: раздел Chase задан, но Execution.Type = {ownerType ?? OrderType.Limit} — догонание не включится " +
                         "(нужен Type = ChaseLimit).");
        }

        if (chase.MaxDeviationBps is { } budget && chase.StepBps is { } step && budget < step)
        {
            warnings.Add($"{path}: MaxDeviationBps ({budget}) меньше шага StepBps ({step}) — догонание выродится в одну перестановку.");
        }

        if (chase.Params is { } parameters)
        {
            foreach (var (key, value) in parameters)
            {
                if (string.IsNullOrWhiteSpace(key) || string.IsNullOrWhiteSpace(value))
                {
                    errors.Add($"{path}.Params: ключ и значение не должны быть пустыми (найдено «{key}»:«{value}»).");
                }
            }
        }

        if (chase.Mode == ChaseMode.Native && chase.Params is not { Count: > 0 })
        {
            errors.Add($"{path}: Mode = Native требует непустого Params — без нативных параметров chase-ордер неотличим от лимитного.");
        }

        var steps = chase.MaxSteps ?? OrderPolicyResolver.DefaultMaxSteps;
        var interval = chase.StepIntervalMs ?? OrderPolicyResolver.DefaultStepIntervalMs;
        if (chase.Mode is null or ChaseMode.Simulated && steps * (long)interval > executionTimeoutMs)
        {
            warnings.Add($"{path}: бюджет догонания {steps} × {interval} мс превышает OrderExecutionTimeoutMs ({executionTimeoutMs} мс) — " +
                         "заявка не успеет пройти все шаги. Увеличьте OrderExecutionTimeoutMs или уменьшите MaxSteps/StepIntervalMs.");
        }

        if (chase.Mode == ChaseMode.Native)
        {
            warnings.Add($"{path}: Mode = Native требует проверки на testnet — убедитесь, что Params соответствуют API биржи, " +
                         "иначе политика сама понизит режим до Simulated.");
        }
    }

    private static void InRange(string path, decimal? value, decimal min, decimal max, List<string> errors)
    {
        if (value is { } v && (v < min || v > max))
        {
            errors.Add($"{path}: значение {v} вне допустимых границ [{min}…{max}].");
        }
    }

    private static void InRange(string path, int? value, int min, int max, List<string> errors)
    {
        if (value is { } v && (v < min || v > max))
        {
            errors.Add($"{path}: значение {v} вне допустимых границ [{min}…{max}].");
        }
    }
}

/// <summary>Результат проверки конфигурации исполнения: ошибки блокируют запуск, предупреждения — нет.</summary>
public sealed record OrderConfigValidation(IReadOnlyList<string> Errors, IReadOnlyList<string> Warnings)
{
    /// <summary>Запуск возможен: грубых ошибок нет.</summary>
    public bool IsValid => Errors.Count == 0;
}

