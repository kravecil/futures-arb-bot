"""
Проводка бирж: фабрика CCXT и карта возможностей должны обновляться вместе, иначе новая биржа
либо отсекается на старте («не встроена в фабрику»), либо торгуется без post-only/IOC.
"""

from futures_arb_bot.core.clock import SystemClock
from futures_arb_bot.core.domain.enums import NetworkMode, OrderType, TimeInForce
from futures_arb_bot.core.domain.options import BotOptions, ExchangeConfigEntry, OrderExecutionOptions
from futures_arb_bot.core.domain.order_config_validator import OrderConfigValidator
from futures_arb_bot.core.domain.order_execution import ExchangeCapabilityMap
from futures_arb_bot.infrastructure.ccxt_factory import CcxtExchangeFactory, instantiate

from .fakes import make_log


def factory() -> CcxtExchangeFactory:
    return CcxtExchangeFactory(make_log(), SystemClock())


def test_every_factory_exchange_has_explicit_capabilities():
    known = {i.lower() for i in ExchangeCapabilityMap.known_ids()}
    assert [i for i in factory().supported_ids if i.lower() not in known] == []


def test_every_capability_entry_is_instantiable_by_factory():
    for exchange_id in ExchangeCapabilityMap.known_ids():
        assert instantiate(exchange_id, {}) is not None


def test_bingx_is_built_into_the_factory():
    assert "bingx" in factory().supported_ids
    connector = factory().create(ExchangeConfigEntry(id="bingx"), NetworkMode.DRY_RUN)
    assert connector.id == "bingx"


def test_bingx_supports_post_only_and_ioc_but_neither_fok_nor_native_chase():
    caps = ExchangeCapabilityMap.for_id("bingx")

    assert caps.id == "bingx"
    assert caps.supports_limit
    assert caps.supports_cancel
    assert caps.supports_fetch_order
    assert caps.post_only_token == "PostOnly"
    assert caps.supports_ioc
    assert not caps.supports_fok
    assert not caps.supports_native_chase
    assert caps.supports_simulated_chase


def test_bingx_execution_section_passes_validator():
    options = BotOptions()
    options.exchanges.items.append(
        ExchangeConfigEntry(id="bingx", execution=OrderExecutionOptions(type=OrderType.CHASE_LIMIT, time_in_force=TimeInForce.POST_ONLY))
    )

    report = OrderConfigValidator.validate(options, factory().supported_ids)

    assert report.is_valid
    assert not any("встроена" in error for error in report.errors)
