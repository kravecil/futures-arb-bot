"""
Поток лучших цен биржи через WebSocket (ccxt.pro): подписка на bid/ask выбранных пар,
локальный кэш котировок и контроль «живости» потока.

Предпочтительный метод — watch_bids_asks (только лучшие цены, самый лёгкий канал), запасной —
watch_tickers. Каждый вызов watch_* возвращает пары, обновившиеся с прошлого раза; ccxt сам держит
соединение и переподключается, а здесь сбои только переживаются с нарастающей паузой.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Collection, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

import ccxt

from futures_arb_bot.core.abstractions import EventLog
from futures_arb_bot.core.clock import Clock
from futures_arb_bot.core.domain.market_data import TickerSnapshot
from futures_arb_bot.core.engine.logging_bridge import get_logger
from futures_arb_bot.core.errors import message_of
from futures_arb_bot.core.numbers import ZERO, to_dec

CHUNK_SIZE = 50
"""Пар на одну подписку: большие пакеты биржи иногда отвергают, мелкие — плодят задачи."""

_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0)

_QUOTE_HASH_PREFIXES = ("bidask", "ticker")
"""Префиксы ключей ожидания ccxt у подписок на лучшие цены и тикеры (watch_bids_asks / watch_tickers)."""

_MAX_RECONNECT_GAP_SECONDS = 60.0


class QuoteStream:
    def __init__(self, api: Any, exchange_id: str, display_name: str, log: EventLog, clock: Clock) -> None:
        self._api = api
        self._exchange_id = exchange_id
        self._display_name = display_name
        self._log = log
        self._clock = clock
        self._logger = get_logger(f"Ws.{exchange_id}")
        has = getattr(api, "has", {}) or {}
        if has.get("watchBidsAsks"):
            self._method: str | None = "watch_bids_asks"
        elif has.get("watchTickers"):
            self._method = "watch_tickers"
        else:
            self._method = None
        self._symbols: set[str] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._raw: dict[str, tuple[float, float, float | None, datetime]] = {}
        self._last_message: float | None = None
        self._last_message_at: datetime | None = None
        self._started_at: float | None = None
        self._messages = 0
        self._failing = False
        self._forced_at: float | None = None
        self._forced_gap: float | None = None
        self._forced_total = 0

    @property
    def supported(self) -> bool:
        """Есть ли у биржи в ccxt поток лучших цен или тикеров."""
        return self._method is not None

    @property
    def method(self) -> str | None:
        return self._method

    @property
    def symbols(self) -> set[str]:
        return set(self._symbols)

    @property
    def messages(self) -> int:
        """Сколько обновлений котировок пришло по потоку за сеанс."""
        return self._messages

    @property
    def started(self) -> bool:
        return self._started_at is not None

    def subscribe(self, symbols: Collection[str]) -> list[str]:
        """Добавить пары в подписку (уже подписанные не трогаются). Возвращает новые пары."""
        if self._method is None:
            return []
        fresh = sorted(s for s in symbols if s not in self._symbols)
        if not fresh:
            return []
        if self._started_at is None:
            self._started_at = time.monotonic()
        self._symbols.update(fresh)
        loop = asyncio.get_running_loop()
        for i in range(0, len(fresh), CHUNK_SIZE):
            chunk = fresh[i : i + CHUNK_SIZE]
            self._tasks.append(loop.create_task(self._run(chunk), name=f"ws-{self._exchange_id}-{i}"))
        return fresh

    def healthy(self, stale_after_seconds: float) -> bool:
        """Поток жив: обновления приходили не позже stale_after_seconds назад."""
        if self._last_message is None:
            return False
        return time.monotonic() - self._last_message < stale_after_seconds

    def silent_for(self) -> float | None:
        """Сколько секунд поток молчит (с момента подписки, если данных ещё не было)."""
        anchor = self._last_message if self._last_message is not None else self._started_at
        return None if anchor is None else time.monotonic() - anchor

    def snapshot(self, volumes: Mapping[str, Decimal]) -> dict[str, TickerSnapshot]:
        """
        Текущие котировки потока. Время котировки — момент, когда поток последний раз прислал
        ИМЕННО ЭТУ пару (а не любую пару потока): иначе застывшая цена пары, по которой подписка
        умолкла или которая пережила переподключение, выглядит свежей и рождает фантомный спред.
        Неизменная пара честно стареет и отсекается фильтром max_ticker_age_seconds — для арбитража
        лучше пропустить сделку, чем войти по старой цене. Оборот за 24 ч берётся из последнего снимка REST.
        """
        result: dict[str, TickerSnapshot] = {}
        for symbol, (bid, ask, last, as_of) in self._raw.items():
            bid_d = to_dec(bid)
            result[symbol] = TickerSnapshot(
                self._exchange_id, symbol, bid_d, to_dec(ask), to_dec(last) if last else bid_d, volumes.get(symbol, ZERO), as_of
            )
        return result

    @property
    def forced_reconnects(self) -> int:
        """Сколько раз за сеанс соединение котировок сбрасывалось сторожевым таймером."""
        return self._forced_total

    def force_reconnect(self, silent_seconds: float, min_gap_seconds: float) -> int:
        """
        Сбросить «зависшие» WebSocket-соединения котировок: соединение не закрыто, но данные не идут,
        а ccxt заметит это только по пропущенному ping-pong (у OKX/Bybit ~36 с). Сброс идёт тем же
        путём, что и тайм-аут ping-pong ccxt: ожидания отклоняются, соединение закрывается, следующий
        watch_* открывает новое. Повторный сброс — не раньше min_gap_seconds, а пока поток молчит,
        пауза между сбросами удваивается (до минуты). Возвращает число сброшенных соединений
        (0 — рано или сбрасывать нечего).
        """
        now = time.monotonic()
        gap = self._forced_gap if self._forced_gap is not None else min_gap_seconds
        if self._forced_at is not None and now - self._forced_at < gap:
            return 0

        clients = getattr(self._api, "clients", None) or {}
        reset = 0
        for client in list(clients.values()):
            if not _is_quote_client(client):
                continue
            on_error = getattr(client, "on_error", None)
            if on_error is None:
                continue
            try:
                on_error(ccxt.RequestTimeout(f"поток котировок молчит {silent_seconds:.0f} с — соединение сброшено сторожевым таймером"))
                reset += 1
            except Exception as ex:
                self._logger.debug("force reconnect failed", exc_info=ex)

        self._forced_at = now
        self._forced_gap = min(gap * 2, _MAX_RECONNECT_GAP_SECONDS) if self._forced_gap is not None else min_gap_seconds * 2
        self._forced_total += reset
        return reset

    async def aclose(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task

    # ------------------------- внутреннее -------------------------

    async def _run(self, chunk: list[str]) -> None:
        assert self._method is not None
        watch = getattr(self._api, self._method)
        failures = 0
        while True:
            try:
                updates = await watch(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                failures += 1
                # котировки пачки пережили обрыв — им больше нельзя верить: после переподключения
                # в расчёт попадут только пары, по которым поток реально прислал новые цены
                self._drop(chunk)
                if not self._failing:
                    self._failing = True
                    self._log.warning(f"[{self._display_name}] поток котировок прерван: {message_of(ex)} — переподключаюсь")
                self._logger.debug(f"{self._method} failed", exc_info=ex)
                await asyncio.sleep(_BACKOFF_SECONDS[min(failures, len(_BACKOFF_SECONDS)) - 1])
                continue

            failures = 0
            if self._failing:
                self._failing = False
                self._log.info(f"[{self._display_name}] поток котировок восстановлен")
            self._apply(updates)

    def _drop(self, symbols: Collection[str]) -> None:
        for symbol in symbols:
            self._raw.pop(symbol, None)

    def _apply(self, updates: Any) -> None:
        if not isinstance(updates, Mapping):
            return
        received = self._clock.now()
        applied = False
        for symbol, ticker in updates.items():
            if not isinstance(ticker, Mapping):
                continue
            bid = ticker.get("bid")
            ask = ticker.get("ask")
            if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)) or bid <= 0 or ask <= 0:
                continue
            # время котировки — момент её получения: метка биржи у части каналов — время последней
            # сделки, а не стакана, и вдобавок зависит от расхождения часов
            stamp = received
            last = ticker.get("last")
            self._raw[symbol] = (float(bid), float(ask), float(last) if isinstance(last, (int, float)) else None, stamp)
            applied = True
        if applied:
            self._forced_gap = None  # поток ожил — следующий сброс снова через обычную паузу
            self._messages += 1
            self._last_message = time.monotonic()
            self._last_message_at = received


def _is_quote_client(client: Any) -> bool:
    """WebSocket-клиент ccxt, через который идут наши котировки (а не, например, поток своих заявок)."""
    for attribute in ("futures", "subscriptions"):
        keys = getattr(client, attribute, None)
        if isinstance(keys, Mapping) and any(str(k).startswith(_QUOTE_HASH_PREFIXES) for k in keys):
            return True
    return False
