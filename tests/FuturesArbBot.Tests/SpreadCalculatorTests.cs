using FuturesArbBot.Core.Engine;
using FuturesArbBot.Tests;

namespace FuturesArbBot.Tests;

public class SpreadCalculatorTests
{
    private static readonly DateTimeOffset Now = new(2026, 1, 1, 12, 0, 0, TimeSpan.Zero);

    private static SpreadCalculator CreateCalculator(decimal slippage = 0.03m)
    {
        var config = new FakeConfigProvider();
        config.Current.Arbitrage.SlippageBufferPercent = slippage;
        config.Current.Arbitrage.IncludeFees = true;
        return new SpreadCalculator(config, new FakeTimeProvider(Now));
    }

    [Fact]
    public void Net_subtracts_fees_and_slippage()
    {
        var cheap = TestTickers.Make("a", "BTC/USDT:USDT", bid: 99.9m, ask: 100m);
        var rich = TestTickers.Make("b", "BTC/USDT:USDT", bid: 101m, ask: 101.1m);

        var estimate = CreateCalculator().Calculate(cheap, rich, new ExchangeFees(0.1m, 0.02m), new ExchangeFees(0.1m, 0.02m));

        Assert.NotNull(estimate);
        // gross = (101 − 100) / 100 × 100 = 1.0000 %
        Assert.Equal(1m, estimate.GrossPercent);
        // net = 1 − (0.1 + 0.1) × 2 (вход и выход обеих ног) − 0.03 = 0.5700 %
        // раньше считалось только открытие (0.7700 %) — edge, которого на самом деле нет
        Assert.Equal(0.57m, estimate.NetPercent);
        Assert.Equal("a", estimate.LongLeg.ExchangeId);
        Assert.Equal("b", estimate.ShortLeg.ExchangeId);
        Assert.Equal(100m, estimate.LongLeg.Price);
        Assert.Equal(101m, estimate.ShortLeg.Price);
    }

    [Fact]
    public void Net_subtracts_fees_of_the_full_round_trip()
    {
        var cheap = TestTickers.Make("a", "BTC/USDT:USDT", bid: 99.9m, ask: 100m);
        var rich = TestTickers.Make("b", "BTC/USDT:USDT", bid: 101m, ask: 101.1m);

        // taker 0.1 % на ноге: вход стоит 0.2 %, выход ещё 0.2 % — вместе 0.4 %, буфер выключен
        var estimate = CreateCalculator(slippage: 0m)
            .Calculate(cheap, rich, new ExchangeFees(0.1m, 0.02m), new ExchangeFees(0.1m, 0.02m));

        Assert.NotNull(estimate);
        Assert.Equal(1m, estimate.GrossPercent);
        // было бы 0.8 % при учёте только входа — этого edge на самом деле не существует
        Assert.Equal(0.6m, estimate.NetPercent);
    }

    [Fact]
    public void Signal_is_rejected_when_round_trip_costs_eat_the_gross_spread()
    {
        var cheap = TestTickers.Make("a", "BTC/USDT:USDT", bid: 99.9m, ask: 100m);
        var rich = TestTickers.Make("b", "BTC/USDT:USDT", bid: 100.55m, ask: 100.6m);

        // gross = 0.55 %: на одном входе хватило бы и комиссий (0.3 %), и буфера (0.03 %),
        // но полный круг стоит 0.6 % — сигнал должен отбрасываться, а не открываться в минус
        var estimate = CreateCalculator().Calculate(cheap, rich, new ExchangeFees(0.15m, 0m), new ExchangeFees(0.15m, 0m));

        Assert.Null(estimate);
    }

    [Fact]
    public void Returns_null_when_prices_do_not_overlap()
    {
        var cheap = TestTickers.Make("a", "BTC/USDT:USDT", 99.8m, 100m);
        var rich = TestTickers.Make("b", "BTC/USDT:USDT", 99.9m, 100.1m);

        var estimate = CreateCalculator().Calculate(cheap, rich, new ExchangeFees(0.1m, 0m), new ExchangeFees(0.1m, 0m));

        Assert.Null(estimate);
    }

    [Fact]
    public void Returns_null_for_same_exchange()
    {
        var cheap = TestTickers.Make("a", "BTC/USDT:USDT", 99.8m, 100m);
        var rich = TestTickers.Make("a", "BTC/USDT:USDT", 101m, 101.1m);

        var estimate = CreateCalculator().Calculate(cheap, rich, new ExchangeFees(0.1m, 0m), new ExchangeFees(0.1m, 0m));

        Assert.Null(estimate);
    }

    [Fact]
    public void Net_positive_without_fees_when_include_fees_disabled()
    {
        var config = new FakeConfigProvider();
        config.Current.Arbitrage.IncludeFees = false;
        config.Current.Arbitrage.SlippageBufferPercent = 0m;
        var calculator = new SpreadCalculator(config, new FakeTimeProvider(Now));

        var cheap = TestTickers.Make("a", "BTC/USDT:USDT", 99.9m, 100m);
        var rich = TestTickers.Make("b", "BTC/USDT:USDT", 100.2m, 100.3m);

        var estimate = calculator.Calculate(cheap, rich, new ExchangeFees(0.5m, 0m), new ExchangeFees(0.5m, 0m));

        Assert.NotNull(estimate);
        Assert.Equal(0.2m, estimate.NetPercent); // комиссии не вычитались
    }

    [Fact]
    public void Ignores_absurd_spreads_above_sanity_cap()
    {
        var config = new FakeConfigProvider();
        config.Current.Arbitrage.MaxSpreadPercent = 5m;
        var calculator = new SpreadCalculator(config, new FakeTimeProvider(Now));

        // расхождение 1900 % — типичная аномалия новых листингов, а не арбитраж
        var cheap = TestTickers.Make("a", "BTC/USDT:USDT", 9.9m, 10m);
        var rich = TestTickers.Make("b", "BTC/USDT:USDT", 200m, 200.2m);

        Assert.Null(calculator.Calculate(cheap, rich, new ExchangeFees(0.1m, 0m), new ExchangeFees(0.1m, 0m)));
    }
}

/// <summary>Фиксированный TimeProvider.</summary>
public sealed class FakeTimeProvider(DateTimeOffset now) : TimeProvider
{
    public override DateTimeOffset GetUtcNow() => now;
}
