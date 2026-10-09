using FuturesArbBot.Core.Abstractions;
using FuturesArbBot.Infrastructure.Ccxt;
using Microsoft.Extensions.Logging.Abstractions;

namespace FuturesArbBot.Tests;

/// <summary>
/// Проводка бирж: фабрика CCXT и карта возможностей должны обновляться вместе,
/// иначе новая биржа либо отсекается на старте («не встроена в фабрику»),
/// либо торгуется без post-only/IOC. Пример — подключение BingX.
/// </summary>
public class ExchangeWiringTests
{
    private sealed class SilentEventLog : IEventLog
    {
        public void Log(AppLogLevel level, string message)
        {
        }

        public IReadOnlyList<LogEvent> Latest(int count) => [];

        public IReadOnlyList<LogEvent> Tail(long afterId, int max) => [];

        public IReadOnlyList<LogEvent> Errors(int count) => [];
    }

    private static CcxtExchangeFactory Factory() => new(new SilentEventLog(), TimeProvider.System, NullLoggerFactory.Instance);

    [Fact]
    public void Every_factory_exchange_has_explicit_capabilities()
    {
        var missing = Factory().SupportedIds
            .Where(id => !ExchangeCapabilityMap.KnownIds.Contains(id, StringComparer.OrdinalIgnoreCase))
            .ToList();

        Assert.Empty(missing);
    }

    [Fact]
    public void Every_capability_entry_is_instantiable_by_factory()
    {
        foreach (var id in ExchangeCapabilityMap.KnownIds)
        {
            Assert.NotNull(CcxtExchangeFactory.Instantiate(id, new Dictionary<string, object>()));
        }
    }

    [Fact]
    public void Bingx_is_built_into_the_factory()
    {
        Assert.Contains("bingx", Factory().SupportedIds);

        var connector = Factory().Create(new ExchangeConfigEntry { Id = "bingx" }, NetworkMode.DryRun);
        Assert.Equal("bingx", connector.Id);
    }

    [Fact]
    public void Bingx_supports_post_only_and_ioc_but_neither_fok_nor_native_chase()
    {
        var caps = ExchangeCapabilityMap.For("bingx");

        Assert.Equal("bingx", caps.Id);
        Assert.True(caps.SupportsLimit);
        Assert.True(caps.SupportsCancel);
        Assert.True(caps.SupportsFetchOrder);
        Assert.Equal("PostOnly", caps.PostOnlyToken);
        Assert.True(caps.SupportsIoc);
        Assert.False(caps.SupportsFok);
        Assert.False(caps.SupportsNativeChase);
        Assert.True(caps.SupportsSimulatedChase);
    }

    [Fact]
    public void Bingx_execution_section_passes_validator()
    {
        var options = new BotOptions();
        options.Exchanges.Items.Add(new ExchangeConfigEntry
        {
            Id = "bingx",
            Execution = new OrderExecutionOptions { Type = OrderType.ChaseLimit, TimeInForce = TimeInForce.PostOnly },
        });

        var report = OrderConfigValidator.Validate(options, Factory().SupportedIds);

        Assert.True(report.IsValid);
        Assert.DoesNotContain(report.Errors, error => error.Contains("встроена"));
    }
}
