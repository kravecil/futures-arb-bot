using FuturesArbBot.Core.Domain;

namespace FuturesArbBot.Tests;

/// <summary>
/// Перевод политики заявки в params-словарь CCXT: reduceOnly, timeInForce,
/// post-only-токены бирж и сырые параметры нативного chase.
/// </summary>
public class OrderParamsBuilderTests
{
    private static readonly ExchangeCapabilities Binance = ExchangeCapabilityMap.For("binanceusdm");
    private static readonly ExchangeCapabilities Okx = ExchangeCapabilityMap.For("okx");
    private static readonly ExchangeCapabilities Kucoin = ExchangeCapabilityMap.For("kucoinfutures");
    private static readonly ExchangeCapabilities Bybit = ExchangeCapabilityMap.For("bybit");

    private static OrderRequest Request(
        OrderType type = OrderType.Limit,
        TimeInForce timeInForce = TimeInForce.Gtc,
        bool reduceOnly = false,
        Dictionary<string, string>? parameters = null) =>
        new("BTC/USDT:USDT", OrderSide.Buy, 1m, reduceOnly, type, 100m, timeInForce, parameters);

    [Fact]
    public void Market_order_carries_only_reduce_only()
    {
        Assert.Empty(OrderParamsBuilder.Build(Request(OrderType.Market, parameters: null), Binance));

        var reduceOnly = OrderParamsBuilder.Build(
            new OrderRequest("BTC/USDT:USDT", OrderSide.Sell, 1m, ReduceOnly: true, Type: OrderType.Market), Binance);

        Assert.Equal(true, reduceOnly["reduceOnly"]);
        Assert.Single(reduceOnly);
    }

    [Fact]
    public void Plain_gtc_limit_adds_no_time_in_force()
    {
        Assert.Empty(OrderParamsBuilder.Build(Request(), Okx));
    }

    [Fact]
    public void Ioc_and_fok_become_time_in_force()
    {
        Assert.Equal("IOC", OrderParamsBuilder.Build(Request(timeInForce: TimeInForce.Ioc), Okx)["timeInForce"]);
        Assert.Equal("FOK", OrderParamsBuilder.Build(Request(timeInForce: TimeInForce.Fok), Okx)["timeInForce"]);
    }

    [Fact]
    public void Post_only_uses_token_of_the_exchange()
    {
        Assert.Equal("PO", OrderParamsBuilder.Build(Request(timeInForce: TimeInForce.PostOnly), Okx)["timeInForce"]);
        Assert.Equal("PostOnly", OrderParamsBuilder.Build(Request(timeInForce: TimeInForce.PostOnly), Bybit)["timeInForce"]);
    }

    [Fact]
    public void Post_only_without_token_falls_back_to_boolean_flag()
    {
        // страховка: политика должна понизить post-only до GTC, но мимо неё проходить не будем
        var parameters = OrderParamsBuilder.Build(Request(timeInForce: TimeInForce.PostOnly), Binance);

        Assert.Equal(true, parameters["postOnly"]);
        Assert.False(parameters.ContainsKey("timeInForce"));
    }

    [Fact]
    public void Native_chase_params_are_merged_with_reduce_only()
    {
        var parameters = OrderParamsBuilder.Build(
            Request(OrderType.ChaseLimit, TimeInForce.Ioc, reduceOnly: true, new Dictionary<string, string> { ["chaseType"] = "priceChase" }),
            Kucoin);

        Assert.Equal(true, parameters["reduceOnly"]);
        Assert.Equal("priceChase", parameters["chaseType"]);
        Assert.Equal("IOC", parameters["timeInForce"]);
    }

    [Fact]
    public void Raw_exchange_params_win_over_generated_ones()
    {
        var parameters = OrderParamsBuilder.Build(
            Request(OrderType.ChaseLimit, TimeInForce.Ioc, parameters: new Dictionary<string, string> { ["timeInForce"] = "4" }),
            Kucoin);

        // сырой параметр — осознанный ручной выбор пользователя
        Assert.Equal("4", parameters["timeInForce"]);
    }
}
