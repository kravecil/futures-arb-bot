using FuturesArbBot.Core.Domain;

namespace FuturesArbBot.Tests;

/// <summary>
/// Развёртывание настроек Execution в политику заявки: значения по умолчанию,
/// слои «глобальные настройки → переопределение биржи» и безопасное понижение
/// по возможностям биржи.
/// </summary>
public class OrderPolicyResolverTests
{
    private static readonly ExchangeCapabilities Binance = ExchangeCapabilityMap.For("binanceusdm");
    private static readonly ExchangeCapabilities Okx = ExchangeCapabilityMap.For("okx");
    private static readonly ExchangeCapabilities Kucoin = ExchangeCapabilityMap.For("kucoinfutures");

    [Fact]
    public void Entry_defaults_to_marketable_limit_without_chase()
    {
        var policy = OrderPolicyResolver.ResolveEntry(null, null, Binance);

        Assert.Equal(OrderType.Limit, policy.Type);
        Assert.Equal(TimeInForce.Gtc, policy.TimeInForce);
        Assert.Equal(0m, policy.LimitOffsetBps);
        Assert.Null(policy.Chase);
        Assert.Empty(policy.Notes);
        Assert.False(policy.ChasesLocally);
    }

    [Fact]
    public void Close_defaults_to_market()
    {
        var policy = OrderPolicyResolver.ResolveClose(null, null, Binance);

        Assert.Equal(OrderType.Market, policy.Type);
        Assert.False(policy.RequiresPrice);
    }

    [Fact]
    public void Global_values_apply_and_exchange_override_wins()
    {
        var global = new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            LimitOffsetBps = 5m,
            Chase = new ChaseOptions { MaxSteps = 5, StepBps = 10m },
        };
        var perExchange = new OrderExecutionOptions { LimitOffsetBps = 20m, Chase = new ChaseOptions { MaxSteps = 2 } };

        var policy = OrderPolicyResolver.ResolveEntry(global, perExchange, Okx);

        Assert.Equal(OrderType.ChaseLimit, policy.Type);
        Assert.Equal(20m, policy.LimitOffsetBps);        // переопределение биржи
        Assert.Equal(2, policy.Chase!.MaxSteps);         // переопределение биржи
        Assert.Equal(10m, policy.Chase.StepBps);         // унаследовано из глобальных настроек
        Assert.Equal(OrderPolicyResolver.DefaultStepIntervalMs, policy.Chase.StepIntervalMs);
        Assert.True(policy.ChasesLocally);               // Simulated по умолчанию
        Assert.True(policy.RequiresPrice);
    }

    [Fact]
    public void Market_type_drops_offset_and_time_in_force()
    {
        var options = new OrderExecutionOptions { Type = OrderType.Market, LimitOffsetBps = 15m, TimeInForce = TimeInForce.PostOnly };

        var policy = OrderPolicyResolver.ResolveEntry(options, null, Okx);

        Assert.Equal(OrderType.Market, policy.Type);
        Assert.Equal(0m, policy.LimitOffsetBps);
        Assert.Equal(TimeInForce.Gtc, policy.TimeInForce);
    }

    [Fact]
    public void PostOnly_downgrades_to_gtc_where_unsupported()
    {
        var options = new OrderExecutionOptions { Type = OrderType.Limit, TimeInForce = TimeInForce.PostOnly };

        var supported = OrderPolicyResolver.ResolveEntry(options, null, Okx);
        var unsupported = OrderPolicyResolver.ResolveEntry(options, null, Binance);

        Assert.Equal(TimeInForce.PostOnly, supported.TimeInForce);
        Assert.Empty(supported.Notes);

        Assert.Equal(TimeInForce.Gtc, unsupported.TimeInForce);
        Assert.Contains(unsupported.Notes, note => note.Contains("Post-only"));
    }

    [Fact]
    public void Native_chase_falls_back_to_simulated_where_not_supported()
    {
        var options = new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions
            {
                Mode = ChaseMode.Native,
                Params = new Dictionary<string, string> { ["chaseType"] = "priceChase" },
            },
        };

        var unsupported = OrderPolicyResolver.ResolveEntry(options, null, Binance);
        Assert.Equal(ChaseMode.Simulated, unsupported.Chase!.Mode);
        Assert.Null(unsupported.ExchangeParams);
        Assert.Contains(unsupported.Notes, note => note.Contains("нативный chase"));

        var supported = OrderPolicyResolver.ResolveEntry(options, null, Kucoin);
        Assert.Equal(ChaseMode.Native, supported.Chase!.Mode);
        Assert.Equal("priceChase", supported.ExchangeParams!["chaseType"]);
    }

    [Fact]
    public void Native_chase_without_params_is_not_sent_as_plain_limit()
    {
        var options = new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions { Mode = ChaseMode.Native },
        };

        var policy = OrderPolicyResolver.ResolveEntry(options, null, Kucoin);

        // без нативных параметров chase-ордер неотличим от лимитного → понижаем до Simulated
        Assert.Equal(ChaseMode.Simulated, policy.Chase!.Mode);
        Assert.Contains(policy.Notes, note => note.Contains("Params"));
    }

    [Fact]
    public void Chase_mode_none_becomes_plain_limit_with_note()
    {
        var options = new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions { Mode = ChaseMode.None },
        };

        var policy = OrderPolicyResolver.ResolveEntry(options, null, Binance);

        Assert.Equal(OrderType.Limit, policy.Type);
        Assert.Null(policy.Chase);
        Assert.Contains(policy.Notes, note => note.Contains("Chase:Mode"));
    }

    [Fact]
    public void Local_chase_conflicts_with_ioc_and_fok()
    {
        // биржа сама снимает остаток такой заявки — переставлять нечего
        foreach (var timeInForce in new[] { TimeInForce.Ioc, TimeInForce.Fok })
        {
            var options = new OrderExecutionOptions
            {
                Type = OrderType.ChaseLimit,
                TimeInForce = timeInForce,
                Chase = new ChaseOptions { Mode = ChaseMode.Simulated },
            };

            var policy = OrderPolicyResolver.ResolveEntry(options, null, Okx);

            Assert.Equal(OrderType.Limit, policy.Type);
            Assert.Null(policy.Chase);
            Assert.Equal(timeInForce, policy.TimeInForce);
            Assert.Contains(policy.Notes, note => note.Contains("догонание невозможно"));
        }
    }

    [Fact]
    public void Chase_on_close_is_downgraded_to_market()
    {
        var options = new OrderExecutionOptions { Close = new OrderExecutionOptions { Type = OrderType.ChaseLimit } };

        var policy = OrderPolicyResolver.ResolveClose(options, null, Kucoin);

        Assert.Equal(OrderType.Market, policy.Type);
        Assert.Contains(policy.Notes, note => note.Contains("закрытии"));
    }

    [Fact]
    public void Deviation_budget_below_one_step_is_clamped_to_step()
    {
        var options = new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions { StepBps = 20m, MaxDeviationBps = 5m },
        };

        var policy = OrderPolicyResolver.ResolveEntry(options, null, Binance);

        Assert.Equal(20m, policy.Chase!.MaxDeviationBps);
    }

    [Fact]
    public void Unknown_exchange_gets_basic_capabilities_only()
    {
        var caps = ExchangeCapabilityMap.For("some-new-exchange");

        Assert.True(caps.SupportsLimit);
        Assert.True(caps.SupportsSimulatedChase);
        Assert.False(caps.SupportsPostOnly);
        Assert.False(caps.SupportsIoc);
        Assert.False(caps.SupportsNativeChase);
    }
}
