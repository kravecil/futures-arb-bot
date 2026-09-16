using FuturesArbBot.Core.Domain;

namespace FuturesArbBot.Tests;

/// <summary>
/// Проверка раздела Execution при старте: что блокирует запуск, а что лечится
/// предупреждением (политика исполнения умеет безопасно понижать настройки).
/// </summary>
public class OrderConfigValidatorTests
{
    private static readonly string[] KnownIds = ["binanceusdm", "bybit", "kucoinfutures"];

    private static BotOptions Options(OrderExecutionOptions execution, OrderExecutionOptions? perExchange = null)
    {
        var options = new BotOptions();
        options.Arbitrage.Execution = execution;
        options.Exchanges.Items.Add(new ExchangeConfigEntry { Id = "bybit", Execution = perExchange });
        return options;
    }

    [Fact]
    public void Empty_configuration_is_valid()
    {
        var report = OrderConfigValidator.Validate(new BotOptions(), KnownIds);

        Assert.True(report.IsValid);
        Assert.Empty(report.Warnings);
    }

    [Fact]
    public void Out_of_range_chase_values_block_startup()
    {
        var report = OrderConfigValidator.Validate(Options(new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions { MaxSteps = 0, StepIntervalMs = 10, StepBps = 5_000m },
        }), KnownIds);

        Assert.False(report.IsValid);
        Assert.Contains(report.Errors, error => error.Contains("Chase.MaxSteps"));
        Assert.Contains(report.Errors, error => error.Contains("Chase.StepIntervalMs"));
        Assert.Contains(report.Errors, error => error.Contains("Chase.StepBps"));
    }

    [Fact]
    public void Native_chase_without_params_block_startup()
    {
        var report = OrderConfigValidator.Validate(Options(new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions { Mode = ChaseMode.Native },
        }), KnownIds);

        Assert.False(report.IsValid);
        Assert.Contains(report.Errors, error => error.Contains("Params"));
    }

    [Fact]
    public void Blank_raw_param_blocks_startup()
    {
        var report = OrderConfigValidator.Validate(Options(new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions { Mode = ChaseMode.Native, Params = new Dictionary<string, string> { [""] = "priceChase" } },
        }), KnownIds);

        Assert.False(report.IsValid);
        Assert.Contains(report.Errors, error => error.Contains("не должны быть пустыми"));
    }

    [Fact]
    public void Nested_close_section_is_rejected()
    {
        var report = OrderConfigValidator.Validate(Options(new OrderExecutionOptions
        {
            Close = new OrderExecutionOptions { Close = new OrderExecutionOptions { Type = OrderType.Limit } },
        }), KnownIds);

        Assert.False(report.IsValid);
        Assert.Contains(report.Errors, error => error.Contains("Close:Close"));
    }

    [Fact]
    public void Execution_on_unknown_exchange_is_an_error()
    {
        var options = new BotOptions();
        options.Exchanges.Items.Add(new ExchangeConfigEntry { Id = "some-new-exchange", Execution = new OrderExecutionOptions { Type = OrderType.Market } });

        var report = OrderConfigValidator.Validate(options, KnownIds);

        Assert.False(report.IsValid);
        Assert.Contains(report.Errors, error => error.Contains("не встроена"));
    }

    [Fact]
    public void Chase_on_close_and_market_with_tif_are_warnings_only()
    {
        var report = OrderConfigValidator.Validate(Options(new OrderExecutionOptions
        {
            Type = OrderType.Market,
            TimeInForce = TimeInForce.PostOnly,
            Close = new OrderExecutionOptions { Type = OrderType.ChaseLimit, Chase = new ChaseOptions { MaxSteps = 2 } },
        }), KnownIds);

        Assert.True(report.IsValid);
        Assert.Contains(report.Warnings, warning => warning.Contains("закрытии"));
        Assert.Contains(report.Warnings, warning => warning.Contains("для market-ордера"));
    }

    [Fact]
    public void Chase_with_ioc_is_reported_as_conflict()
    {
        var report = OrderConfigValidator.Validate(Options(new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            TimeInForce = TimeInForce.Ioc,
        }), KnownIds);

        Assert.True(report.IsValid);
        Assert.Contains(report.Warnings, warning => warning.Contains("локальное догонание невозможно"));
    }

    [Fact]
    public void Chase_budget_larger_than_execution_timeout_is_reported()
    {
        var options = new BotOptions();
        options.Arbitrage.OrderExecutionTimeoutMs = 3_000;
        options.Arbitrage.Execution = new OrderExecutionOptions
        {
            Type = OrderType.ChaseLimit,
            Chase = new ChaseOptions { MaxSteps = 20, StepIntervalMs = 1_000 },
        };

        var report = OrderConfigValidator.Validate(options, KnownIds);

        Assert.True(report.IsValid);
        Assert.Contains(report.Warnings, warning => warning.Contains("OrderExecutionTimeoutMs"));
    }

    [Fact]
    public void Chase_section_without_chase_limit_type_is_reported()
    {
        var report = OrderConfigValidator.Validate(Options(new OrderExecutionOptions
        {
            Type = OrderType.Limit,
            Chase = new ChaseOptions { MaxSteps = 3 },
        }), KnownIds);

        Assert.True(report.IsValid);
        Assert.Contains(report.Warnings, warning => warning.Contains("ChaseLimit"));
    }
}
