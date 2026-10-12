"""Журнал событий: кольцевой буфер в памяти, опциональная запись в logs/session-*.log."""

from __future__ import annotations

import os
import threading
from collections import deque
from typing import IO

from futures_arb_bot.core.abstractions import ConfigProvider
from futures_arb_bot.core.clock import Clock
from futures_arb_bot.core.domain.enums import AppLogLevel
from futures_arb_bot.core.domain.statistics import LogEvent

_LEVEL_ORDER = {level: i for i, level in enumerate(AppLogLevel)}


class InMemoryEventLog:
    """Потокобезопасный журнал событий (вызывается и из цикла asyncio, и из обработчика logging)."""

    CAPACITY = 600
    ERRORS_CAPACITY = 120

    def __init__(self, config: ConfigProvider, clock: Clock) -> None:
        self._config = config
        self._clock = clock
        self._gate = threading.Lock()
        self._events: deque[LogEvent] = deque(maxlen=self.CAPACITY)
        self._errors: deque[LogEvent] = deque(maxlen=self.ERRORS_CAPACITY)
        self._sequence = 0
        self._file: IO[str] | None = None
        self._file_broken = False

    def log(self, level: AppLogLevel, message: str) -> None:
        with self._gate:
            self._sequence += 1
            evt = LogEvent(self._sequence, self._clock.now(), level, message)
            self._events.append(evt)
            if level == AppLogLevel.ERROR:
                self._errors.append(evt)
            self._write_to_file(evt)

    def latest(self, count: int, min_level: AppLogLevel = AppLogLevel.DEBUG) -> list[LogEvent]:
        """Последние события (для дашборда), от старых к новым; min_level отсекает подробности."""
        if count <= 0:
            return []
        threshold = _LEVEL_ORDER[min_level]
        with self._gate:
            picked: list[LogEvent] = []
            for evt in reversed(self._events):
                if _LEVEL_ORDER[evt.level] >= threshold:
                    picked.append(evt)
                    if len(picked) >= count:
                        break
        picked.reverse()
        return picked

    def tail(self, after_id: int, max_count: int) -> list[LogEvent]:
        """События с номером больше after_id (для консольного потока)."""
        with self._gate:
            return [e for e in self._events if e.id > after_id][: max(0, max_count)]

    def errors(self, count: int) -> list[LogEvent]:
        with self._gate:
            items = list(self._errors)
        return items[-count:] if count > 0 else []

    def debug(self, message: str) -> None:
        self.log(AppLogLevel.DEBUG, message)

    def info(self, message: str) -> None:
        self.log(AppLogLevel.INFO, message)

    def success(self, message: str) -> None:
        self.log(AppLogLevel.SUCCESS, message)

    def warning(self, message: str) -> None:
        self.log(AppLogLevel.WARNING, message)

    def error(self, message: str) -> None:
        self.log(AppLogLevel.ERROR, message)

    def close(self) -> None:
        with self._gate:
            if self._file is not None:
                try:
                    self._file.close()
                finally:
                    self._file = None

    def _write_to_file(self, evt: LogEvent) -> None:
        if self._file_broken:
            return
        try:
            if not self._config.current.general.write_log_file:
                return
            if self._file is None:
                os.makedirs("logs", exist_ok=True)
                path = os.path.join("logs", f"session-{self._clock.now():%Y%m%d-%H%M%S}.log")
                self._file = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 — файл живёт весь сеанс
                self._file.write(f"# FuturesArbBot session log, открыты файлы конфигурации: {self._config.config_directory}\n")
            stamp = evt.time.strftime("%Y-%m-%d %H:%M:%S.") + f"{evt.time.microsecond // 1000:03d}"
            self._file.write(f"{stamp}\t{evt.level}\t{evt.message}\n")
            self._file.flush()
        except Exception:
            # файл недоступен — молча отключаем запись в файл, консоль продолжает работать
            self._file_broken = True
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
            self._file = None
