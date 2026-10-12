"""Развёртывание execution в политику: значения по умолчанию, слои настроек и безопасное понижение."""

import pytest

from futures_arb_bot.core.domain.enums import ChaseMode, OrderType, TimeInForce
from futures_arb_bot.core.domain.options import ChaseOptions, OrderExecutionOptions
from futures_arb_bot.core.domain.order_execution import ExchangeCapabilityMap, OrderPolicyResolver

from .fakes import D

BINANCE = ExchangeCapabilityMap.for_id("binanceusdm")
OKX = ExchangeCapabilityMap.for_id("okx")
KUCOIN = ExchangeCapabilityMap.for_id("kucoinfutures")


def test_entry_defaults_to_marketable_limit_without_chase():
    policy = OrderPolicyResolver.resolve_entry(None, None, BINANCE)

    assert policy.type == OrderType.LIMIT
    assert policy.time_in_force == TimeInForce.GTC
    assert policy.limit_offset_bps == D(0)
    assert policy.chase is None
    assert policy.notes == []
    assert not policy.chases_locally


def test_close_defaults_to_market():
    policy = OrderPolicyResolver.resolve_close(None, None, BINANCE)
    assert policy.type == OrderType.MARKET
    assert not policy.requires_price


def test_global_values_apply_and_exchange_override_wins():
    global_ = OrderExecutionOptions(type=OrderType.CHASE_LIMIT, limit_offset_bps=D(5), chase=ChaseOptions(max_steps=5, step_bps=D(10)))
    per_exchange = OrderExecutionOptions(limit_offset_bps=D(20), chase=ChaseOptions(max_steps=2))

    policy = OrderPolicyResolver.resolve_entry(global_, per_exchange, OKX)

    assert policy.type == OrderType.CHASE_LIMIT
    assert policy.limit_offset_bps == D(20)  # переопределение биржи
    assert policy.chase is not None
    assert policy.chase.max_steps == 2  # переопределение биржи
    assert policy.chase.step_bps == D(10)  # унаследовано из глобальных настроек
    assert policy.chase.step_interval_ms == OrderPolicyResolver.DEFAULT_STEP_INTERVAL_MS
    assert policy.chases_locally  # Simulated по умолчанию
    assert policy.requires_price


def test_market_type_drops_offset_and_time_in_force():
    options = OrderExecutionOptions(type=OrderType.MARKET, limit_offset_bps=D(15), time_in_force=TimeInForce.POST_ONLY)

    policy = OrderPolicyResolver.resolve_entry(options, None, OKX)

    assert policy.type == OrderType.MARKET
    assert policy.limit_offset_bps == D(0)
    assert policy.time_in_force == TimeInForce.GTC


def test_post_only_downgrades_to_gtc_where_unsupported():
    options = OrderExecutionOptions(type=OrderType.LIMIT, time_in_force=TimeInForce.POST_ONLY)

    supported = OrderPolicyResolver.resolve_entry(options, None, OKX)
    unsupported = OrderPolicyResolver.resolve_entry(options, None, BINANCE)

    assert supported.time_in_force == TimeInForce.POST_ONLY
    assert supported.notes == []
    assert unsupported.time_in_force == TimeInForce.GTC
    assert any("Post-only" in n for n in unsupported.notes)


def test_native_chase_falls_back_to_simulated_where_not_supported():
    options = OrderExecutionOptions(type=OrderType.CHASE_LIMIT, chase=ChaseOptions(mode=ChaseMode.NATIVE, params={"chaseType": "priceChase"}))

    unsupported = OrderPolicyResolver.resolve_entry(options, None, BINANCE)
    assert unsupported.chase is not None
    assert unsupported.chase.mode == ChaseMode.SIMULATED
    assert unsupported.exchange_params is None
    assert any("нативный chase" in n for n in unsupported.notes)

    supported = OrderPolicyResolver.resolve_entry(options, None, KUCOIN)
    assert supported.chase is not None
    assert supported.chase.mode == ChaseMode.NATIVE
    assert supported.exchange_params is not None
    assert supported.exchange_params["chaseType"] == "priceChase"


def test_native_chase_without_params_is_not_sent_as_plain_limit():
    options = OrderExecutionOptions(type=OrderType.CHASE_LIMIT, chase=ChaseOptions(mode=ChaseMode.NATIVE))

    policy = OrderPolicyResolver.resolve_entry(options, None, KUCOIN)

    # без нативных параметров chase-ордер неотличим от лимитного → понижаем до Simulated
    assert policy.chase is not None
    assert policy.chase.mode == ChaseMode.SIMULATED
    assert any("Params" in n for n in policy.notes)


def test_chase_mode_none_becomes_plain_limit_with_note():
    options = OrderExecutionOptions(type=OrderType.CHASE_LIMIT, chase=ChaseOptions(mode=ChaseMode.NONE))

    policy = OrderPolicyResolver.resolve_entry(options, None, BINANCE)

    assert policy.type == OrderType.LIMIT
    assert policy.chase is None
    assert any("Chase:Mode" in n for n in policy.notes)


@pytest.mark.parametrize("time_in_force", [TimeInForce.IOC, TimeInForce.FOK])
def test_local_chase_conflicts_with_ioc_and_fok(time_in_force):
    # биржа сама снимает остаток такой заявки — переставлять нечего
    options = OrderExecutionOptions(type=OrderType.CHASE_LIMIT, time_in_force=time_in_force, chase=ChaseOptions(mode=ChaseMode.SIMULATED))

    policy = OrderPolicyResolver.resolve_entry(options, None, OKX)

    assert policy.type == OrderType.LIMIT
    assert policy.chase is None
    assert policy.time_in_force == time_in_force
    assert any("догонание невозможно" in n for n in policy.notes)


def test_chase_on_close_is_downgraded_to_market():
    options = OrderExecutionOptions(close=OrderExecutionOptions(type=OrderType.CHASE_LIMIT))

    policy = OrderPolicyResolver.resolve_close(options, None, KUCOIN)

    assert policy.type == OrderType.MARKET
    assert any("закрытии" in n for n in policy.notes)


def test_deviation_budget_below_one_step_is_clamped_to_step():
    options = OrderExecutionOptions(type=OrderType.CHASE_LIMIT, chase=ChaseOptions(step_bps=D(20), max_deviation_bps=D(5)))

    policy = OrderPolicyResolver.resolve_entry(options, None, BINANCE)

    assert policy.chase is not None
    assert policy.chase.max_deviation_bps == D(20)


def test_unknown_exchange_gets_basic_capabilities_only():
    caps = ExchangeCapabilityMap.for_id("some-new-exchange")

    assert caps.supports_limit
    assert caps.supports_simulated_chase
    assert not caps.supports_post_only
    assert not caps.supports_ioc
    assert not caps.supports_native_chase
