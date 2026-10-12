"""
Неблокирующий отправитель уведомлений об арбитражных сигналах.

``notify`` лишь кладёт готовый текст в ограниченную очередь и немедленно возвращается: цикл
сканирования никогда не ждёт сеть. Фоновая задача выгребает очередь по одному сообщению,
а транспорт сам выполняет повторы. Защита: порог спреда, кулдаун на символ, лимит в минуту,
а после серии сбоев канал «закрывается» на circuit_break_minutes (при постоянной ошибке — сразу).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from datetime import datetime, timedelta

from futures_arb_bot.core.abstractions import ConfigProvider, EventLog, NotificationTransport
from futures_arb_bot.core.cancellation import CancellationToken, OperationCanceledError
from futures_arb_bot.core.clock import Clock
from futures_arb_bot.core.domain.arbitrage import SpreadEstimate
from futures_arb_bot.core.domain.enums import NotificationOutcome
from futures_arb_bot.core.engine.logging_bridge import get_logger
from futures_arb_bot.core.engine.notification_formatter import format_notification
from futures_arb_bot.core.errors import message_of

_COOLDOWN_CACHE_LIMIT = 512


class QueuedSpreadNotifier:
    def __init__(self, config: ConfigProvider, transport: NotificationTransport, log: EventLog, clock: Clock) -> None:
        self._config = config
        self._transport = transport
        self._log = log
        self._clock = clock
        self._logger = get_logger("SpreadNotifier")
        self._last_sent_by_symbol: dict[str, datetime] = {}
        self._recent_sends: deque[datetime] = deque()
        self._ct = CancellationToken()
        capacity = min(max(config.current.notifications.queue_capacity, 8), 1024)
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=capacity)
        self._pump: asyncio.Task[None] | None = None
        self._consecutive_failures = 0
        self._circuit_until: datetime | None = None
        self._suppressed = 0

    @property
    def pending_count(self) -> int:
        """Число сообщений, ожидающих отправки (для диагностики и тестов)."""
        return self._queue.qsize()

    @property
    def suppressed_count(self) -> int:
        """Число сигналов, отброшенных фильтрами или переполненной очередью."""
        return self._suppressed

    def notify(self, estimate: SpreadEstimate) -> None:
        options = self._config.current
        notifications = options.notifications
        if not notifications.is_usable:
            return

        threshold = notifications.min_spread_percent if notifications.min_spread_percent is not None else options.arbitrage.min_spread_percent_up
        if estimate.net_percent < threshold:
            return

        now = self._clock.now()
        if self._circuit_until is not None and now < self._circuit_until:
            self._suppress()
            return

        if self._in_cooldown(estimate.symbol, now, notifications.signal_cooldown_seconds):
            self._suppress()
            return

        if self._rate_limited(now, notifications.max_per_minute):
            self._suppress()
            self._log.debug(f"Уведомления: превышен лимит {notifications.max_per_minute}/мин — сигнал {estimate.symbol} пропущен")
            return

        self._last_sent_by_symbol[estimate.symbol] = now
        self._recent_sends.append(now)
        text = format_notification(estimate, notifications)

        self._start_pump()
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull, asyncio.QueueShutDown:
            self._suppress()
            self._log.warning(f"Очередь уведомлений переполнена — сигнал {estimate.symbol} пропущен")

    async def aclose(self) -> None:
        """Остановить отправитель: очередь закрывается и недолго досылается, процесс не зависает."""
        self._queue.shutdown()
        if self._pump is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self._pump), timeout=2)

        if self._queue.qsize() > 0:
            self._log.debug(f">{self._queue.qsize()} уведомлений не отправлены: сеанс закрыт раньше, чем опустела очередь")

        self._ct.cancel()
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
            with contextlib.suppress(BaseException):
                await self._pump

    async def wait_idle(self, timeout: float = 5.0) -> None:
        """Дождаться, пока очередь опустеет и текущая отправка завершится (для тестов)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._queue.empty() and self._queue._unfinished_tasks == 0:  # type: ignore[attr-defined]
                return
            await asyncio.sleep(0.01)

    def _start_pump(self) -> None:
        """Фоновая задача запускается ровно один раз — при первой постановке в очередь."""
        if self._pump is None:
            self._pump = asyncio.get_running_loop().create_task(self._run_pump())

    async def _run_pump(self) -> None:
        ct = self._ct
        try:
            while True:
                try:
                    text = await self._queue.get()
                except asyncio.QueueShutDown:
                    break
                try:
                    await self._deliver(text, ct)
                finally:
                    self._queue.task_done()
        except OperationCanceledError:
            pass  # штатная остановка
        except Exception as ex:
            self._log.error(f"Отправитель уведомлений остановлен: {message_of(ex)}")
            self._logger.warning("notifier pump fault", exc_info=ex)

    async def _deliver(self, text: str, ct: CancellationToken) -> None:
        notifications = self._config.current.notifications
        if self._is_circuit_open(self._clock.now()):
            self._log.debug("Уведомления приостановлены после серии сбоев — сообщение отброшено")
            return

        result = await self._transport.send(text, ct)
        if result.is_delivered:
            self._consecutive_failures = 0
            return

        self._consecutive_failures += 1
        failures = self._consecutive_failures
        self._log.error(f"Уведомление не доставлено ({result.outcome}): {result.error}")
        if result.outcome == NotificationOutcome.PERMANENT or failures >= notifications.failure_circuit_limit:
            self._open_circuit(self._clock.now(), failures, notifications.circuit_break_minutes, result.outcome)

    def _in_cooldown(self, symbol: str, now: datetime, cooldown_seconds: int) -> bool:
        if cooldown_seconds <= 0:
            return False

        last = self._last_sent_by_symbol.get(symbol)
        if last is not None and now - last < timedelta(seconds=cooldown_seconds):
            return True

        if len(self._last_sent_by_symbol) > _COOLDOWN_CACHE_LIMIT:
            cutoff = now - timedelta(seconds=cooldown_seconds)
            for stale in [s for s, t in self._last_sent_by_symbol.items() if t < cutoff]:
                del self._last_sent_by_symbol[stale]

        return False

    def _rate_limited(self, now: datetime, max_per_minute: int) -> bool:
        window = timedelta(minutes=1)
        while self._recent_sends and now - self._recent_sends[0] >= window:
            self._recent_sends.popleft()
        return len(self._recent_sends) >= max_per_minute

    def _is_circuit_open(self, now: datetime) -> bool:
        return self._circuit_until is not None and now < self._circuit_until

    def _open_circuit(self, now: datetime, failures: int, break_minutes: int, outcome: NotificationOutcome) -> None:
        if self._circuit_until is not None and now < self._circuit_until:
            return

        self._circuit_until = now + timedelta(minutes=break_minutes)
        self._consecutive_failures = 0
        reason = (
            "постоянная ошибка — проверьте BotToken и AdminChatId"
            if outcome == NotificationOutcome.PERMANENT
            else f"{failures} неудачных отправок подряд"
        )
        self._log.warning(f"Уведомления приостановлены на {break_minutes} мин: {reason}")
        self._logger.warning(f"notifications circuit opened for {break_minutes} min ({reason})")

    def _suppress(self) -> None:
        self._suppressed += 1
