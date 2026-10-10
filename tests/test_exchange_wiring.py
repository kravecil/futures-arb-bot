"""Порт ExchangeWiringTests + OrderConfigValidatorTests (существенные кейсы)."""

from __future__ import annotations

from decimal import Decimal

from fakes import FakeConfigProvider

from futures_arb_bot.domain.enums import ChaseMode, OrderType, TimeInForce
from futures_arb_bot.domain.options import (
    BotOptions,
    ChaseOptions,
    ExchangeConfigEntry,
    OrderExecutionOptions,
)
from futures_arb_bot.domain.order_config_validator import OrderConfigValidator
from futures_arb_bot.domain.order_execution import ExchangeCapabilityMap
from futures_arb_bot.exchange.factory import SUPPORTED_IDS

D = Decimal


def test_factory_and_capability_map_cover_same_exchanges():
    """Карта возможностей покрывает всех бирж фабрики (как в C#: таблица сверяется с фабрикой)."""

    known = ExchangeCapabilityMap.KnownIds()
    for exchange_id in SUPPORTED_IDS:
        assert exchange_id in known, exchange_id


def test_capability_map_values_match_csharp_table():
    okx = ExchangeCapabilityMap.For("okx")
    assert okx.PostOnlyToken == "PO"
    assert okx.SupportsIoc and okx.SupportsFok and not okx.SupportsNativeChase

    gate = ExchangeCapabilityMap.For("gate")
    assert gate.PostOnlyToken == "post_only"
    assert gate.SupportsIoc and not gate.SupportsFok

    kucoin = ExchangeCapabilityMap.For("kucoinfutures")
    assert kucoin.SupportsNativeChase
    assert kucoin.PostOnlyToken is None

    mexc = ExchangeCapabilityMap.For("mexc")
    assert not mexc.SupportsIoc and not mexc.SupportsFok and mexc.PostOnlyToken is None


def validate(options: BotOptions):
    return OrderConfigValidator.Validate(options, SUPPORTED_IDS)


def test_valid_default_config_has_no_errors():
    validation = validate(FakeConfigProvider().Current)
    assert validation.IsValid
    assert not validation.Errors


def test_nested_close_close_is_error():
    options = FakeConfigProvider().Current
    options.Arbitrage.Execution = OrderExecutionOptions(Close=OrderExecutionOptions(Close=OrderExecutionOptions()))

    validation = validate(options)
    assert not validation.IsValid
    assert any("Close:Close" in e for e in validation.Errors)


def test_native_chase_without_params_is_error():
    options = FakeConfigProvider().Current
    options.Arbitrage.Execution = OrderExecutionOptions(Type=OrderType.ChaseLimit, Chase=ChaseOptions(Mode=ChaseMode.Native))

    validation = validate(options)
    assert any("Mode = Native требует" in e for e in validation.Errors)


def test_chase_budget_over_timeout_warns():
    options = FakeConfigProvider().Current
    options.Arbitrage.OrderExecutionTimeoutMs = 1000
    options.Arbitrage.Execution = OrderExecutionOptions(
        Type=OrderType.ChaseLimit, Chase=ChaseOptions(MaxSteps=10, StepIntervalMs=400)
    )

    validation = validate(options)
    assert validation.IsValid  # предупреждение, не ошибка
    assert any("бюджет догонания" in w for w in validation.Warnings)


def test_chase_section_with_limit_type_warns():
    options = FakeConfigProvider().Current
    options.Arbitrage.Execution = OrderExecutionOptions(Type=OrderType.Limit, Chase=ChaseOptions(StepBps=D("5")))

    validation = validate(options)
    assert any("догонание не включится" in w for w in validation.Warnings)


def test_ioc_with_local_chase_warns():
    options = FakeConfigProvider().Current
    options.Arbitrage.Execution = OrderExecutionOptions(
        Type=OrderType.ChaseLimit, TimeInForce=TimeInForce.Ioc, Chase=ChaseOptions(Mode=ChaseMode.Simulated)
    )

    validation = validate(options)
    assert any("локальное догонание невозможно" in w for w in validation.Warnings)


def test_out_of_range_values_are_errors():
    options = FakeConfigProvider().Current
    options.Arbitrage.Execution = OrderExecutionOptions(LimitOffsetBps=D("5000"))

    validation = validate(options)
    assert any("LimitOffsetBps" in e and "вне допустимых границ" in e for e in validation.Errors)


def test_unknown_exchange_execution_warns_about_capabilities():
    options = FakeConfigProvider().Current
    options.Exchanges.Items.append(ExchangeConfigEntry(Id="newexchange", Execution=OrderExecutionOptions()))

    validation = validate(options)
    assert any("не встроена в фабрику" in e for e in validation.Errors)
    assert any("возможности биржи не описаны" in w for w in validation.Warnings)
