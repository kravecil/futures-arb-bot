namespace FuturesArbBot.App.Ui;

/// <summary>
/// Подробный отчёт по сеансу: печатается после остановки робота
/// (в том числе при выходе по Ctrl+C).
/// </summary>
public sealed class SessionReportPrinter(IEventLog log)
{
    public void Print(SessionReport report)
    {
        AnsiConsole.Write(new Rule("[cyan]Итоги сеанса[/]").RuleStyle("cyan").Centered());
        AnsiConsole.WriteLine();

        PrintGeneral(report);
        PrintTrades(report);
        PrintStillOpen(report);
        PrintExchanges(report);
        PrintTopSymbols(report);
        PrintErrors();
        PrintFinalPnl(report);
    }

    private static void PrintGeneral(SessionReport report)
    {
        var table = new Table().Border(TableBorder.Rounded).HideHeaders()
            .AddColumn("Параметр")
            .AddColumn("Значение");

        table.AddRow("[cyan]Режим работы[/]", DescribeMode(report.Mode, report.TradingWasEnabled));
        table.AddRow("[cyan]Начало сеанса[/]", report.StartedAt.ToLocalTime().ToString("dd.MM.yyyy HH:mm:ss"));
        table.AddRow("[cyan]Длительность[/]", Formatting.Duration(report.Duration));
        table.AddRow("[cyan]Тиков сканирования[/]", report.ScanTicks.ToString("N0", CultureInfo.InvariantCulture));
        table.AddRow("[cyan]Тикеров получено[/]", report.TickersProcessed.ToString("N0", CultureInfo.InvariantCulture));
        table.AddRow("[cyan]Сигналов найдено[/]", $"{report.OpportunitiesDetected:N0} ({report.OpportunitiesPerHour:0}/час)");
        table.AddRow(
            "[cyan]Лучший спред[/]",
            report.BestEver is null ? "—" : $"{Markup.Escape(report.BestEver.Symbol)} · {Formatting.Pct(report.BestEver.NetPercent)} ({report.BestEver.LongLeg.ExchangeId} → {report.BestEver.ShortLeg.ExchangeId})");
        table.AddRow("[cyan]Ордеров открыто / ошибок[/]", $"{report.OrdersOpened} / {report.OrdersFailed}");
        table.AddRow("[cyan]Позиций открыто / закрыто[/]", $"{report.OrdersOpened} / {report.ClosedPositions.Count}");

        AnsiConsole.Write(table);
        AnsiConsole.WriteLine();
    }

    private void PrintTrades(SessionReport report)
    {
        if (report.ClosedPositions.Count == 0)
        {
            return;
        }

        var table = new Table().Border(TableBorder.Rounded)
            .Title("[cyan]Закрытые арбитражи[/]")
            .AddColumn("[cyan]Символ[/]")
            .AddColumn("[cyan]Лонг @[/]")
            .AddColumn("[cyan]Шорт @[/]")
            .AddColumn("[cyan]Выход Л@[/]")
            .AddColumn("[cyan]Выход Ш@[/]")
            .AddColumn(new TableColumn("[cyan]Размер[/]").RightAligned())
            .AddColumn("[cyan]Причина[/]")
            .AddColumn(new TableColumn("[cyan]PnL[/]").RightAligned());

        foreach (var trade in report.ClosedPositions)
        {
            table.AddRow(
                Markup.Escape(trade.Symbol),
                $"{Markup.Escape(trade.LongExchangeId)} {Formatting.Price(trade.EntryLong)}",
                $"{Markup.Escape(trade.ShortExchangeId)} {Formatting.Price(trade.EntryShort)}",
                Formatting.Price(trade.ExitLong ?? 0m),
                Formatting.Price(trade.ExitShort ?? 0m),
                Formatting.Volume(trade.Size),
                DescribeReason(trade.Reason) + (trade.Simulated ? " · сим." : string.Empty),
                trade.PnlUsd > 0m ? $"[green]{Formatting.Usd(trade.PnlUsd)}[/]" : trade.PnlUsd < 0m ? $"[red]{Formatting.Usd(trade.PnlUsd)}[/]" : Formatting.Usd(trade.PnlUsd));
        }

        AnsiConsole.Write(table);
        AnsiConsole.WriteLine();
    }

    private void PrintStillOpen(SessionReport report)
    {
        if (report.StillOpen.Count == 0)
        {
            return;
        }

        var table = new Table().Border(TableBorder.Rounded)
            .Title("[yellow]Остались открытыми (ClosePositionsOnExit = false)[/]")
            .AddColumn("[cyan]Символ[/]")
            .AddColumn("[cyan]Лонг[/]")
            .AddColumn("[cyan]Шорт[/]")
            .AddColumn(new TableColumn("[cyan]Размер[/]").RightAligned())
            .AddColumn("[cyan]Открыта[/]");

        foreach (var position in report.StillOpen)
        {
            table.AddRow(
                Markup.Escape(position.Symbol),
                $"{Markup.Escape(position.LongExchangeId)} @ {Formatting.Price(position.EntryLong)}",
                $"{Markup.Escape(position.ShortExchangeId)} @ {Formatting.Price(position.EntryShort)}",
                Formatting.Volume(position.Size),
                position.OpenedAt.ToLocalTime().ToString("HH:mm:ss"));
        }

        AnsiConsole.Write(table);
        AnsiConsole.WriteLine();
    }

