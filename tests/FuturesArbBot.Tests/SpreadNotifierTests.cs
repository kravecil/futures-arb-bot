using FuturesArbBot.Core.Abstractions;
using FuturesArbBot.Core.Engine;
using Microsoft.Extensions.Logging.Abstractions;

namespace FuturesArbBot.Tests;

/// <summary>
/// Неблокирующий отправитель уведомлений: порог спреда, кулдаун на символ, лимит в минуту,
/// защита от серии сбоев и слив очереди при остановке. Время — ручное, транспорт — фейковый.
/// </summary>
public class SpreadNotifierTests
{
    private static readonly DateTimeOffset Now = new(2026, 1, 1, 12, 0, 0, TimeSpan.Zero);

    private static BotOptions Configured(Action<NotificationOptions>? tweak = null)
    {
        var options = new BotOptions();
        options.General.WriteLogFile = false;
        options.Arbitrage.MinSpreadPercentUp = 0.4m;
        options.Notifications.Enabled = true;
        options.Notifications.BotToken = "token";
        options.Notifications.AdminChatId = "42";
        options.Notifications.MaxRetries = 0;
        tweak?.Invoke(options.Notifications);
        return options;
    }

    private static SpreadEstimate Estimate(string symbol = "BTC/USDT:USDT", decimal net = 0.8m) => new(
        symbol,
        new OpportunityLeg("binanceusdm", 100m, 0.1m),
        new OpportunityLeg("bybit", 101m, 0.1m),
        GrossPercent: 1m,
        NetPercent: net,
        QuoteVolumeUsd: 1_000_000m,
        DetectedAt: Now);

    private static (SpreadNotifier Notifier, FakeTransport Transport) Create(BotOptions options, ManualTimeProvider? time = null)
    {
        var clock = time ?? new ManualTimeProvider(Now);
        var config = new FakeConfigProvider(options);
        var transport = new FakeTransport();
        return (new SpreadNotifier(config, transport, new EventLog(config, clock), clock, NullLogger<SpreadNotifier>.Instance), transport);
    }

    /// <summary>Дождаться условия, пока фоновая читатель обрабатывает очередь.</summary>
    private static async Task WaitUntilAsync(Func<bool> condition, int timeoutMs = 2_000)
    {
        var deadline = Environment.TickCount64 + timeoutMs;
        while (!condition() && Environment.TickCount64 < deadline)
        {
            await Task.Delay(10);
        }

        Assert.True(condition(), "фоновая отправка не дождалась условия");
    }

    [Fact]
    public async Task Disabled_section_sends_nothing()
    {
        var (notifier, transport) = Create(Configured(n => n.Enabled = false));
        using (notifier)
        {
            notifier.Notify(Estimate());
            await Task.Delay(30);

            Assert.Equal(0, transport.CallCount);
            Assert.Equal(0, notifier.PendingCount);
        }
    }

    [Fact]
    public async Task Missing_token_or_chat_sends_nothing()
    {
        var (notifier, transport) = Create(Configured(n => n.AdminChatId = string.Empty));
        using (notifier)
        {
            notifier.Notify(Estimate());
            await Task.Delay(30);

            Assert.Equal(0, transport.CallCount);
        }
    }

    [Fact]
    public async Task Threshold_inherited_from_arbitrage_entry()
    {
        var options = Configured(n => n.SignalCooldownSeconds = 0);
        options.Arbitrage.MinSpreadPercentUp = 0.9m;
        var (notifier, transport) = Create(options);
        using (notifier)
        {
            notifier.Notify(Estimate(net: 0.8m));
            await Task.Delay(30);
            Assert.Equal(0, transport.CallCount);

            notifier.Notify(Estimate(net: 1.2m));
            await WaitUntilAsync(() => transport.CallCount == 1);
        }
    }

    [Fact]
    public async Task Own_threshold_overrides_arbitrage_entry()
    {
        var (notifier, transport) = Create(Configured(n => n.MinSpreadPercent = 1.5m));
        using (notifier)
        {
            notifier.Notify(Estimate(net: 1.4m));
            await Task.Delay(30);
            Assert.Equal(0, transport.CallCount);

            notifier.Notify(Estimate(net: 1.6m));
            await WaitUntilAsync(() => transport.CallCount == 1);
            Assert.Contains("+1.600%", Assert.Single(transport.Sent));
        }
    }

