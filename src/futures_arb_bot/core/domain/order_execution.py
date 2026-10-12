"""
Политика исполнения заявок: возможности бирж, развёртывание настроек execution в итоговую
политику с безопасным понижением неподдерживаемого и перевод заявки в params CCXT.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, ClassVar

from futures_arb_bot.core.domain.enums import ChaseMode, OrderType, TimeInForce
from futures_arb_bot.core.domain.market_data import OrderRequest
from futures_arb_bot.core.domain.options import OrderExecutionOptions
from futures_arb_bot.core.numbers import ZERO


@dataclass(frozen=True, slots=True)
class ExchangeCapabilities:
    """Что биржа умеет по части заявок (см. EXCHANGE_CAPABILITIES)."""

    id: str
    supports_limit: bool
    supports_cancel: bool
    supports_fetch_order: bool
    post_only_token: str | None
    """Значение timeInForce для post-only (None — биржа его не понимает)."""
    supports_ioc: bool
    supports_fok: bool
    supports_native_chase: bool
    native_chase_defaults: Mapping[str, str] | None = None
    """Параметры нативного chase по умолчанию (None — требуется явная настройка в конфиге)."""

    @property
    def supports_post_only(self) -> bool:
        return self.post_only_token is not None

    @property
    def supports_simulated_chase(self) -> bool:
        """Локальное догонание требует лимитных заявок, отмены и опроса состояния."""
        return self.supports_limit and self.supports_cancel and self.supports_fetch_order


def _caps(id_: str, post_only: str | None, ioc: bool, fok: bool, native_chase: bool = False) -> ExchangeCapabilities:
    return ExchangeCapabilities(id_, True, True, True, post_only, ioc, fok, native_chase)


class ExchangeCapabilityMap:
    """
    Карта возможностей бирж — совпадает со списком фабрики коннекторов. Всё, что не подтверждено,
    помечено как неподдерживаемое, чтобы политика не отправила параметр, который биржа отвергнет.
    Таблицу надо сверять при апгрейде CCXT.
    """

    _MAP: ClassVar[dict[str, ExchangeCapabilities]] = {
        # Binance USDM: timeInForce = GTC|IOC|FOK, post-only в фьючерсном API нет
        "binanceusdm": _caps("binanceusdm", None, True, True),
        "binance": _caps("binance", None, True, True),
        "bybit": _caps("bybit", "PostOnly", True, True),
        "okx": _caps("okx", "PO", True, True),
        "gate": _caps("gate", "post_only", True, False),
        # MEXC Futures ждёт числовые коды timeInForce — строковые токены не отправляем
        "mexc": _caps("mexc", None, False, False),
        # KuCoin Futures: timeInForce = GTC|IOC; chase есть, но Native требует своих params и проверки на testnet
        "kucoinfutures": _caps("kucoinfutures", None, True, False, native_chase=True),
        "bitget": _caps("bitget", "post_only", True, True),
        "htx": _caps("htx", None, True, True),
        "coinex": _caps("coinex", None, False, False),
        "phemex": _caps("phemex", None, False, False),
        "krakenfutures": _caps("krakenfutures", None, False, False),
        # BingX perpetual: timeInForce = GTC|IOC|PostOnly (FOK нет); chase-ордеров нет — догоняет исполнитель
        "bingx": _caps("bingx", "PostOnly", True, False),
    }

    @classmethod
    def for_id(cls, exchange_id: str) -> ExchangeCapabilities:
        """Возможности биржи по идентификатору (неизвестная биржа — базовый набор)."""
        caps = cls._MAP.get(exchange_id.lower())
        if caps is not None:
            return caps
        return ExchangeCapabilities(exchange_id, True, True, True, None, False, False, False)

    @classmethod
    def known_ids(cls) -> list[str]:
        return list(cls._MAP)


@dataclass(frozen=True, slots=True)
class ChasePlan:
    """Развёрнутый план догонания цены."""

    mode: ChaseMode
    max_steps: int
    step_interval_ms: int
    step_bps: Decimal
    max_deviation_bps: Decimal
    fallback_to_market: bool


@dataclass(frozen=True, slots=True)
class OrderPolicy:
    """Итоговая политика исполнения заявки на конкретной бирже."""

    type: OrderType
    time_in_force: TimeInForce
    limit_offset_bps: Decimal
    chase: ChasePlan | None
    exchange_params: Mapping[str, str] | None
    notes: list[str] = field(default_factory=list)

    @property
    def chases_locally(self) -> bool:
        return self.chase is not None and self.chase.mode == ChaseMode.SIMULATED

    @property
    def chases_natively(self) -> bool:
        return self.chase is not None and self.chase.mode == ChaseMode.NATIVE

    @property
    def requires_price(self) -> bool:
        return self.type in (OrderType.LIMIT, OrderType.CHASE_LIMIT)

    def with_(self, **changes: Any) -> OrderPolicy:
        return replace(self, **changes)


MARKETABLE_LIMIT = OrderPolicy(OrderType.LIMIT, TimeInForce.GTC, ZERO, None, None, [])
"""Marketable limit — историческое поведение входа по умолчанию."""

MARKET_ORDER = OrderPolicy(OrderType.MARKET, TimeInForce.GTC, ZERO, None, None, [])
"""Рыночная заявка — поведение закрытия по умолчанию."""


@dataclass(frozen=True, slots=True)
class _Effective:
    type: OrderType | None
    limit_offset_bps: Decimal | None
    time_in_force: TimeInForce | None
    mode: ChaseMode | None
    max_steps: int | None
    step_interval_ms: int | None
    step_bps: Decimal | None
    max_deviation_bps: Decimal | None
    fallback_to_market: bool | None
    params: Mapping[str, str] | None


class OrderPolicyResolver:
    """Глобальный раздел → переопределение биржи → возможности биржи (безопасное понижение)."""

    DEFAULT_MAX_STEPS = 5
    DEFAULT_STEP_INTERVAL_MS = 400
    DEFAULT_STEP_BPS = Decimal(5)
    DEFAULT_MAX_DEVIATION_BPS = Decimal(50)
    DEFAULT_FALLBACK_TO_MARKET = True

    @classmethod
    def resolve_entry(
        cls, global_: OrderExecutionOptions | None, per_exchange: OrderExecutionOptions | None, caps: ExchangeCapabilities
    ) -> OrderPolicy:
        """Политика входной заявки ноги (по умолчанию — marketable limit)."""
        return cls._resolve(global_, per_exchange, caps, is_close=False)

    @classmethod
    def resolve_close(
        cls, global_: OrderExecutionOptions | None, per_exchange: OrderExecutionOptions | None, caps: ExchangeCapabilities
    ) -> OrderPolicy:
        """Политика закрытия ноги: только из секций close; по умолчанию — market."""
        return cls._resolve(
            global_.close if global_ is not None else None,
            per_exchange.close if per_exchange is not None else None,
            caps,
            is_close=True,
        )

    @classmethod
    def _resolve(
        cls,
        base: OrderExecutionOptions | None,
        over: OrderExecutionOptions | None,
        caps: ExchangeCapabilities,
        is_close: bool,
    ) -> OrderPolicy:
        eff = cls._merge(base, over)
        notes: list[str] = []
        type_ = eff.type if eff.type is not None else (OrderType.MARKET if is_close else OrderType.LIMIT)
        offset_bps = eff.limit_offset_bps if eff.limit_offset_bps is not None else ZERO

        if type_ in (OrderType.LIMIT, OrderType.CHASE_LIMIT) and not caps.supports_limit:
            notes.append(f"биржа {caps.id} не принимает лимитные заявки — исполнение market")
            type_ = OrderType.MARKET

        # для market условия действия не имеют смысла — не тащим их в params биржи
        if type_ == OrderType.MARKET:
            time_in_force = TimeInForce.GTC
        else:
            time_in_force = cls._degrade_tif(eff.time_in_force or TimeInForce.GTC, caps, notes)

        chase: ChasePlan | None = None
        exchange_params: Mapping[str, str] | None = None

        if type_ == OrderType.CHASE_LIMIT:
            if is_close:
                notes.append("догонание цены на закрытии позиции не допускается — закрытие market")
                type_ = OrderType.MARKET
            else:
                mode = cls._resolve_chase_mode(eff, caps, notes)
                if mode == ChaseMode.NONE:
                    type_ = OrderType.LIMIT
                else:
                    step = eff.step_bps if eff.step_bps is not None else cls.DEFAULT_STEP_BPS
                    max_deviation = eff.max_deviation_bps if eff.max_deviation_bps is not None else cls.DEFAULT_MAX_DEVIATION_BPS
                    if max_deviation < step:
                        max_deviation = step  # бюджет меньше одного шага — одна перестановка

                    chase = ChasePlan(
                        mode,
                        eff.max_steps if eff.max_steps is not None else cls.DEFAULT_MAX_STEPS,
                        eff.step_interval_ms if eff.step_interval_ms is not None else cls.DEFAULT_STEP_INTERVAL_MS,
                        step,
                        max_deviation,
                        eff.fallback_to_market if eff.fallback_to_market is not None else cls.DEFAULT_FALLBACK_TO_MARKET,
                    )

                    if mode == ChaseMode.NATIVE:
                        exchange_params = eff.params if eff.params is not None else caps.native_chase_defaults
                        keys = ", ".join(exchange_params.keys()) if exchange_params else ""
                        notes.append(
                            f"нативный chase на {caps.id}: заявка уйдёт как limit с параметрами [{keys}] — режим требует проверки на testnet"
                        )

        if type_ == OrderType.MARKET:
            offset_bps = ZERO  # смещение цены к market не применяется

        return OrderPolicy(type_, time_in_force, offset_bps, chase, exchange_params, notes)

    @staticmethod
    def _resolve_chase_mode(eff: _Effective, caps: ExchangeCapabilities, notes: list[str]) -> ChaseMode:
        mode = eff.mode if eff.mode is not None else ChaseMode.SIMULATED

        if mode == ChaseMode.NONE:
            notes.append("режим догонания отключён (Chase:Mode = None) — обычная лимитная заявка")
            return ChaseMode.NONE

        if mode == ChaseMode.SIMULATED and eff.time_in_force in (TimeInForce.IOC, TimeInForce.FOK):
            notes.append(f"догонание невозможно с {eff.time_in_force} (остаток снимается сразу) — обычная лимитная заявка")
            return ChaseMode.NONE

        if mode == ChaseMode.NATIVE and not caps.supports_native_chase:
            notes.append(f"нативный chase на {caps.id} недоступен — используется локальное догонание (Simulated)")
            mode = ChaseMode.SIMULATED

        if mode == ChaseMode.NATIVE and not eff.params and caps.native_chase_defaults is None:
            notes.append("параметры нативного chase (Execution:Chase:Params) не заданы — используется локальное догонание (Simulated)")
            mode = ChaseMode.SIMULATED

        if mode == ChaseMode.SIMULATED and not caps.supports_simulated_chase:
            notes.append(f"локальное догонание на {caps.id} недоступно (нет отмены/опроса заявок) — обычная лимитная заявка")
            return ChaseMode.NONE

        return mode

    @staticmethod
    def _degrade_tif(requested: TimeInForce, caps: ExchangeCapabilities, notes: list[str]) -> TimeInForce:
        def downgrade(name: str) -> TimeInForce:
            notes.append(f"{name} на {caps.id} не поддерживается — заявка с GTC")
            return TimeInForce.GTC

        if requested == TimeInForce.POST_ONLY and not caps.supports_post_only:
            return downgrade("Post-only")
        if requested == TimeInForce.IOC and not caps.supports_ioc:
            return downgrade("IOC")
        if requested == TimeInForce.FOK and not caps.supports_fok:
            return downgrade("FOK")
        return requested

    @staticmethod
    def _merge(base: OrderExecutionOptions | None, over: OrderExecutionOptions | None) -> _Effective:
        def pick(attr: str) -> Any:
            value = getattr(over, attr, None) if over is not None else None
            if value is None and base is not None:
                value = getattr(base, attr, None)
            return value

        chase_base = base.chase if base is not None else None
        chase_over = over.chase if over is not None else None

        def pick_chase(attr: str) -> Any:
            value = getattr(chase_over, attr, None) if chase_over is not None else None
            if value is None and chase_base is not None:
                value = getattr(chase_base, attr, None)
            return value

        over_params = chase_over.params if chase_over is not None else None
        params = over_params if over_params else (chase_base.params if chase_base is not None else None)

        return _Effective(
            pick("type"),
            pick("limit_offset_bps"),
            pick("time_in_force"),
            pick_chase("mode"),
            pick_chase("max_steps"),
            pick_chase("step_interval_ms"),
            pick_chase("step_bps"),
            pick_chase("max_deviation_bps"),
            pick_chase("fallback_to_market"),
            params,
        )


class OrderParamsBuilder:
    """Перевод OrderRequest в params-словарь CCXT (проверяется без реальных бирж)."""

    @staticmethod
    def build(request: OrderRequest, caps: ExchangeCapabilities) -> dict[str, Any]:
        parameters: dict[str, Any] = {}

        if request.reduce_only:
            parameters["reduceOnly"] = True

        if request.type in (OrderType.LIMIT, OrderType.CHASE_LIMIT) and request.time_in_force != TimeInForce.GTC:
            if request.time_in_force == TimeInForce.POST_ONLY:
                # понижение до GTC уже сделала политика; если пришли напрямую — страховка
                if caps.post_only_token is not None:
                    parameters["timeInForce"] = caps.post_only_token
                else:
                    parameters["postOnly"] = True
            else:
                parameters["timeInForce"] = request.time_in_force.value.upper()

        if request.exchange_params:
            # сырые параметры биржи перекрывают остальное: это осознанный ручной выбор
            parameters.update(request.exchange_params)

        return parameters
