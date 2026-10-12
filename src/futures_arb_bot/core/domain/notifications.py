"""Результат доставки уведомления."""

from __future__ import annotations

from dataclasses import dataclass

from futures_arb_bot.core.domain.enums import NotificationOutcome


@dataclass(frozen=True, slots=True)
class NotificationDelivery:
    """Исход плюс человекочитаемое описание ошибки для журнала."""

    outcome: NotificationOutcome
    error: str | None = None

    @staticmethod
    def delivered() -> NotificationDelivery:
        return _DELIVERED

    @staticmethod
    def transient(error: str) -> NotificationDelivery:
        return NotificationDelivery(NotificationOutcome.TRANSIENT, error)

    @staticmethod
    def permanent(error: str) -> NotificationDelivery:
        return NotificationDelivery(NotificationOutcome.PERMANENT, error)

    @property
    def is_delivered(self) -> bool:
        return self.outcome == NotificationOutcome.DELIVERED


_DELIVERED = NotificationDelivery(NotificationOutcome.DELIVERED)
