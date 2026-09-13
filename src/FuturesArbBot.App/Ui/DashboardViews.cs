namespace FuturesArbBot.App.Ui;

/// <summary>Переиспользуемые визуальные блоки дашборда (таблица возможностей).</summary>
public static class DashboardViews
{
    public static IRenderable OpportunitiesTable(DashboardSnapshot? snapshot, ArbitrageOptions arbitrage)
    {
        var table = new Table()
            .Border(TableBorder.Rounded)
            .BorderStyle(Color.Cyan1)
            .Expand();

        if (snapshot is null)
        {
            table.Title("[cyan]Топ возможностей[/]");
            table.AddColumn("[cyan]Символ[/]");
            table.AddRow("[grey]ожидание данных…[/]");
            return table;
        }

        table.Title($"[cyan]Топ возможностей[/] [grey]· отслеживается пар: {snapshot.TrackedSymbols} · порог входа {Formatting.Pct(arbitrage.MinSpreadPercentUp)}[/]");
        table.AddColumn(new TableColumn("[cyan]Символ[/]").NoWrap());
        table.AddColumn("[cyan]Лонг (купить)[/]");
        table.AddColumn("[cyan]Шорт (продать)[/]");
        table.AddColumn(new TableColumn("[cyan]Спред[/]").RightAligned());
        table.AddColumn(new TableColumn("[cyan]Нетто[/]").RightAligned());
        table.AddColumn(new TableColumn("[cyan]Объём 24ч[/]").RightAligned());

        if (snapshot.Top.Count == 0)
        {
            table.AddRow("[grey]—[/]", "[grey]пока нет пересекающихся пар с положительным нетто-спредом[/]", string.Empty, string.Empty, string.Empty, string.Empty);
            return table;
        }

        foreach (var estimate in snapshot.Top)
        {
            var strong = estimate.NetPercent >= arbitrage.MinSpreadPercentUp;
            var symbol = strong ? $"[bold white]{Markup.Escape(estimate.Symbol)}[/] ▸" : Markup.Escape(estimate.Symbol);

            var net = strong
                ? $"[bold green]{Formatting.Pct(estimate.NetPercent)}[/]"
                : estimate.NetPercent > 0m ? $"[green]{Formatting.Pct(estimate.NetPercent)}[/]" : $"[silver]{Formatting.Pct(estimate.NetPercent)}[/]";

            table.AddRow(
                symbol,
                $"[white]{Markup.Escape(estimate.LongLeg.ExchangeId)}[/] [grey]@[/] {Formatting.Price(estimate.LongLeg.Price)}",
                $"[white]{Markup.Escape(estimate.ShortLeg.ExchangeId)}[/] [grey]@[/] {Formatting.Price(estimate.ShortLeg.Price)}",
                Formatting.Pct(estimate.GrossPercent),
                net,
                $"[silver]{Formatting.Volume(estimate.QuoteVolumeUsd)}[/]");
        }

        return table;
    }
}
