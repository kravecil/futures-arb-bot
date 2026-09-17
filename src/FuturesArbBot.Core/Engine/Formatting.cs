using System.Text;

namespace FuturesArbBot.Core.Engine;

/// <summary>Форматирование чисел для консоли и отчётов.</summary>
public static class Formatting
{
    /// <summary>Цена с разумным числом знаков в зависимости от порядка величины.</summary>
    public static string Price(decimal price) => Math.Abs(price) switch
    {
        >= 1000m => price.ToString("#,0.##", CultureInfo.InvariantCulture),
        >= 1m => price.ToString("#,0.####", CultureInfo.InvariantCulture),
        _ => price.ToString("0.######", CultureInfo.InvariantCulture),
    };

    /// <summary>Сумма в долларах со знаком.</summary>
    public static string Usd(decimal amount) => string.Create(CultureInfo.InvariantCulture, $"{amount:+#,0.00;-#,0.00;#,0.00} $");

    /// <summary>Объём в «человекочитаемом» виде: 12.3M, 456K…</summary>
    public static string Volume(decimal value) => Math.Abs(value) switch
    {
        >= 1_000_000_000m => $"{value / 1_000_000_000m:0.#}B",
        >= 1_000_000m => $"{value / 1_000_000m:0.#}M",
        >= 1_000m => $"{value / 1_000m:0.#}K",
        _ => value.ToString("0.#", CultureInfo.InvariantCulture),
    };

    /// <summary>Процент со знаком и тремя знаками после запятой.</summary>
    public static string Pct(decimal percent) => percent.ToString("+0.000;-0.000;0.000", CultureInfo.InvariantCulture) + "%";

    /// <summary>Процент или «нет данных» для отсутствующего значения (например, фандинг-рейта ноги).</summary>
    public static string OptionalPct(decimal? percent) => percent is { } value ? Pct(value) : "н/д";

    /// <summary>Длительность в виде «1д 02:03:04» / «02:03:04».</summary>
    public static string Duration(TimeSpan span) => span.TotalDays >= 1
        ? $"{(int)span.TotalDays}д {span:hh\\:mm\\:ss}"
        : span.ToString(@"hh\:mm\:ss", CultureInfo.InvariantCulture);

    /// <summary>Склейка частей через разделитель (C# 13: params ReadOnlySpan).</summary>
    public static string Join(char separator, params ReadOnlySpan<string?> parts)
    {
        StringBuilder sb = new();
        foreach (var part in parts)
        {
            if (string.IsNullOrWhiteSpace(part))
            {
                continue;
            }

            if (sb.Length > 0)
            {
                sb.Append(separator);
            }

            sb.Append(part);
        }

        return sb.ToString();
    }

    extension(decimal value)
    {
        /// <summary>Красит процент: положительный — зелёным, отрицательный — красным (Spectre-разметка).</summary>
        public string PctMarkup => value switch
        {
            > 0m => $"[green]{Pct(value)}[/]",
            < 0m => $"[red]{Pct(value)}[/]",
            _ => Pct(value),
        };
    }

    extension(AppLogLevel level)
    {
        /// <summary>Цвет уровня события для Spectre-разметки.</summary>
        public string ColorMarkup => level switch
        {
            AppLogLevel.Debug => "grey",
            AppLogLevel.Info => "silver",
            AppLogLevel.Success => "green",
            AppLogLevel.Warning => "yellow",
            AppLogLevel.Error => "red",
            _ => "white",
        };
    }
}