    private static void PrintExchanges(SessionReport report)
    {
        var table = new Table().Border(TableBorder.Rounded)
            .Title("[cyan]Биржи[/]")
            .AddColumn("[cyan]Биржа[/]")
            .AddColumn("[cyan]Режим[/]")
            .AddColumn(new TableColumn("[cyan]Рынков[/]").RightAligned())
            .AddColumn(new TableColumn("[cyan]Запросов[/]").RightAligned())
            .AddColumn(new TableColumn("[cyan]Ошибок[/]").RightAligned())
            .AddColumn(new TableColumn("[cyan]Задержка[/]").RightAligned())
            .AddColumn(new TableColumn("[cyan]Taker[/]").RightAligned());

        foreach (var exchange in report.Exchanges)
        {
            table.AddRow(
                Markup.Escape(string.IsNullOrWhiteSpace(exchange.DisplayName) ? exchange.Id : exchange.DisplayName),
                exchange.Mode.ToString(),
                exchange.Markets.ToString("N0", CultureInfo.InvariantCulture),
                exchange.Requests.ToString("N0", CultureInfo.InvariantCulture),
                exchange.Errors > 0 ? $"[red]{exchange.Errors}[/]" : "0",
                $"{exchange.AvgLatencyMs:0} мс",
                exchange.TakerPercent > 0m ? $"{exchange.TakerPercent:0.####}%" : "—");
        }

        AnsiConsole.Write(table);
        AnsiConsole.WriteLine();
    }

    private void PrintTopSymbols(SessionReport report)
    {
        if (report.TopSymbols.Count == 0)
        {
            return;
        }

        var chart = new BarChart()
            .Width(70)
            .Label("[cyan]Топ символов по числу сигналов[/]")
            .CenterLabel()
            .LeftAlignLabel();

        foreach (var (symbol, count) in report.TopSymbols)
        {
            chart.AddItem(Markup.Escape(symbol), count, Color.Cyan1);
        }

        AnsiConsole.Write(chart);
        AnsiConsole.WriteLine();
    }

    private void PrintErrors()
    {
        var errors = log.Errors(5);
        if (errors.Count == 0)
        {
            return;
        }

        var table = new Table().Border(TableBorder.Rounded)
            .Title("[red]Последние ошибки[/]")
            .HideHeaders()
            .AddColumn(string.Empty);

        foreach (var error in errors)
        {
            table.AddRow($"[dim]{error.Time.ToLocalTime():HH:mm:ss}[/] {Markup.Escape(error.Message)}");
        }

        AnsiConsole.Write(table);
        AnsiConsole.WriteLine();
    }

    private static void PrintFinalPnl(SessionReport report)
    {
        if (report.ClosedPositions.Count == 0)
        {
            return;
        }

        var pnl = report.RealizedPnlUsd;
        var color = pnl > 0m ? "green" : pnl < 0m ? "red" : "white";
        var suffix = report.Mode == NetworkMode.DryRun ? " [grey](симуляция)[/]" : string.Empty;
        AnsiConsole.Write(new Rule().RuleStyle("grey"));
        AnsiConsole.MarkupLine($"Итоговая PnL за сеанс: [bold {color}]{Formatting.Usd(pnl)}[/]{suffix}, уплачено комиссий {Formatting.Usd(report.FeesPaidUsd)}.");
        AnsiConsole.WriteLine();
    }

    private static string DescribeMode(NetworkMode mode, bool trading) => (mode, trading) switch
    {
        (NetworkMode.DryRun, _) => "DRY-RUN (симуляция)",
        (NetworkMode.Testnet, true) => "TESTNET (торговля включена)",
        (NetworkMode.Testnet, false) => "TESTNET (мониторинг)",
        (NetworkMode.Live, true) => "LIVE (реальная торговля)",
        (NetworkMode.Live, false) => "LIVE (мониторинг)",
        _ => mode.ToString(),
    };

    private static string DescribeReason(CloseReason reason) => reason switch
    {
        CloseReason.TakeProfit => "тейк-профит",
        CloseReason.StopLoss => "стоп-лосс",
        CloseReason.Timeout => "таймаут",
        CloseReason.SessionEnd => "конец сеанса",
        CloseReason.Rollback => "откат",
        _ => reason.ToString(),
    };
}
