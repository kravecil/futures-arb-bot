"""
Поток собственных заявок через WebSocket (ccxt.pro, watch_orders): кэш последних статусов
по id заявки и сигнал «что-то изменилось» для исполнителя, ждущего исполнения.

Поток заявок шлёт сообщения только при изменении заявки, поэтому статус без изменений дольше
max_cache_age_ms перепроверяется запросом REST — страховка от «тихого» обрыва соединения,
при котором кэш застыл бы на старом состоянии.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Mapping
from typing import Any

from futures_arb_bot.core.abstractions import EventLog
from futures_arb_bot.core.domain.market_data import OrderUpdate
from futures_arb_bot.core.engine.logging_bridge import get_logger
from futures_arb_bot.core.errors import message_of

_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0)
_CACHE_LIMIT = 2_000

OrderParser = Callable[[Mapping[str, Any]], OrderUpdate | None]


class OrderStream:
    def __init__(self, api: Any, exchange_id: str, display_name: str, log: EventLog, parse: OrderParser) -> None:
        self._api = api
        self._display_name = display_name
        self._log = log
        self._parse = parse
        self._logger = get_logger(f"WsOrders.{exchange_id}")
        has = getattr(api, "has", {}) or {}
        self._supported = bool(has.get("watchOrders"))
        self._task: asyncio.Task[None] | None = None
        self._states: dict[str, tuple[OrderUpdate, float]] = {}
        self._client_ids: dict[str, str] = {}
        self._failing = False
        self._events = 0
        self._changed = asyncio.Condition()
        self._version = 0

    @property
    def supported(self) -> bool:
        return self._supported

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def healthy(self) -> bool:
        """Поток запущен и не в состоянии переподключения."""
        return self.running and not self._failing

    @property
    def events(self) -> int:
        """Сколько событий по заявкам пришло через поток за сеанс."""
        return self._events

    def start(self) -> bool:
        if not self._supported or self.running:
            return False
        self._task = asyncio.get_running_loop().create_task(self._run(), name=f"ws-orders-{self._display_name}")
        return True

    def remember(self, update: OrderUpdate, client_order_id: str | None = None) -> None:
        """Состояние заявки из ответа REST/WS-запроса: «свежее» на текущий момент."""
        self._store(update, client_order_id)

    def forget(self, order_id: str) -> None:
        self._states.pop(order_id, None)

    def get(self, order_id: str, max_age_seconds: float) -> OrderUpdate | None:
        """Статус из кэша, если поток жив и запись моложе max_age_seconds; иначе None — спросить REST."""
        if not self.healthy:
            return None
        entry = self._states.get(order_id)
        if entry is None:
            return None
        update, stored_at = entry
        if time.monotonic() - stored_at > max_age_seconds:
            return None
        return update

    def find_by_client_id(self, client_order_id: str) -> str | None:
        """id заявки по нашему clientOrderId, если поток её уже видел."""
        return self._client_ids.get(client_order_id)

    async def wait_update(self, timeout: float) -> bool:
        """Дождаться любого изменения заявок (или таймаута). True — изменение было."""
        if not self.healthy or timeout <= 0:
            if timeout > 0:
                await asyncio.sleep(timeout)
            return False
        async with self._changed:
            seen = self._version
            try:
                await asyncio.wait_for(self._changed.wait_for(lambda: self._version != seen), timeout)
            except TimeoutError:
                return False
            return True

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    # ------------------------- внутреннее -------------------------

    def _store(self, update: OrderUpdate, client_order_id: str | None) -> None:
        if len(self._states) > _CACHE_LIMIT:
            oldest = sorted(self._states.items(), key=lambda kv: kv[1][1])[: _CACHE_LIMIT // 2]
            for order_id, _ in oldest:
                self._states.pop(order_id, None)
        self._states[update.order_id] = (update, time.monotonic())
        if client_order_id:
            self._client_ids[client_order_id] = update.order_id

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                orders = await self._api.watch_orders(None, None, None, {"type": "swap"})
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                failures += 1
                if not self._failing:
                    self._failing = True
                    self._log.warning(f"[{self._display_name}] поток заявок прерван: {message_of(ex)} — статусы по REST, переподключаюсь")
                self._logger.debug("watch_orders failed", exc_info=ex)
                await asyncio.sleep(_BACKOFF_SECONDS[min(failures, len(_BACKOFF_SECONDS)) - 1])
                continue

            failures = 0
            if self._failing:
                self._failing = False
                self._log.info(f"[{self._display_name}] поток заявок восстановлен")
            await self._apply(orders)

    async def _apply(self, orders: Any) -> None:
        if not isinstance(orders, list):
            return
        changed = False
        for order in orders:
            if not isinstance(order, Mapping):
                continue
            update = self._parse(order)
            if update is None:
                continue
            client_id = order.get("clientOrderId")
            self._store(update, str(client_id) if client_id else None)
            changed = True
        if changed:
            self._events += 1
            async with self._changed:
                self._version += 1
                self._changed.notify_all()
