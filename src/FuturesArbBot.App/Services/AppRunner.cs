using Microsoft.Extensions.Configuration;

namespace FuturesArbBot.App.Services;

/// <summary>
/// Оркестратор сеанса: баннер → подключение бирж → проверки безопасности →
/// сканирование (дашборд или поток событий) → корректная остановка → итоговый отчёт.
/// </summary>
public sealed class AppRunner(
    IServiceProvider services,
    IConfigProvider config,
    IExchangeFactory factory,
    ConnectorRegistry registry,
    IStatisticsCollector stats,
    IEventLog log,
    TimeProvider time,
    CliOptions cli) : IDisposable
{
    private RunState? _state;

    public async Task<int> RunAsync(CancellationToken ct)
    {
        if (!ValidateOptions(config.Current, out var problem))
        {
            AnsiConsole.MarkupLine($"[red]Конфигурация некорректна:[/] {Markup.Escape(problem)}");
            return 2;
        }

        if (!ValidateExecution(config.Current, out problem))
        {
            AnsiConsole.MarkupLine($"[red]Настройки исполнения некорректны:[/] {Markup.Escape(problem)}");
            return 2;
        }

        Banner.Print(config);

        var connectors = await ConnectExchangesAsync(ct);
        if (connectors.Count < 2)
        {
            AnsiConsole.MarkupLine("[red]Подключено меньше двух бирж — арбитраж невозможен. Проверьте exchanges.json и сеть.[/]");
            return 2;
        }

        registry.Replace(connectors);

        DescribeExecution(connectors);

        if (!await CheckTradingAccessAsync(connectors, ct))
        {
            return 2;
        }

        _state = new RunState();
        log.Info($"Сеанс запущен: режим {config.Current.General.NetworkMode}, бирж {connectors.Count}, торговля {(config.Current.Arbitrage.Enabled ? "ВКЛЮЧЕНА" : "выключена (мониторинг)")}");
        LogNotificationState(config.Current.Notifications);

        var scanner = services.GetRequiredService<IArbitrageScanner>();

        if (cli.Once)
        {
            return await RunOnceAsync(scanner, ct);
        }

        return await RunInteractiveAsync(scanner, ct);
    }

    // ------------------------- режимы работы -------------------------

    private async Task<int> RunInteractiveAsync(IArbitrageScanner scanner, CancellationToken ct)
    {
        if (cli.NoUi)
        {
            var feed = new SimpleConsoleFeed(log);
            using var feedCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            var feedTask = feed.RunAsync(feedCts.Token);

            try
            {
                await scanner.RunAsync(ct);
            }
            catch (OperationCanceledException)
            {
            }

            feedCts.Cancel();
            try
            {
                await feedTask;
            }
            catch
            {
                // поток событий уже не нужен
            }
        }
        else
        {
            var dashboard = services.GetRequiredService<Dashboard>();
            var dashboardTask = dashboard.RunAsync(ct, () => scanner.Snapshot);

            try
            {
                await scanner.RunAsync(ct);
            }
            catch (OperationCanceledException)
            {
            }
            catch (Exception ex)
            {
                _state?.LastError = ex.Message; // C# 14: присваивание с null-условием
                log.Error($"Сканер остановлен с ошибкой: {ex.Message}");
            }

            try
            {
                await dashboardTask;
            }
            catch
            {
                // дашборд уже завершился
            }
        }

        await ShutdownAsync();
        PrintFinalReport();
        return 0;
    }

    private async Task<int> RunOnceAsync(IArbitrageScanner scanner, CancellationToken ct)
    {
        AnsiConsole.MarkupLine("[grey]Режим --once: одиночное сканирование.[/]");
        await scanner.ScanOnceAsync(ct);

        var snapshot = scanner.Snapshot;
        if (snapshot is not null)
        {
            AnsiConsole.Write(DashboardViews.OpportunitiesTable(snapshot, config.Current.Arbitrage));
        }

        await ShutdownAsync();
        PrintFinalReport();
        return 0;
    }

    // ------------------------- запуск и остановка -------------------------

    /// <summary>
    /// Показать, какие заявки реально уйдут на каждую биржу (конфигурация + переопределения
    /// + возможности биржи). Понижения unsupported-настроек видны сразу при старте,
    /// а не в момент первой сделки.
    /// </summary>
    private void DescribeExecution(IReadOnlyList<IExchangeConnector> connectors)
    {
        var options = config.Current.Arbitrage;
        var entries = config.Current.Exchanges.Items;

        foreach (var connector in connectors)
        {
            var entry = entries.FirstOrDefault(e => string.Equals(e.Id, connector.Id, StringComparison.OrdinalIgnoreCase));
            var caps = ExchangeCapabilityMap.For(connector.Id);
            var openPolicy = OrderPolicyResolver.ResolveEntry(options.Execution, entry?.Execution, caps);
            var closePolicy = OrderPolicyResolver.ResolveClose(options.Execution, entry?.Execution, caps);

            AnsiConsole.MarkupLineInterpolated($"[grey]·[/] {Markup.Escape(connector.DisplayName)}: вход — {Describe(openPolicy)}, закрытие — {Describe(closePolicy)}");

            foreach (var note in openPolicy.Notes.Concat(closePolicy.Notes))
            {
                AnsiConsole.MarkupLineInterpolated($"[yellow]![/] {Markup.Escape(connector.DisplayName)}: {Markup.Escape(note)}");
                log.Warning($"[{connector.Id}] {note}");
            }
        }
    }

    /// <summary>Описание политики заявки для строки запуска.</summary>
    private static string Describe(OrderPolicy policy)
    {
        List<string> parts = [];

        if (policy.RequiresPrice && policy.LimitOffsetBps != 0m)
        {
            parts.Add($"смещение {policy.LimitOffsetBps} bps");
        }

        if (policy.Chase is { } chase)
        {
            if (policy.ChasesNatively)
            {
                var keys = string.Join(", ", policy.ExchangeParams?.Keys ?? []);
                parts.Insert(0, $"ChaseLimit (нативный chase биржи, параметры: {(keys.Length > 0 ? keys : "—")})");
            }
            else
            {
                var tail = chase.FallbackToMarket ? "остаток — market" : "без market-добивки";
                parts.Insert(0, $"ChaseLimit (локальные перестановки: до {chase.MaxSteps} шагов по {chase.StepBps} bps, не дальше {chase.MaxDeviationBps} bps от старта, {tail})");
            }
        }
        else
        {
            parts.Insert(0, policy.Type.ToString());

            if (policy.RequiresPrice && policy.TimeInForce != TimeInForce.Gtc)
            {
                parts.Insert(1, policy.TimeInForce.ToString());
            }
        }

        return string.Join(", ", parts);
    }

    private async Task<List<IExchangeConnector>> ConnectExchangesAsync(CancellationToken ct)
    {
        var entries = config.Current.Exchanges.Items.Where(e => e.Enabled).ToList();
        if (entries.Count == 0)
        {
            AnsiConsole.MarkupLine("[red]В exchanges.json нет ни одной включённой биржи.[/]");
            return [];
        }

        var mode = config.Current.General.NetworkMode;
        var unsupported = entries.Where(e => !factory.SupportedIds.Contains(e.Id, StringComparer.OrdinalIgnoreCase)).ToList();
        foreach (var entry in unsupported)
        {
            AnsiConsole.MarkupLine($"[red]Биржа «{Markup.Escape(entry.Id)}» не встроена. Поддерживаются:[/] {Markup.Escape(string.Join(", ", factory.SupportedIds))}");
        }

        List<IExchangeConnector> connectors = [];
        foreach (var entry in entries.Except(unsupported))
        {
            try
            {
                AnsiConsole.MarkupLineInterpolated($"[cyan]→[/] {Markup.Escape(string.IsNullOrWhiteSpace(entry.Name) ? entry.Id : entry.Name)}: подключение…");
                var connector = factory.Create(entry, mode);
                await connector.ConnectAsync(ct);
                connectors.Add(connector);
                AnsiConsole.MarkupLineInterpolated($"[green]✓[/] {Markup.Escape(connector.DisplayName)}: фьючерсных рынков {connector.MarketCount}");
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch (Exception ex)
            {
                AnsiConsole.MarkupLineInterpolated($"[red]✗[/] {Markup.Escape(entry.Name)}: {Markup.Escape(ex.Message)}");
                log.Error($"[{entry.Id}] не подключилась: {ex.Message}");
            }
        }

        return connectors;
    }

    private async Task<bool> CheckTradingAccessAsync(List<IExchangeConnector> connectors, CancellationToken ct)
    {
        var options = config.Current;
        var mode = options.General.NetworkMode;

        if (mode == NetworkMode.Live && options.Arbitrage.Enabled && !options.Arbitrage.AllowLive)
        {
            AnsiConsole.MarkupLine("[red]Режим Live требует явного подтверждения: выставьте Arbitrage.AllowLive = true в appsettings.json.[/]");
            return false;
        }

        if (mode == NetworkMode.DryRun || !options.Arbitrage.Enabled)
        {
            return true; // торгуем виртуально или только мониторим — ключи не нужны
        }

        // Testnet/Live + торговля: проверяем валидность ключей приватным запросом
        foreach (var connector in connectors)
        {
            AnsiConsole.MarkupLineInterpolated($"[cyan]→[/] {Markup.Escape(connector.DisplayName)}: проверка торгового доступа…");
            if (!await connector.VerifyAccessAsync(ct))
            {
                AnsiConsole.MarkupLine("[red]Ключи недействительны или доступ закрыт — включите торговлю нельзя. Заполните ApiKey/Secret в exchanges.json (или переменные ARB_*).[/]");
                return false;
            }

            AnsiConsole.MarkupLineInterpolated($"[green]✓[/] {Markup.Escape(connector.DisplayName)}: доступ подтверждён");
        }

        return true;
    }

    private async Task ShutdownAsync()
    {
        var options = config.Current;
        var executor = services.GetRequiredService<ITradeExecutor>();

        if (executor.HasOpenPositions)
        {
            if (options.Arbitrage.Enabled && options.Arbitrage.ClosePositionsOnExit)
            {
                await executor.CloseAllAsync(CloseReason.SessionEnd, CancellationToken.None);
            }
            else
            {
                log.Warning("Открытые позиции сохранены: ClosePositionsOnExit = false — закрыть их вручную на биржах.");
            }
        }

        log.Info("Сеанс завершён.");
    }

    /// <summary>Строка журнала про канал уведомлений: включён, не настроен или выключен.</summary>
    private void LogNotificationState(NotificationOptions notifications)
    {
        if (notifications.IsUsable)
        {
            var threshold = notifications.MinSpreadPercent ?? config.Current.Arbitrage.MinSpreadPercentUp;
            log.Info($"Уведомления в MAX: включены (чат {notifications.AdminChatId}, порог {Formatting.Pct(threshold)}, не чаще одного на символ в {notifications.SignalCooldownSeconds} с)");
            return;
        }

        if (notifications.Enabled)
        {
            log.Warning("Уведомления включены, но BotToken или AdminChatId не заданы — сигналы не отправляются. Помощь: --notify-test и --notify-chat-id.");
            return;
        }

        log.Debug("Уведомления выключены (Notifications:Enabled = false).");
    }

    private void PrintFinalReport()
    {
        var report = stats.Snapshot(time.GetUtcNow());
        services.GetRequiredService<SessionReportPrinter>().Print(report);
    }

    /// <summary>
    /// Проверка раздела Execution: ошибки блокируют запуск, предупреждения (в том числе
    /// понижение неподдерживаемых биржей настроек) выводятся в консоль и в журнал.
    /// </summary>
    private bool ValidateExecution(BotOptions options, out string problem)
    {
        var report = OrderConfigValidator.Validate(options, factory.SupportedIds);
        problem = string.Join(" ", report.Errors);

        foreach (var warning in report.Warnings)
        {
            AnsiConsole.MarkupLineInterpolated($"[yellow]![/] {Markup.Escape(warning)}");
            log.Warning(warning);
        }

        return report.IsValid;
    }

    private static bool ValidateOptions(BotOptions options, out string problem)
    {
        var arbitrage = options.Arbitrage;
        problem = string.Empty;

        if (arbitrage.MinSpreadPercentDown >= arbitrage.MinSpreadPercentUp)
        {
            problem = "MinSpreadPercentDown должен быть меньше MinSpreadPercentUp (порог закрытия ниже порога входа).";
            return false;
        }

        if (arbitrage.StopLossSpreadPercent <= arbitrage.MinSpreadPercentUp)
        {
            problem = "StopLossSpreadPercent должен быть больше MinSpreadPercentUp.";
            return false;
        }

        if (arbitrage.OrderSizeUsd <= 0m)
        {
            problem = "OrderSizeUsd должен быть положительным.";
            return false;
        }

        if (options.General.RefreshIntervalMs < 500)
        {
            problem = "RefreshIntervalMs не может быть меньше 500 мс (бан со стороны бирж).";
            return false;
        }

        return true;
    }

    private sealed class RunState
    {
        public string? LastError { get; set; }
    }

    public void Dispose()
    {
        registry.Dispose();
        if (services is IDisposable disposable)
        {
            disposable.Dispose();
        }
    }
}
