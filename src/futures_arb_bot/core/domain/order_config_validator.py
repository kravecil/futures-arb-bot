"""
Проверка раздела execution до подключения бирж: явные ошибки блокируют запуск, всё, что
политика умеет безопасно понизить, уходит в предупреждения.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal

from futures_arb_bot.core.domain.enums import ChaseMode, OrderType, TimeInForce
from futures_arb_bot.core.domain.options import BotOptions, ChaseOptions, OrderExecutionOptions
from futures_arb_bot.core.domain.order_execution import ExchangeCapabilityMap, OrderPolicyResolver
from futures_arb_bot.core.numbers import dec_str


@dataclass(frozen=True, slots=True)
class OrderConfigValidation:
    errors: list[str]
    warnings: list[str]

    @property
    def is_valid(self) -> bool:
        return not self.errors


class OrderConfigValidator:
    @staticmethod
    def validate(options: BotOptions, known_exchange_ids: Collection[str] | None = None) -> OrderConfigValidation:
        errors: list[str] = []
        warnings: list[str] = []
        arbitrage = options.arbitrage
        capability_ids = {i.lower() for i in ExchangeCapabilityMap.known_ids()}
        known = {i.lower() for i in known_exchange_ids} if known_exchange_ids else set()

        _check_section("Arbitrage.Execution", arbitrage.execution, arbitrage.order_execution_timeout_ms, errors, warnings)

        for entry in options.exchanges.items:
            if entry.execution is None:
                continue

            if known and entry.id.lower() not in known:
                errors.append(f"exchanges.yaml: биржа «{entry.id}» не встроена в фабрику — раздел Execution у неё не имеет смысла.")

            if entry.id.lower() not in capability_ids:
                warnings.append(
                    f"[{entry.id}] возможности биржи не описаны в карте — берутся базовые (limit/cancel/fetch), "
                    "post-only, IOC/FOK и нативный chase считаются недоступными."
                )

            _check_section(f"exchanges.yaml: {entry.id}.Execution", entry.execution, arbitrage.order_execution_timeout_ms, errors, warnings)

        return OrderConfigValidation(errors, warnings)


def _check_section(path: str, section: OrderExecutionOptions | None, timeout_ms: int, errors: list[str], warnings: list[str]) -> None:
    if section is None:
        return

    if section.close is not None and section.close.close is not None:
        errors.append(f"{path}: вложенная секция Close:Close не поддерживается — у Close может быть только своя политика.")

    _in_range(f"{path}.LimitOffsetBps", section.limit_offset_bps, Decimal(-1000), Decimal(1000), errors)

    chase_mode = section.chase.mode if section.chase is not None else None
    if (
        section.type == OrderType.CHASE_LIMIT
        and chase_mode in (None, ChaseMode.SIMULATED)
        and section.time_in_force in (TimeInForce.IOC, TimeInForce.FOK)
    ):
        warnings.append(
            f"{path}: с TimeInForce = {section.time_in_force} локальное догонание невозможно "
            "(остаток заявки снимается биржей сразу) — Type будет понижен до Limit."
        )

    if section.type == OrderType.MARKET and section.time_in_force is not None:
        warnings.append(f"{path}: TimeInForce = {section.time_in_force} игнорируется для market-ордера.")

    if section.close is not None:
        close = section.close
        if close.type == OrderType.CHASE_LIMIT:
            warnings.append(f"{path}.Close.Type: догонание цены на закрытии не допускается — закрытие будет market.")
        if close.chase is not None:
            warnings.append(f"{path}.Close.Chase: раздел Chase на закрытии игнорируется.")
        _check_section(f"{path}.Close", close, timeout_ms, errors, warnings)

    if section.chase is not None:
        _check_chase(f"{path}.Chase", section.chase, section.type, timeout_ms, errors, warnings)


def _check_chase(path: str, chase: ChaseOptions, owner_type: OrderType | None, timeout_ms: int, errors: list[str], warnings: list[str]) -> None:
    _in_range(f"{path}.MaxSteps", chase.max_steps, 1, 100, errors)
    _in_range(f"{path}.StepIntervalMs", chase.step_interval_ms, 50, 60_000, errors)
    _in_range(f"{path}.StepBps", chase.step_bps, Decimal(0), Decimal(1000), errors)
    _in_range(f"{path}.MaxDeviationBps", chase.max_deviation_bps, Decimal(0), Decimal(10_000), errors)

    if owner_type in (None, OrderType.LIMIT):
        shown = owner_type or OrderType.LIMIT
        warnings.append(f"{path}: раздел Chase задан, но Execution.Type = {shown} — догонание не включится (нужен Type = ChaseLimit).")

    if chase.max_deviation_bps is not None and chase.step_bps is not None and chase.max_deviation_bps < chase.step_bps:
        warnings.append(
            f"{path}: MaxDeviationBps ({dec_str(chase.max_deviation_bps)}) меньше шага StepBps ({dec_str(chase.step_bps)}) — "
            "догонание выродится в одну перестановку."
        )

    if chase.params is not None:
        for key, value in chase.params.items():
            if not key.strip() or not value.strip():
                errors.append(f"{path}.Params: ключ и значение не должны быть пустыми (найдено «{key}»:«{value}»).")

    if chase.mode == ChaseMode.NATIVE and not chase.params:
        errors.append(f"{path}: Mode = Native требует непустого Params — без нативных параметров chase-ордер неотличим от лимитного.")

    steps = chase.max_steps if chase.max_steps is not None else OrderPolicyResolver.DEFAULT_MAX_STEPS
    interval = chase.step_interval_ms if chase.step_interval_ms is not None else OrderPolicyResolver.DEFAULT_STEP_INTERVAL_MS
    if chase.mode in (None, ChaseMode.SIMULATED) and steps * interval > timeout_ms:
        warnings.append(
            f"{path}: бюджет догонания {steps} × {interval} мс превышает OrderExecutionTimeoutMs ({timeout_ms} мс) — "
            "заявка не успеет пройти все шаги. Увеличьте OrderExecutionTimeoutMs или уменьшите MaxSteps/StepIntervalMs."
        )

    if chase.mode == ChaseMode.NATIVE:
        warnings.append(
            f"{path}: Mode = Native требует проверки на testnet — убедитесь, что Params соответствуют API биржи, "
            "иначе политика сама понизит режим до Simulated."
        )


def _in_range(path: str, value: Decimal | int | None, low: Decimal | int, high: Decimal | int, errors: list[str]) -> None:
    if value is not None and (value < low or value > high):
        errors.append(f"{path}: значение {dec_str(value)} вне допустимых границ [{dec_str(low)}…{dec_str(high)}].")
