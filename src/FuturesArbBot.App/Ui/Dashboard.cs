namespace FuturesArbBot.App.Ui;

/// <summary>
/// Live-дашборд на Spectre.Console: шапка состояния, таблица лучших
/// возможностей, журнал событий и строка статистики. Обновляется 2–3 раза в секунду.
/// </summary>
public sealed class Dashboard(IEventLog log, IConfigProvider config, TimeProvider time)
{
    public async Task RunAsync(CancellationToken ct, Func<DashboardSnapshot?> snapshot)
    {
        var ui = config.Current.General.ConsoleUi;

        var root = new Layout("root")
            .SplitRows(
                new Layout("header").Size(6),
                new Layout("body"),
                new Layout("footer").Size(3));
        root["body"].SplitColumns(
            new Layout("opportunities"),
            new Layout("log").Size(52));

        try
        {
            await AnsiConsole.Live(root)
                .AutoClear(true)
                .StartAsync(async ctx =>
                {
                    while (!ct.IsCancellationRequested)
                    {
                        Render(root, snapshot(), ui);
                        ctx.Refresh();
                        try
                        {
                            await Task.Delay(ui.RefreshMs, ct);
                        }
                        catch (OperationCanceledException)
                        {
                            break;
                        }
                    }

                    // финальный кадр перед остановкой
                    Render(root, snapshot(), ui);
                    ctx.Refresh();
                });
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            log.Warning($"Дашборд недоступен ({ex.Message}); продолжаю в текстовом режиме.");
        }
    }

    private void Render(Layout root, DashboardSnapshot? snapshot, UiOptions ui)
    {
        root["header"].Update(Header(snapshot));
        root["opportunities"].Update(DashboardViews.OpportunitiesTable(snapshot, config.Current.Arbitrage));
        root["log"].Update(LogPanel(ui.LogLines));
        root["footer"].Update(Footer(snapshot));
    }

    private IRenderable Header(DashboardSnapshot? snapshot)
    {
        var options = config.Current;
        var mode = options.General.NetworkMode switch
        {
            NetworkMode.DryRun => "[yellow]DRY-RUN[/]",
            NetworkMode.Testnet => "[blue]TESTNET[/]",
            NetworkMode.Live => "[bold red]LIVE[/]",
            _ => "?",
        };

        var grid = new Grid();
        for (var i = 0; i < 6; i++)
        {
            grid.AddColumn();
        }

        if (snapshot is null)
        {
            grid.AddRow("[bold cyan]FUTURES ARB BOT[/]", "[grey]ожидание данных…[/]", string.Empty, string.Empty, string.Empty, string.Empty);
            return grid;
        }

        var pnl = Formatting.Usd(snapshot.RealizedPnlUsd);
        var pnlMarkup = snapshot.RealizedPnlUsd switch
        {
            > 0m => $"[green]{pnl}[/]",
            < 0m => $"[red]{pnl}[/]",
            _ => pnl,
        };

        grid.AddRow(
            "[bold cyan]FUTURES ARB BOT[/]",
            $"режим {mode}",
            $"биржи: [white]{snapshot.OnlineExchanges}[/]",
            $"пары: [white]{snapshot.TrackedSymbols}[/]",
            $"позиции: [white]{snapshot.OpenPositions}[/]",
            $"PnL: {pnlMarkup}{(snapshot.PnlSimulated ? " [grey](симуляция)[/]" : string.Empty)}");
        grid.AddRow(
            $"[grey]обновлено[/] {snapshot.UpdatedAt.ToLocalTime():HH:mm:ss}",
            $"[grey]тиков[/] {snapshot.ScanTicks}",
            $"[grey]сигналов[/] {snapshot.Opportunities}",
            $"[grey]ошибок[/] {snapshot.Errors}",
            $"[grey]лучший нетто[/] {(snapshot.BestSymbol is null ? "—" : $"{Formatting.Pct(snapshot.BestNetEverPercent)} ({snapshot.BestSymbol})")}",
            $"[grey]торговля[/] {(snapshot.TradingEnabled ? "[green]вкл[/]" : "[silver]выкл[/]")}");

        return grid;
    }

    private IRenderable LogPanel(int lines)
    {
        var events = log.Latest(lines);
        var text = events.Count == 0
            ? "[grey]журнал пуст[/]"
            : string.Join('\n', events.Select(e =>
                $"[dim]{e.Time.ToLocalTime():HH:mm:ss}[/] [{e.Level.ColorMarkup}]{Markup.Escape(e.Message)}[/]"));

        return new Panel(new Markup(text))
            .Border(BoxBorder.Rounded)
            .BorderStyle(Color.Grey)
            .Header("[cyan]Журнал[/]");
    }

    private IRenderable Footer(DashboardSnapshot? snapshot)
    {
        if (snapshot is null)
        {
            return new Markup("[grey]подключение к биржам…[/]");
        }

        var summary = Formatting.Join(' ', new[]
        {
            "[cyan]FuturesArbBot[/]",
            $"тиков [white]{snapshot.ScanTicks}[/]",
            $"сигналов [white]{snapshot.Opportunities}[/]",
            $"позиций [white]{snapshot.OpenPositions}[/]",
            $"ошибок [white]{snapshot.Errors}[/]",
            "[dim]Ctrl+C — остановка и отчёт[/]",
        });

        return new Markup(summary);
    }
}
