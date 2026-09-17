using FuturesArbBot.Core.Engine;

namespace FuturesArbBot.Tests;

/// <summary>
/// Сборка текста уведомления из шаблона Notifications:TextTemplate: подстановки,
/// значения по умолчанию и поведение при пустом/неполном шаблоне.
/// </summary>
public class NotificationFormatterTests
{
    private static readonly DateTimeOffset Detected = new(2026, 1, 1, 12, 34, 56, TimeSpan.Zero);

    private static SpreadEstimate Estimate(decimal net = 0.8m, decimal? longFunding = null, decimal? shortFunding = null) => new(
        "BTC/USDT:USDT",
        new OpportunityLeg("binanceusdm", 100.1234m, 0.1m) { FundingPercent = longFunding },
        new OpportunityLeg("bybit", 101.5m, 0.05m) { FundingPercent = shortFunding },
        GrossPercent: 1.38m,
        NetPercent: net,
        QuoteVolumeUsd: 12_345_678m,
        DetectedAt: Detected);

    [Fact]
    public void Default_template_renders_symbol_prices_and_spreads()
    {
        var text = NotificationFormatter.Format(Estimate(), new NotificationOptions());

        Assert.Contains("BTC/USDT:USDT", text);
        Assert.Contains("binanceusdm @ 100.1234", text);
        Assert.Contains("bybit @ 101.5", text);
        Assert.Contains("+0.800%", text);
        Assert.Contains("+1.380%", text);
        Assert.Contains("12.3M", text);
        Assert.Contains("2026-01-01 12:34:56", text);
    }

    [Fact]
    public void Missing_funding_renders_as_unknown()
    {
        var text = NotificationFormatter.Format(Estimate(), new NotificationOptions());

        Assert.Contains("фандинг н/д/н/д", text);
    }

    [Fact]
    public void Funding_of_both_legs_is_shown()
    {
        var text = NotificationFormatter.Format(Estimate(longFunding: -0.5m, shortFunding: 0.25m), new NotificationOptions());

        Assert.Contains("-0.500%/+0.250%", text);
    }

    [Fact]
    public void Empty_template_falls_back_to_default()
    {
        var options = new NotificationOptions { TextTemplate = "   " };

        Assert.Equal(
            NotificationFormatter.Format(Estimate(), new NotificationOptions()),
            NotificationFormatter.Format(Estimate(), options));
    }

    [Fact]
    public void Custom_template_replaces_known_tokens()
    {
        var options = new NotificationOptions
        {
            TextTemplate = "{symbol}|{net}|{gross}|{longExchange}|{longPrice}|{shortExchange}|{shortPrice}|{longFee}|{shortFee}|{volume}",
        };

        Assert.Equal(
            "BTC/USDT:USDT|+0.800%|+1.380%|binanceusdm|100.1234|bybit|101.5|+0.100%|+0.050%|12.3M",
            NotificationFormatter.Format(Estimate(), options));
    }

    [Fact]
    public void Unknown_tokens_are_left_as_is()
    {
        var text = NotificationFormatter.Format(Estimate(), new NotificationOptions { TextTemplate = "спред {net} {oops}" });

        Assert.Equal("спред +0.800% {oops}", text);
    }

    [Fact]
    public void Test_message_mentions_mode_and_time()
    {
        var text = NotificationFormatter.TestMessage(Detected, NetworkMode.Testnet);

        Assert.Contains("Testnet", text);
        Assert.Contains("2026-01-01 12:34:56", text);
    }
}
