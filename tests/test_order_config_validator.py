"""Проверка раздела execution при старте: что блокирует запуск, а что лечится предупреждением."""

from futures_arb_bot.core.domain.enums import ChaseMode, OrderType, TimeInForce
from futures_arb_bot.core.domain.options import BotOptions, ChaseOptions, ExchangeConfigEntry, OrderExecutionOptions
from futures_arb_bot.core.domain.order_config_validator import OrderConfigValidator

from .fakes import D

KNOWN_IDS = ["binanceusdm", "bybit", "kucoinfutures"]


def options(execution: OrderExecutionOptions, per_exchange: OrderExecutionOptions | None = None) -> BotOptions:
    result = BotOptions()
    result.arbitrage.execution = execution
    result.exchanges.items.append(ExchangeConfigEntry(id="bybit", execution=per_exchange))
    return result


def test_empty_configuration_is_valid():
    report = OrderConfigValidator.validate(BotOptions(), KNOWN_IDS)
    assert report.is_valid
    assert report.warnings == []


def test_out_of_range_chase_values_block_startup():
    report = OrderConfigValidator.validate(
        options(OrderExecutionOptions(type=OrderType.CHASE_LIMIT, chase=ChaseOptions(max_steps=0, step_interval_ms=10, step_bps=D(5000)))),
        KNOWN_IDS,
    )
    assert not report.is_valid
    assert any("Chase.MaxSteps" in e for e in report.errors)
    assert any("Chase.StepIntervalMs" in e for e in report.errors)
    assert any("Chase.StepBps" in e for e in report.errors)


def test_native_chase_without_params_block_startup():
    report = OrderConfigValidator.validate(
        options(OrderExecutionOptions(type=OrderType.CHASE_LIMIT, chase=ChaseOptions(mode=ChaseMode.NATIVE))), KNOWN_IDS
    )
    assert not report.is_valid
    assert any("Params" in e for e in report.errors)


def test_blank_raw_param_blocks_startup():
    report = OrderConfigValidator.validate(
        options(OrderExecutionOptions(type=OrderType.CHASE_LIMIT, chase=ChaseOptions(mode=ChaseMode.NATIVE, params={"": "priceChase"}))),
        KNOWN_IDS,
    )
    assert not report.is_valid
    assert any("не должны быть пустыми" in e for e in report.errors)


def test_nested_close_section_is_rejected():
    report = OrderConfigValidator.validate(
        options(OrderExecutionOptions(close=OrderExecutionOptions(close=OrderExecutionOptions(type=OrderType.LIMIT)))), KNOWN_IDS
    )
    assert not report.is_valid
    assert any("Close:Close" in e for e in report.errors)


def test_execution_on_unknown_exchange_is_an_error():
    result = BotOptions()
    result.exchanges.items.append(ExchangeConfigEntry(id="some-new-exchange", execution=OrderExecutionOptions(type=OrderType.MARKET)))

    report = OrderConfigValidator.validate(result, KNOWN_IDS)

    assert not report.is_valid
    assert any("не встроена" in e for e in report.errors)


def test_chase_on_close_and_market_with_tif_are_warnings_only():
    report = OrderConfigValidator.validate(
        options(
            OrderExecutionOptions(
                type=OrderType.MARKET,
                time_in_force=TimeInForce.POST_ONLY,
                close=OrderExecutionOptions(type=OrderType.CHASE_LIMIT, chase=ChaseOptions(max_steps=2)),
            )
        ),
        KNOWN_IDS,
    )
    assert report.is_valid
    assert any("закрытии" in w for w in report.warnings)
    assert any("для market-ордера" in w for w in report.warnings)


def test_chase_with_ioc_is_reported_as_conflict():
    report = OrderConfigValidator.validate(options(OrderExecutionOptions(type=OrderType.CHASE_LIMIT, time_in_force=TimeInForce.IOC)), KNOWN_IDS)
    assert report.is_valid
    assert any("локальное догонание невозможно" in w for w in report.warnings)


def test_chase_budget_larger_than_execution_timeout_is_reported():
    result = BotOptions()
    result.arbitrage.order_execution_timeout_ms = 3_000
    result.arbitrage.execution = OrderExecutionOptions(type=OrderType.CHASE_LIMIT, chase=ChaseOptions(max_steps=20, step_interval_ms=1_000))

    report = OrderConfigValidator.validate(result, KNOWN_IDS)

    assert report.is_valid
    assert any("OrderExecutionTimeoutMs" in w for w in report.warnings)


def test_chase_section_without_chase_limit_type_is_reported():
    report = OrderConfigValidator.validate(options(OrderExecutionOptions(type=OrderType.LIMIT, chase=ChaseOptions(max_steps=3))), KNOWN_IDS)
    assert report.is_valid
    assert any("ChaseLimit" in w for w in report.warnings)
