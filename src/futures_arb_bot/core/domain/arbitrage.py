"""Арбитражная возможность и пара встречных позиций."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from futures_arb_bot.core.domain.enums import CloseReason, PositionStatus
from futures_arb_bot.core.numbers import ZERO


@dataclass(frozen=True, slots=True)
class OpportunityLeg:
    """Одна «нога» арбитражной позиции. funding_percent=None — биржа не отдала фандинг."""

    exchange_id: str
    price: Decimal
    fee_percent: Decimal
    funding_percent: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SpreadEstimate:
    """Оценка возможности: купить дешевле на одной бирже и продать дороже на другой."""

    symbol: str
    long_leg: OpportunityLeg
    short_leg: OpportunityLeg
    gross_percent: Decimal
    net_percent: Decimal
    quote_volume_usd: Decimal
    detected_at: datetime


@dataclass(eq=False, slots=True)
class PositionPair:
    """Пара встречных фьючерсных позиций (лонг + шорт), дельта-нейтральная связка."""

    symbol: str
    long_exchange_id: str
    short_exchange_id: str
    long_size: Decimal
    """Фактически набранный объём лонга в базовой валюте."""
    short_size: Decimal
    entry_long: Decimal
    entry_short: Decimal
    opened_at: datetime
    simulated: bool = False
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    closed_long_volume: Decimal = ZERO
    closed_short_volume: Decimal = ZERO
    fees_entry_usd: Decimal = ZERO
    status: PositionStatus = PositionStatus.OPEN
    long_closed: bool = False
    """Лонговая нога закрыта (промежуточное состояние многошагового закрытия)."""
    short_closed: bool = False
    exit_long: Decimal | None = None
    exit_short: Decimal | None = None
    fees_exit_usd: Decimal = ZERO
    closed_at: datetime | None = None
    reason: CloseReason | None = None
    realized_pnl_usd: Decimal | None = None

    @property
    def matched_size(self) -> Decimal:
        """Согласованный (встречный) объём позиции — минимум из двух ног."""
        return min(self.long_size, self.short_size)

    @property
    def imbalance(self) -> Decimal:
        """Перекос ног: положителен — больше лонг, отрицателен — больше шорт."""
        return self.long_size - self.short_size

    @property
    def closed_matched_volume(self) -> Decimal:
        """Закрытый встречный объём позиции — для отчётов после обнуления ног."""
        return min(self.closed_long_volume, self.closed_short_volume)
