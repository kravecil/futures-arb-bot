"""Перечисления домена. Значения совпадают с именами в журнале и конфигурации (регистр не важен)."""

from __future__ import annotations

from enum import StrEnum


def _normalize(text: str) -> str:
    return text.replace("_", "").replace("-", "").replace(" ", "").lower()


class _CaseInsensitiveEnum(StrEnum):
    """Разбор из конфигурации без учёта регистра и подчёркиваний: «dry_run» = «DryRun»."""

    @classmethod
    def _missing_(cls, value: object):  # type: ignore[override]
        if isinstance(value, str):
            wanted = _normalize(value)
            for member in cls:
                if _normalize(member.value) == wanted:
                    return member
        return None


class NetworkMode(_CaseInsensitiveEnum):
    """Режим работы робота."""

    DRY_RUN = "DryRun"
    """Симуляция: сделки виртуальные, ордера на биржу не отправляются."""
    TESTNET = "Testnet"
    """Тестовая сеть биржи (testnet): реальные ордера, но условные деньги."""
    LIVE = "Live"
    """Реальная торговля настоящими деньгами. Требует allow_live = true."""


class MarketDataSource(_CaseInsensitiveEnum):
    """Откуда брать котировки."""

    WEBSOCKET = "WebSocket"
    """Потоки бирж (ccxt.pro); при молчании потока биржа временно переходит на REST."""
    REST = "Rest"
    """Опрос REST раз в refresh_interval_ms."""


class OrderSide(_CaseInsensitiveEnum):
    BUY = "Buy"
    SELL = "Sell"


class OrderType(_CaseInsensitiveEnum):
    MARKET = "Market"
    """Рыночный: исполняется сразу по доступной ликвидности."""
    LIMIT = "Limit"
    """Лимитный: исполняется по указанной цене или лучше."""
    CHASE_LIMIT = "ChaseLimit"
    """Лимитный с догонанием цены (chase): локальные перестановки или нативный chase биржи."""


class TimeInForce(_CaseInsensitiveEnum):
    GTC = "Gtc"
    """Живёт в стакане до полного исполнения или отмены."""
    IOC = "Ioc"
    """Неисполненный остаток отменяется сразу после попытки исполнения."""
    FOK = "Fok"
    """Полное исполнение или отмена."""
    POST_ONLY = "PostOnly"
    """Только мейкер (post-only)."""


class ChaseMode(_CaseInsensitiveEnum):
    NONE = "None"
    """Догонания нет: обычная лимитная заявка."""
    SIMULATED = "Simulated"
    """Локальное догонание: исполнитель сам снимает и переставляет заявку."""
    NATIVE = "Native"
    """Нативный chase-ордер биржи."""


class PositionStatus(_CaseInsensitiveEnum):
    OPEN = "Open"
    CLOSED = "Closed"


class CloseReason(_CaseInsensitiveEnum):
    TAKE_PROFIT = "TakeProfit"
    """Спред сжался до порога «вниз» — фиксация прибыли."""
    STOP_LOSS = "StopLoss"
    """Спред вырос до стоп-лосса."""
    TIMEOUT = "Timeout"
    """Позиция достигла максимального возраста."""
    SESSION_END = "SessionEnd"
    """Закрытие при остановке робота (Ctrl+C)."""
    ROLLBACK = "Rollback"
    """Аварийный откат: вторая нога не открылась, первая закрыта."""


class AppLogLevel(_CaseInsensitiveEnum):
    DEBUG = "Debug"
    INFO = "Info"
    SUCCESS = "Success"
    WARNING = "Warning"
    ERROR = "Error"


class OrderStatus(_CaseInsensitiveEnum):
    """Жизненный статус ордера на бирже."""

    OPEN = "Open"
    FILLED = "Filled"
    CANCELED = "Canceled"
    EXPIRED = "Expired"
    REJECTED = "Rejected"
    UNKNOWN = "Unknown"
    """Статус не распознан — трактовать как «ещё жив»."""


class MaxSendEndpoint(_CaseInsensitiveEnum):
    """Способ отправки сообщения через MAX Bot API."""

    MESSAGES = "Messages"
    """Актуальный v2-путь: POST /messages?chat_id=… (рекомендуется)."""
    CHATS_MESSAGE = "ChatsMessage"
    """Запасной путь: POST /chats/{chat_id}/message."""


class NotificationOutcome(_CaseInsensitiveEnum):
    DELIVERED = "Delivered"
    TRANSIENT = "Transient"
    """Временный сбой (сеть, 5xx, 429)."""
    PERMANENT = "Permanent"
    """Постоянная ошибка (неверный токен, нет диалога, запрещённый формат)."""
