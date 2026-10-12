"""Перевод политики заявки в params CCXT: reduceOnly, timeInForce, post-only-токены и сырые параметры."""

from futures_arb_bot.core.domain.enums import OrderSide, OrderType, TimeInForce
from futures_arb_bot.core.domain.market_data import OrderRequest
from futures_arb_bot.core.domain.order_execution import ExchangeCapabilityMap, OrderParamsBuilder

from .fakes import D

BINANCE = ExchangeCapabilityMap.for_id("binanceusdm")
OKX = ExchangeCapabilityMap.for_id("okx")
KUCOIN = ExchangeCapabilityMap.for_id("kucoinfutures")
BYBIT = ExchangeCapabilityMap.for_id("bybit")


def request(type_=OrderType.LIMIT, tif=TimeInForce.GTC, reduce_only=False, parameters=None) -> OrderRequest:
    return OrderRequest("BTC/USDT:USDT", OrderSide.BUY, D(1), reduce_only, type_, D(100), tif, parameters)


def test_market_order_carries_only_reduce_only():
    assert OrderParamsBuilder.build(request(OrderType.MARKET), BINANCE) == {}

    reduce_only = OrderParamsBuilder.build(OrderRequest("BTC/USDT:USDT", OrderSide.SELL, D(1), reduce_only=True, type=OrderType.MARKET), BINANCE)
    assert reduce_only == {"reduceOnly": True}


def test_plain_gtc_limit_adds_no_time_in_force():
    assert OrderParamsBuilder.build(request(), OKX) == {}


def test_ioc_and_fok_become_time_in_force():
    assert OrderParamsBuilder.build(request(tif=TimeInForce.IOC), OKX)["timeInForce"] == "IOC"
    assert OrderParamsBuilder.build(request(tif=TimeInForce.FOK), OKX)["timeInForce"] == "FOK"


def test_post_only_uses_token_of_the_exchange():
    assert OrderParamsBuilder.build(request(tif=TimeInForce.POST_ONLY), OKX)["timeInForce"] == "PO"
    assert OrderParamsBuilder.build(request(tif=TimeInForce.POST_ONLY), BYBIT)["timeInForce"] == "PostOnly"


def test_post_only_without_token_falls_back_to_boolean_flag():
    # страховка: политика должна понизить post-only до GTC, но мимо неё проходить не будем
    parameters = OrderParamsBuilder.build(request(tif=TimeInForce.POST_ONLY), BINANCE)
    assert parameters["postOnly"] is True
    assert "timeInForce" not in parameters


def test_native_chase_params_are_merged_with_reduce_only():
    parameters = OrderParamsBuilder.build(request(OrderType.CHASE_LIMIT, TimeInForce.IOC, True, {"chaseType": "priceChase"}), KUCOIN)
    assert parameters["reduceOnly"] is True
    assert parameters["chaseType"] == "priceChase"
    assert parameters["timeInForce"] == "IOC"


def test_raw_exchange_params_win_over_generated_ones():
    parameters = OrderParamsBuilder.build(request(OrderType.CHASE_LIMIT, TimeInForce.IOC, parameters={"timeInForce": "4"}), KUCOIN)
    # сырой параметр — осознанный ручной выбор пользователя
    assert parameters["timeInForce"] == "4"
