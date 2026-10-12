"""
Текстовый режим (--no-ui): печатает новые события журнала в консоль без Live-рендеринга
(удобно для отладки и терминалов без ANSI).
"""

from __future__ import annotations

from futures_arb_bot.app.console import console
from futures_arb_bot.app.ui.dashboard import log_line
from futures_arb_bot.core.cancellation import CancellationToken, OperationCanceledError, delay
from futures_arb_bot.core.domain.enums import AppLogLevel
from futures_arb_bot.core.engine.event_log import InMemoryEventLog


class SimpleConsoleFeed:
    def __init__(self, log: InMemoryEventLog) -> None:
        self._log = log

    async def run(self, ct: CancellationToken) -> None:
        after = 0
        while not ct.is_cancellation_requested:
            for evt in self._log.tail(after, 50):
                after = evt.id
                if evt.level == AppLogLevel.DEBUG:
                    continue
                console.print(log_line(f"{evt.time.astimezone():%H:%M:%S}", evt.level, evt.message))

            try:
                await delay(0.25, ct)
            except OperationCanceledError:
                break
