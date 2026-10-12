"""
Мост из стандартного logging в журнал событий: записи уровня INFO и выше попадают
и в дашборд, и в файл сеанса с префиксом «[Категория]».
"""

from __future__ import annotations

import logging

from futures_arb_bot.core.abstractions import EventLog
from futures_arb_bot.core.domain.enums import AppLogLevel

ROOT_LOGGER = "futures_arb_bot"

# Без подключённого моста записи никуда не выводятся (в том числе lastResort в stderr).
logging.getLogger(ROOT_LOGGER).addHandler(logging.NullHandler())
logging.getLogger(ROOT_LOGGER).propagate = False


def get_logger(category: str) -> logging.Logger:
    """Логгер категории (обычно — имя класса)."""
    return logging.getLogger(f"{ROOT_LOGGER}.{category}")


class EventLogHandler(logging.Handler):
    def __init__(self, sink: EventLog) -> None:
        super().__init__(level=logging.INFO)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                ex = record.exc_info[1]
                message += f" :: {type(ex).__name__}: {ex}"

            if record.levelno >= logging.ERROR:
                level = AppLogLevel.ERROR
            elif record.levelno >= logging.WARNING:
                level = AppLogLevel.WARNING
            else:
                level = AppLogLevel.INFO

            category = record.name.rsplit(".", 1)[-1]
            self._sink.log(level, f"[{category}] {message}")
        except Exception:
            self.handleError(record)


def attach(sink: EventLog) -> EventLogHandler:
    """Направить все логгеры приложения в журнал событий."""
    root = logging.getLogger(ROOT_LOGGER)
    handler = EventLogHandler(sink)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return handler
