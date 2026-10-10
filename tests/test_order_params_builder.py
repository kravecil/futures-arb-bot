"""Порт OrderParamsBuilderTests.cs — перевод политики заявки в params CCXT."""

from decimal import Decimal

from futures_arb_bot.domain.enums import OrderSide, OrderType, TimeInForce
from futures_arb_bot.domain.market_data import OrderRequest
from futures_arb_bot.domain.order_execution import ExchangeCapabilityMap, OrderParamsBuilder

D = Decimal

Binance = ExchangeCapabilityMap.For("binanceusdm")
Okx = ExchangeCapabilityMap.For("okx")
Gate = ExchangeCapabilityMap.For("gate")


def request(**changes) -> OrderRequest:
    base = dict(Symbol="BTC/USDT:USDT", Side=OrderSide.Buy, Amount=D("1"))
    base.update(changes)
    return OrderRequest(**base)


def test_market_no_params():
    params = OrderParamsBuilder.Build(request(Type=OrderType.Market), Okx)
    assert params == {}


def test_reduce_only_flag():
    params = OrderParamsBuilder.Build(request(Type=OrderType.Market, ReduceOnly=True), Okx)
    assert params == {"reduceOnly": True}


def test_gtc_not_sent():
    params = OrderParamsBuilder.Build(request(Type=OrderType.Limit, Price=D("100")), Okx)
    assert params == {}


def test_ioc_fok_upper_cased():
    assert OrderParamsBuilder.Build(request(Type=OrderType.Limit, TimeInForce=TimeInForce.Ioc), Okx) == {"timeInForce": "IOC"}
    assert OrderParamsBuilder.Build(request(Type=OrderType.Limit, TimeInForce=TimeInForce.Fok), Binance) == {"timeInForce": "FOK"}


def test_post_only_uses_exchange_token():
    okx = OrderParamsBuilder.Build(request(Type=OrderType.Limit, TimeInForce=TimeInForce.PostOnly), Okx)
    assert okx == {"timeInForce": "PO"}

    gate = OrderParamsBuilder.Build(request(Type=OrderType.Limit, TimeInForce=TimeInForce.PostOnly), Gate)
    assert gate == {"timeInForce": "post_only"}

    # у биржи без токена — страховка postOnly=true (политика до этого уже понизила бы до GTC)
    binance = OrderParamsBuilder.Build(request(Type=OrderType.Limit, TimeInForce=TimeInForce.PostOnly), Binance)
    assert binance == {"postOnly": True}


def test_chase_limit_type_still_carries_tif():
    order = request(Type=OrderType.ChaseLimit, Price=D("100"), TimeInForce=TimeInForce.Ioc)
    assert OrderParamsBuilder.Build(order, Okx) == {"timeInForce": "IOC"}


def test_raw_exchange_params_override():
    order = request(
        Type=OrderType.ChaseLimit,
        Price=D("100"),
        TimeInForce=TimeInForce.Ioc,
        ExchangeParams={"timeInForce": "GTC", "chaseType": "priceChase"},
    )
    params = OrderParamsBuilder.Build(order, Okx)
    assert params["timeInForce"] == "GTC"
    assert params["chaseType"] == "priceChase"
