namespace FuturesArbBot.Core.Engine;

/// <summary>
/// Сборка текста уведомления администратору из шаблона <c>Notifications:TextTemplate</c>
/// и оценки спреда. Разметка (markdown) в тексте — ответственность шаблона: поля подставляются
/// как есть, поэтому при <c>UseMarkdown = false</c> шаблон не должен содержать управляющих символов.
/// </summary>
public static class NotificationFormatter
{
    /// <summary>Развернуть шаблон в готовый текст сообщения.</summary>
    public static string Format(SpreadEstimate estimate, NotificationOptions options)
    {
        var template = string.IsNullOrWhiteSpace(options.TextTemplate)
            ? NotificationOptions.DefaultTextTemplate
            : options.TextTemplate;

        var substitutions = new (string Token, string Value)[]
        {
            ("{symbol}", estimate.Symbol),
            ("{longExchange}", estimate.LongLeg.ExchangeId),
            ("{longPrice}", Formatting.Price(estimate.LongLeg.Price)),
            ("{shortExchange}", estimate.ShortLeg.ExchangeId),
            ("{shortPrice}", Formatting.Price(estimate.ShortLeg.Price)),
            ("{net}", Formatting.Pct(estimate.NetPercent)),
            ("{gross}", Formatting.Pct(estimate.GrossPercent)),
            ("{longFee}", Formatting.Pct(estimate.LongLeg.FeePercent)),
            ("{shortFee}", Formatting.Pct(estimate.ShortLeg.FeePercent)),
            ("{longFunding}", Formatting.OptionalPct(estimate.LongLeg.FundingPercent)),
            ("{shortFunding}", Formatting.OptionalPct(estimate.ShortLeg.FundingPercent)),
            ("{volume}", Formatting.Volume(estimate.QuoteVolumeUsd)),
            ("{time}", Time(estimate.DetectedAt)),
        };

        foreach (var (token, value) in substitutions)
        {
            template = template.Replace(token, value);
        }

        return template;
    }

    /// <summary>Текст служебного сообщения для проверки канала (--notify-test).</summary>
    public static string TestMessage(DateTimeOffset now, NetworkMode mode) =>
        $"FuturesArbBot: канал уведомлений настроен верно. Тестовое сообщение отправлено {Time(now)}, режим {mode}.";

    /// <summary>Момент обнаружения в UTC: «2026-09-17 12:34:56, время UTC».</summary>
    private static string Time(DateTimeOffset moment) =>
        moment.ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss', время UTC'", CultureInfo.InvariantCulture);
}
