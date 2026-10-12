"""Рыночные данные и заявки."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from futures_arb_bot.core.domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from futures_arb_bot.core.numbers import ZERO


@dataclass(frozen=True, slots=True)
class ExchangeFees:
    """Комиссии биржи в процентах от оборота."""

    taker_percent: Decimal
    maker_percent: Decimal


@dataclass(frozen=True, slots=True)
class MarketInfo:
    """Описание торгового инструмента (линейный бессрочный фьючерс)."""

    symbol: str
    is_perpetual: bool
    is_linear: bool
    quote_currency: str
    min_amount: Decimal | None
    taker_percent: Decimal
    maker_percent: Decimal


@dataclass(frozen=True, slots=True)
class TickerSnapshot:
    """Снимок тикера конкретной биржи на момент опроса. timestamp=None — биржа не дала время."""

    exchange_id: str
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    quote_volume_24h: Decimal
    timestamp: datetime | None

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def is_tradable(self) -> bool:
        return self.bid > ZERO and self.ask > ZERO


@dataclass(frozen=True, slots=True)
class OrderUpdate:
    """Снимок состояния ордера для опроса исполнения лимитных заявок."""

    order_id: str
    status: OrderStatus
    filled_amount: Decimal
    average_price: Decimal | None
    fee_quote: Decimal | None = None
    """Накопленная комиссия по заявке в валюте расчётов (USDT); None — биржа не сообщила."""

    @property
    def is_dead(self) -> bool:
        return self.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED)

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Запрос на ордер. Для лимитных типов (Limit, ChaseLimit) обязана быть задана цена."""

    symbol: str
    side: OrderSide
    amount: Decimal
    reduce_only: bool = False
    type: OrderType = OrderType.MARKET
    price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    exchange_params: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Результат исполнения ордера."""

    success: bool
    order_id: str | None = None
    average_price: Decimal | None = None
    filled_amount: Decimal = ZERO
    error: str | None = None
    fee_quote: Decimal | None = None
    """Комиссия за уже исполненную часть в валюте расчётов (USDT); None — биржа не сообщила."""

    @staticmethod
    def ok(order_id: str | None, average_price: Decimal | None, filled: Decimal, fee_quote: Decimal | None = None) -> OrderResult:
        return OrderResult(True, order_id, average_price, filled, fee_quote=fee_quote)

    @staticmethod
    def fail(error: str) -> OrderResult:
        return OrderResult(False, error=error)


@dataclass(frozen=True, slots=True)
class FetchTickersResult:
    """Результат опроса тикеров биржи."""

    tickers: Mapping[str, TickerSnapshot]
    latency: timedelta = field(default_factory=timedelta)


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """Фактическая открытая позиция на бирже: side — сторона экспозиции (Buy — лонг, Sell — шорт)."""

    symbol: str
    side: OrderSide
    amount: Decimal
    entry_price: Decimal | None