    [Fact]
    public async Task Cooldown_holds_the_same_symbol()
    {
        var time = new ManualTimeProvider(Now);
        var (notifier, transport) = Create(Configured(n => n.SignalCooldownSeconds = 120), time);
        using (notifier)
        {
            notifier.Notify(Estimate());
            await WaitUntilAsync(() => transport.CallCount == 1);

            notifier.Notify(Estimate());
            await Task.Delay(30);
            Assert.Equal(1, transport.CallCount);

            time.Advance(TimeSpan.FromSeconds(121));
            notifier.Notify(Estimate());
            await WaitUntilAsync(() => transport.CallCount == 2);
        }
    }

    [Fact]
    public async Task Different_symbols_do_not_share_cooldown()
    {
        var (notifier, transport) = Create(Configured());
        using (notifier)
        {
            notifier.Notify(Estimate("BTC/USDT:USDT"));
            notifier.Notify(Estimate("ETH/USDT:USDT"));

            await WaitUntilAsync(() => transport.CallCount == 2);
        }
    }

    [Fact]
    public async Task Per_minute_limit_drops_the_rest()
    {
        var (notifier, transport) = Create(Configured(n =>
        {
            n.SignalCooldownSeconds = 0;
            n.MaxPerMinute = 2;
        }));
        using (notifier)
        {
            foreach (var symbol in new[] { "A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT", "D/USDT:USDT" })
            {
                notifier.Notify(Estimate(symbol));
            }

            await WaitUntilAsync(() => transport.CallCount == 2);
            await Task.Delay(30);

            Assert.Equal(2, transport.CallCount);
            Assert.True(notifier.SuppressedCount >= 2, $"ожидались отброшенные сигналы, SuppressedCount = {notifier.SuppressedCount}");
        }
    }

    [Fact]
    public async Task Transient_failures_open_the_circuit()
    {
        var (notifier, transport) = Create(Configured(n =>
        {
            n.SignalCooldownSeconds = 0;
            n.FailureCircuitLimit = 2;
        }));
        using (notifier)
        {
            transport.Outcomes.Add(NotificationDelivery.Transient("5xx"));

            notifier.Notify(Estimate("A/USDT:USDT"));
            notifier.Notify(Estimate("B/USDT:USDT"));
            await WaitUntilAsync(() => transport.CallCount == 2);

            notifier.Notify(Estimate("C/USDT:USDT"));
            await Task.Delay(50);
            Assert.Equal(2, transport.CallCount);
        }
    }

    [Fact]
    public async Task Permanent_failure_opens_the_circuit_at_once()
    {
        var (notifier, transport) = Create(Configured(n => n.SignalCooldownSeconds = 0));
        using (notifier)
        {
            transport.Outcomes.Add(NotificationDelivery.Permanent("401 verify.token"));

            notifier.Notify(Estimate("A/USDT:USDT"));
            await WaitUntilAsync(() => transport.CallCount == 1);

            notifier.Notify(Estimate("B/USDT:USDT"));
            await Task.Delay(50);
            Assert.Equal(1, transport.CallCount);
        }
    }

    [Fact]
    public async Task Success_resets_the_failure_counter()
    {
        var (notifier, transport) = Create(Configured(n =>
        {
            n.SignalCooldownSeconds = 0;
            n.FailureCircuitLimit = 2;
        }));
        using (notifier)
        {
            transport.Outcomes.Add(NotificationDelivery.Transient("5xx"));
            transport.Outcomes.Add(null);

            notifier.Notify(Estimate("A/USDT:USDT"));
            await WaitUntilAsync(() => transport.CallCount == 1);

            notifier.Notify(Estimate("B/USDT:USDT"));
            await WaitUntilAsync(() => transport.CallCount == 2);

            notifier.Notify(Estimate("C/USDT:USDT"));
            await WaitUntilAsync(() => transport.CallCount == 3);
        }
    }

    [Fact]
    public void Dispose_drains_the_pending_queue()
    {
        var (notifier, transport) = Create(Configured(n => n.SignalCooldownSeconds = 0));
        transport.Delay = TimeSpan.FromMilliseconds(40);

        notifier.Notify(Estimate("A/USDT:USDT"));
        notifier.Notify(Estimate("B/USDT:USDT"));
        notifier.Dispose();

        Assert.Equal(2, transport.CallCount);
    }
}

