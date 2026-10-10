"""Развёртывание настроек Execution в политику заявки — порт OrderPolicyResolverTests.cs."""

from decimal import Decimal

from futures_arb_bot.domain.enums import ChaseMode, OrderType, TimeInForce
from futures_arb_bot.domain.options import ChaseOptions, OrderExecutionOptions
from futures_arb_bot.domain.order_execution import ExchangeCapabilityMap, OrderPolicyResolver

D = Decimal

Binance = ExchangeCapabilityMap.For("binanceusdm")
Okx = ExchangeCapabilityMap.For("okx")
Kucoin = ExchangeCapabilityMap.For("kucoinfutures")


def test_entry_defaults_to_marketable_limit_without_chase():
    policy = OrderPolicyResolver.ResolveEntry(None, None, Binance)

    assert policy.Type == OrderType.Limit
    assert policy.TimeInForce == TimeInForce.Gtc
    assert policy.LimitOffsetBps == D("0")
    assert policy.Chase is None
    assert not policy.Notes
    assert not policy.ChasesLocally


def test_close_defaults_to_market():
    policy = OrderPolicyResolver.ResolveClose(None, None, Binance)

    assert policy.Type == OrderType.Market
    assert not policy.RequiresPrice


def test_global_values_apply_and_exchange_override_wins():
    global_section = OrderExecutionOptions(
        Type=OrderType.ChaseLimit,
        LimitOffsetBps=D("5"),
        Chase=ChaseOptions(MaxSteps=5, StepBps=D("10")),
    )
    per_exchange = OrderExecutionOptions(LimitOffsetBps=D("20"), Chase=ChaseOptions(MaxSteps=2))

    policy = OrderPolicyResolver.ResolveEntry(global_section, per_exchange, Okx)

    assert policy.Type == OrderType.ChaseLimit
    assert policy.LimitOffsetBps == D("20")  # переопределение биржи
    assert policy.Chase.MaxSteps == 2  # переопределение биржи
    assert policy.Chase.StepBps == D("10")  # унаследовано из глобальных настроек
    assert policy.Chase.StepIntervalMs == OrderPolicyResolver.DEFAULT_STEP_INTERVAL_MS
    assert policy.ChasesLocally  # Simulated по умолчанию
    assert policy.RequiresPrice


def test_market_type_drops_offset_and_time_in_force():
    options = OrderExecutionOptions(Type=OrderType.Market, LimitOffsetBps=D("15"), TimeInForce=TimeInForce.PostOnly)

    policy = OrderPolicyResolver.ResolveEntry(options, None, Okx)

    assert policy.Type == OrderType.Market
    assert policy.LimitOffsetBps == D("0")
    assert policy.TimeInForce == TimeInForce.Gtc


def test_post_only_downgrades_to_gtc_where_unsupported():
    options = OrderExecutionOptions(Type=OrderType.Limit, TimeInForce=TimeInForce.PostOnly)

    supported = OrderPolicyResolver.ResolveEntry(options, None, Okx)
    unsupported = OrderPolicyResolver.ResolveEntry(options, None, Binance)

    assert supported.TimeInForce == TimeInForce.PostOnly
    assert not supported.Notes

    assert unsupported.TimeInForce == TimeInForce.Gtc
    assert any("Post-only" in note for note in unsupported.Notes)


def test_native_chase_falls_back_to_simulated_where_unsupported():
    options = OrderExecutionOptions(
        Type=OrderType.ChaseLimit,
        Chase=ChaseOptions(Mode=ChaseMode.Native, Params={"chaseType": "priceChase"}),
    )

    unsupported = OrderPolicyResolver.ResolveEntry(options, None, Binance)
    assert unsupported.Chase.Mode == ChaseMode.Simulated
    assert any("нативный chase" in note for note in unsupported.Notes)

    supported = OrderPolicyResolver.ResolveEntry(options, None, Kucoin)
    assert supported.Chase.Mode == ChaseMode.Native
    assert supported.ExchangeParams["chaseType"] == "priceChase"


def test_native_chase_without_params_is_not_sent_as_plain_limit():
    options = OrderExecutionOptions(
        Type=OrderType.ChaseLimit,
        Chase=ChaseOptions(Mode=ChaseMode.Native),
    )

    policy = OrderPolicyResolver.ResolveEntry(options, None, Kucoin)

    # без нативных параметров chase-ордер неотличим от лимитного → понижаем до Simulated
    assert policy.Chase.Mode == ChaseMode.Simulated
    assert any("Params" in note for note in policy.Notes)


def test_chase_mode_none_becomes_plain_limit_with_note():
    options = OrderExecutionOptions(
        Type=OrderType.ChaseLimit,
        Chase=ChaseOptions(Mode=ChaseMode.None_),
    )

    policy = OrderPolicyResolver.ResolveEntry(options, None, Binance)

    assert policy.Type == OrderType.Limit
    assert policy.Chase is None
    assert any("Chase:Mode" in note for note in policy.Notes)


def test_local_chase_conflicts_with_ioc_and_fok():
    # биржа сама снимает остаток такой заявки — переставлять нечего
    for time_in_force in (TimeInForce.Ioc, TimeInForce.Fok):
        options = OrderExecutionOptions(
            Type=OrderType.ChaseLimit,
            TimeInForce=time_in_force,
            Chase=ChaseOptions(Mode=ChaseMode.Simulated),
        )

        policy = OrderPolicyResolver.ResolveEntry(options, None, Okx)

        assert policy.Type == OrderType.Limit
        assert policy.Chase is None
        assert policy.TimeInForce == time_in_force
        assert any("догонание невозможно" in note for note in policy.Notes)


def test_chase_on_close_is_downgraded_to_market():
    options = OrderExecutionOptions(Close=OrderExecutionOptions(Type=OrderType.ChaseLimit))

    policy = OrderPolicyResolver.ResolveClose(options, None, Kucoin)

    assert policy.Type == OrderType.Market
    assert any("закрытии" in note for note in policy.Notes)


def test_deviation_budget_below_one_step_is_clamped_to_step():
    options = OrderExecutionOptions(
        Type=OrderType.ChaseLimit,
        Chase=ChaseOptions(StepBps=D("20"), MaxDeviationBps=D("5")),
    )

    policy = OrderPolicyResolver.ResolveEntry(options, None, Binance)

    assert policy.Chase.MaxDeviationBps == D("20")


def test_unknown_exchange_gets_basic_capabilities_only():
    caps = ExchangeCapabilityMap.For("some-new-exchange")

    assert caps.SupportsLimit
    assert caps.SupportsSimulatedChase
    assert not caps.SupportsPostOnly
    assert not caps.SupportsIoc
    assert not caps.SupportsNativeChase
