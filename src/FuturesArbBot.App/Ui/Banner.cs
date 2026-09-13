namespace FuturesArbBot.App.Ui;

/// <summary>Стартовый баннер с настройками сеанса.</summary>
public static class Banner
{
    public static void Print(IConfigProvider config)
    {
        var options = config.Current;

        AnsiConsole.Write(new FigletText("FUTURES ARB").Color(Color.Cyan1).LeftJustified());
        AnsiConsole.Write(new Rule("[grey]межбиржевой арбитраж фьючерсов · CCXT[/]").RuleStyle("grey").LeftJustified());
        AnsiConsole.WriteLine();

        var mode = options.General.NetworkMode switch
        {
            NetworkMode.DryRun => "[yellow]DRY-RUN[/] [grey](симуляция, ордера не отправляются)[/]",
            NetworkMode.Testnet => "[blue]TESTNET[/] [grey](тестовая сеть)[/]",
            NetworkMode.Live => "[bold red]LIVE[/] [red](реальные деньги!)[/]",
            _ => "?",
        };

        var trading = options.Arbitrage.Enabled
            ? "[green]включена[/] [grey](ордера исполняются)[/]"
            : "[silver]выключена[/] [grey](только мониторинг спредов)[/]";

        var grid = new Grid().AddColumn().AddColumn();
        grid.AddRow("[grey]Режим сети:[/]", mode);
        grid.AddRow("[grey]Торговля:[/]", trading);
        grid.AddRow("[grey]Пороги спреда:[/]", $"вход {Formatting.Pct(options.Arbitrage.MinSpreadPercentUp)} / фиксация {Formatting.Pct(options.Arbitrage.MinSpreadPercentDown)} / стоп {Formatting.Pct(options.Arbitrage.StopLossSpreadPercent)}");
        grid.AddRow("[grey]Ордер:[/]", $"{options.Arbitrage.OrderSizeUsd} USD × {options.Arbitrage.Leverage} (плечо), комиссия {(options.Arbitrage.IncludeFees ? "учитывается" : "НЕ учитывается")}");
        grid.AddRow("[grey]Конфигурация:[/]", Markup.Escape(config.ConfigDirectory));

        AnsiConsole.Write(grid);
        AnsiConsole.WriteLine();
        AnsiConsole.MarkupLine("[grey]Остановка: Ctrl+C — корректное завершение с итоговой статистикой.[/]");
        AnsiConsole.WriteLine();
    }
}
