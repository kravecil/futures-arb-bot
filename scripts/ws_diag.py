"""
Диагностика WebSocket-потоков котировок: сеть сервера, ccxt или нагрузка внутри робота.

Что делает (по умолчанию 30 минут):
  * берёт биржи и пары ТАК ЖЕ, как робот: включённые биржи из config/exchanges.yaml, общие пары
    с оборотом из config/symbols.yaml (по REST, той же логикой отбора, что у робота);
  * открывает к каждой бирже «сырое» WebSocket-соединение (aiohttp, без ccxt) на лучшие цены
    этих пар — тот же канал биржи, что использует ccxt (Bybit orderbook.1, OKX bbo-tbt, Gate book_ticker);
  * открывает те же подписки кодом самого робота (QuoteStream: ccxt.pro, пачки по 50 пар, keepAlive
    и сторожевой сброс из appsettings.yaml) — это «робот без нагрузки»;
  * раз в --rest-interval с проверяет REST, замеряет задержки цикла событий и пишет каждую > --lag-ms;
  * с --with-bot параллельно запускает самого робота в DryRun (--no-ui), пишет его вывод в отдельный
    файл, ловит в нём замирания потоков и замеряет задержки цикла событий ВНУТРИ робота.

В конце — сводка и разбор каждого замирания робота: замерло ли в ту же секунду «сырое» соединение
(сеть/биржа), был ли занят цикл событий робота (нагрузка в роботе), замер ли ccxt у скрипта (ccxt).

Весь вывод сразу пишется в файлы (копировать из терминала не нужно):
    logs/ws-diag-ГГГГММДД-ЧЧММСС.log           — всё по порядку (общая хронология)
    logs/ws-diag-ГГГГММДД-ЧЧММСС-summary.txt   — только сводка и вывод
    logs/ws-diag-ГГГГММДД-ЧЧММСС-bot.log       — полный вывод робота (с --with-bot)

Запуск из каталога проекта:
    uv run python scripts/ws_diag.py --with-bot              # главный тест: 30 мин, вместе с роботом
    uv run python scripts/ws_diag.py                         # без робота
    uv run python scripts/ws_diag.py --minutes 10 --symbols BTC,ETH --exchanges okx

Робот всегда запускается в DryRun (ARB_GENERAL__NETWORK_MODE=DryRun поверх конфигурации и .env)
с выключенными уведомлениями; если он всё же сообщит другой режим — скрипт его сразу остановит.
Ctrl+C — досрочная остановка: робот останавливается корректно, сводка всё равно пишется.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shlex
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import aiohttp

# ------------------------- константы -------------------------

RAW_WS = {
    "bybit": "wss://stream.bybit.com/v5/public/linear",
    "okx": "wss://ws.okx.com:8443/ws/v5/public",
    "gate": "wss://fx-ws.gateio.ws/v4/ws/usdt",
}
REST_PROBES = {
    "bybit": "https://api.bybit.com/v5/market/time",
    "okx": "https://www.okx.com/api/v5/public/time",
    "gate": "https://api.gateio.ws/api/v4/spot/time",
    "binanceusdm": "https://fapi.binance.com/fapi/v1/time",
    "bitget": "https://api.bitget.com/api/v2/public/time",
    "mexc": "https://contract.mexc.com/api/v1/contract/ping",
}
CHILD_MARK = "@@WSDIAG"
"""Префикс служебных строк, которые робот, запущенный скриптом, пишет в stderr (задержки его цикла событий)."""

PROGRESS_EVERY_SECONDS = 300

# ------------------------- время -------------------------

_T0_MONO = time.monotonic()
_T0_WALL = time.time()


def wall(mono: float | None = None) -> datetime:
    """Монотонное время → местное время (часы робота в консоли тоже местные)."""
    mono = time.monotonic() if mono is None else mono
    return datetime.fromtimestamp(_T0_WALL + (mono - _T0_MONO)).astimezone()


def stamp(mono: float | None = None) -> str:
    return wall(mono).strftime("%H:%M:%S.%f")[:-3]


# ------------------------- вывод: консоль + файлы -------------------------


class Output:
    def __init__(self, main_path: Path | None, summary_path: Path | None) -> None:
        self.main_path = main_path
        self.summary_path = summary_path
        self._main: TextIO | None = open(main_path, "a", encoding="utf-8", buffering=1) if main_path else None  # noqa: SIM115
        self._summary: list[str] = []

    def line(self, text: str, mono: float | None = None, console: bool = True) -> None:
        stamped = f"{stamp(mono)}  {text}"
        if console:
            print(stamped, flush=True)
        if self._main is not None:
            self._main.write(stamped + "\n")

    def report(self, text: str = "") -> None:
        """Строка сводки: в консоль, в общий журнал и в файл сводки."""
        print(text, flush=True)
        self._summary.append(text)
        if self._main is not None:
            self._main.write(text + "\n")

    def close(self) -> None:
        if self.summary_path is not None and self._summary:
            self.summary_path.write_text("\n".join(self._summary) + "\n", encoding="utf-8")
        if self._main is not None:
            self._main.close()
            self._main = None


# ------------------------- статистика -------------------------


@dataclass
class Interval:
    start: float
    end: float | None = None

    def duration(self, now: float | None = None) -> float:
        end = self.end if self.end is not None else (time.monotonic() if now is None else now)
        return max(0.0, end - self.start)

    def overlaps(self, start: float, end: float) -> bool:
        my_end = self.end if self.end is not None else time.monotonic()
        return self.start <= end and my_end >= start


@dataclass
class ChannelStats:
    """Статистика одного канала («okx raw», «okx ccxt»)."""

    name: str
    exchange: str
    kind: str  # raw | ccxt
    symbols: int = 0
    connects: int = 0
    messages: int = 0
    bytes: int = 0
    freezes: list[Interval] = field(default_factory=list)
    first_freeze_after: list[float] = field(default_factory=list)
    bytes_before_first_freeze: list[int] = field(default_factory=list)
    dead_reconnects: int = 0
    forced_reconnects: int = 0
    errors: list[str] = field(default_factory=list)
    ping_rtts: list[float] = field(default_factory=list)

    connected_at: float | None = None
    last_message_at: float | None = None
    bytes_since_connect: int = 0
    open_freeze: Interval | None = None
    froze_this_connection: bool = False

    def on_connect(self) -> None:
        now = time.monotonic()
        self.connects += 1
        self.connected_at = now
        self.last_message_at = now
        self.bytes_since_connect = 0
        self.froze_this_connection = False
        self.close_open_freeze(now)

    def touch(self, at: float, size: int = 0, count: int = 1) -> Interval | None:
        """Пришли данные (в момент at). Если они прервали замирание — возвращает его."""
        self.messages += count
        self.bytes += size
        self.bytes_since_connect += size
        self.last_message_at = at
        if self.open_freeze is not None:
            closed = self.open_freeze
            closed.end = at
            self.open_freeze = None
            return closed
        return None

    def check_freeze(self, freeze_after: float) -> Interval | None:
        """Данных нет дольше freeze_after — открывает замирание (с момента последних данных) и возвращает его."""
        if self.last_message_at is None or self.connected_at is None or self.open_freeze is not None:
            return None
        if time.monotonic() - self.last_message_at < freeze_after:
            return None
        self.open_freeze = Interval(self.last_message_at)
        self.freezes.append(self.open_freeze)
        if not self.froze_this_connection:
            self.froze_this_connection = True
            self.first_freeze_after.append(self.last_message_at - self.connected_at)
            self.bytes_before_first_freeze.append(self.bytes_since_connect)
        return self.open_freeze

    def since_connect(self) -> float:
        if self.connected_at is None or self.last_message_at is None:
            return 0.0
        return self.last_message_at - self.connected_at

    def silent_for(self) -> float:
        return 0.0 if self.last_message_at is None else time.monotonic() - self.last_message_at

    def close_open_freeze(self, at: float | None = None) -> None:
        if self.open_freeze is not None:
            self.open_freeze.end = time.monotonic() if at is None else at
            self.open_freeze = None


@dataclass
class LagEvent:
    at: float
    """Когда задержка закончилась (монотонное время скрипта)."""
    ms: float

    def overlaps(self, start: float, end: float) -> bool:
        return self.at - self.ms / 1000 <= end and self.at >= start


@dataclass
class LagStats:
    name: str
    samples: list[float] = field(default_factory=list)
    """Все замеры (только для цикла скрипта; у робота — медианы периодов)."""
    events: list[LagEvent] = field(default_factory=list)
    """Задержки больше порога."""
    medians: list[float] = field(default_factory=list)
    max_ms: float = 0.0
    count: int = 0


@dataclass
class RestStats:
    name: str
    latencies: list[float] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@dataclass
class BotExchange:
    name: str
    exchange: str
    freezes: list[Interval] = field(default_factory=list)
    open_freeze: Interval | None = None
    forced: int = 0
    broken: list[str] = field(default_factory=list)
    subscribed: int | None = None


# ------------------------- выбор бирж и пар — как у робота -------------------------


class DiagLog:
    """Журнал для кода робота внутри скрипта (протокол EventLog): предупреждения — в общий вывод."""

    def __init__(self, out: Output, prefix: str, stats: ChannelStats | None = None) -> None:
        self._out = out
        self._prefix = prefix
        self.stats = stats

    def log(self, level: Any, message: str) -> None:
        text = str(level).lower()
        if text == "debug":
            return
        if self.stats is not None:
            if "поток котировок прерван" in message:
                self.stats.errors.append(message.split("прерван:", 1)[-1].strip())
            elif "поток котировок восстановлен" in message:
                self.stats.connects += 1
        self._out.line(f"[{self._prefix}] {message}", console=text in ("warning", "error"))

    def latest(self, count: int) -> list[Any]:
        return []

    def tail(self, after_id: int, max_count: int) -> list[Any]:
        return []

    def errors(self, count: int) -> list[Any]:
        return []

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        self.log("info", message)

    def success(self, message: str) -> None:
        self.log("info", message)

    def warning(self, message: str) -> None:
        self.log("warning", message)

    def error(self, message: str) -> None:
        self.log("error", message)


@dataclass
class ExchangePlan:
    exchange: str
    name: str
    symbols: list[str]
    raw_ids: list[str]
    connector: Any
    log: DiagLog


async def build_plan(args: argparse.Namespace, provider: Any, out: Output) -> list[ExchangePlan]:
    from futures_arb_bot.core.clock import SystemClock
    from futures_arb_bot.core.domain.enums import NetworkMode
    from futures_arb_bot.core.engine.symbol_filter import DefaultSymbolFilter
    from futures_arb_bot.infrastructure.ccxt_connector import CcxtExchangeConnector

    options = provider.current
    wanted_ids = [e.strip().lower() for e in args.exchanges.split(",") if e.strip()] if args.exchanges else None
    entries = [e for e in options.exchanges.items if (e.id.lower() in wanted_ids if wanted_ids else e.enabled)]
    if wanted_ids:
        known = {e.id.lower() for e in options.exchanges.items}
        for missing in [i for i in wanted_ids if i not in known]:
            out.line(f"биржи «{missing}» нет в exchanges.yaml — пропускаю")
    if len(entries) < 1:
        raise RuntimeError("нет бирж для проверки (включите их в config/exchanges.yaml или укажите --exchanges)")

    connectors: list[tuple[Any, Any, DiagLog]] = []
    for entry in entries:
        log = DiagLog(out, f"{entry.id.lower()} ccxt")
        connector = CcxtExchangeConnector(entry, NetworkMode.DRY_RUN, log, SystemClock())
        if args.proxy:
            with contextlib.suppress(Exception):
                connector._api.wssProxy = args.proxy
                connector._api.httpsProxy = args.proxy
        try:
            await connector.connect()
            connectors.append((entry, connector, log))
        except Exception as ex:
            out.line(f"[{entry.id}] не удалось загрузить рынки: {type(ex).__name__}: {str(ex)[:200]}")
            with contextlib.suppress(Exception):
                await connector.aclose()

    symbol_filter = DefaultSymbolFilter(provider)
    allowed = {c.id: {m.symbol for m in c.perpetual_markets if symbol_filter.is_allowed(m, 0)} for _, c, _ in connectors}

    per_exchange: dict[str, set[str]] = {}
    if args.symbols.strip().lower() == "auto":
        # та же логика, что ArbitrageScanner._update_subscriptions: пары минимум на двух биржах,
        # с оборотом не ниже порога, не больше max_symbols самых ликвидных
        symbols_options = options.symbols
        book: dict[str, dict[str, Any]] = {}
        for _, connector, _ in connectors:
            try:
                result = await connector.fetch_tickers()
            except Exception as ex:
                out.line(f"[{connector.id}] не удалось получить тикеры: {type(ex).__name__}: {str(ex)[:200]}")
                continue
            for symbol, ticker in result.tickers.items():
                if symbol not in allowed[connector.id]:
                    continue
                if ticker.quote_volume_24h > 0 and ticker.quote_volume_24h < symbols_options.min_quote_volume24h_usd:
                    continue
                book.setdefault(symbol, {})[connector.id] = ticker.quote_volume_24h
        min_exchanges = 2 if len(connectors) >= 2 else 1
        cross = [(s, max(v.values())) for s, v in book.items() if len(v) >= min_exchanges]
        if symbols_options.max_symbols > 0:
            cross = sorted(cross, key=lambda x: x[1], reverse=True)[: symbols_options.max_symbols]
        wanted = {s for s, _ in cross}
        for _, connector, _ in connectors:
            per_exchange[connector.id] = {s for s in wanted if connector.id in book.get(s, {})}
    else:
        bases = [b.strip().upper() for b in args.symbols.split(",") if b.strip()]
        for _, connector, _ in connectors:
            markets = {m.symbol for m in connector.perpetual_markets}
            per_exchange[connector.id] = {f"{b}/USDT:USDT" for b in bases if f"{b}/USDT:USDT" in markets}

    plans: list[ExchangePlan] = []
    for entry, connector, log in connectors:
        symbols = sorted(per_exchange.get(connector.id, set()))
        if not symbols:
            out.line(f"[{connector.id}] нет пар для подписки — биржа пропущена")
            with contextlib.suppress(Exception):
                await connector.aclose()
            continue
        markets = getattr(connector._api, "markets", {}) or {}
        raw_ids = [str(markets[s]["id"]) for s in symbols if s in markets and markets[s].get("id")]
        plans.append(ExchangePlan(connector.id.lower(), entry.name if entry.name.strip() else entry.id, symbols, raw_ids, connector, log))
    return plans


# ------------------------- «сырые» WebSocket -------------------------


def raw_subscribe(exchange: str, ids: list[str]) -> list[str]:
    match exchange:
        case "bybit":
            topics = [f"orderbook.1.{i}" for i in ids]
            return [json.dumps({"op": "subscribe", "args": topics[k : k + 10]}) for k in range(0, len(topics), 10)]
        case "okx":
            args = [{"channel": "bbo-tbt", "instId": i} for i in ids]
            return [json.dumps({"op": "subscribe", "args": args[k : k + 50]}) for k in range(0, len(args), 50)]
        case "gate":
            return [
                json.dumps({"time": int(time.time()), "channel": "futures.book_ticker", "event": "subscribe", "payload": ids[k : k + 50]})
                for k in range(0, len(ids), 50)
            ]
    raise ValueError(exchange)


def raw_ping(exchange: str) -> str:
    match exchange:
        case "bybit":
            return json.dumps({"op": "ping"})
        case "okx":
            return "ping"
        case "gate":
            return json.dumps({"time": int(time.time()), "channel": "futures.ping"})
    raise ValueError(exchange)


def raw_is_pong(exchange: str, data: str) -> bool:
    match exchange:
        case "okx":
            return data == "pong"
        case "bybit":
            return '"pong"' in data and ('"op":"pong"' in data or '"ret_msg":"pong"' in data or '"op": "pong"' in data)
        case "gate":
            return "futures.pong" in data
    return False


async def raw_channel(plan: ExchangePlan, url: str, stats: ChannelStats, args: argparse.Namespace, out: Output, stop: asyncio.Event) -> None:
    attempt = 0
    async with aiohttp.ClientSession(trust_env=args.trust_env) as session:
        while not stop.is_set():
            try:
                async with session.ws_connect(
                    url, proxy=args.proxy, heartbeat=None, max_msg_size=0, timeout=aiohttp.ClientWSTimeout(ws_close=5)
                ) as ws:
                    attempt = 0
                    stats.on_connect()
                    out.line(f"[{stats.name}] подключено (#{stats.connects}), пар {len(plan.raw_ids)}")
                    for message in raw_subscribe(plan.exchange, plan.raw_ids):
                        await ws.send_str(message)
                    ping_sent: list[float] = []
                    closed_by_us: list[bool] = []

                    async def pinger(ws: aiohttp.ClientWebSocketResponse = ws, sent: list[float] = ping_sent) -> None:
                        while not ws.closed:
                            await asyncio.sleep(args.ping)
                            sent.append(time.monotonic())
                            with contextlib.suppress(Exception):
                                await ws.send_str(raw_ping(plan.exchange))

                    async def watchdog(ws: aiohttp.ClientWebSocketResponse = ws, ours: list[bool] = closed_by_us) -> None:
                        while not ws.closed:
                            await asyncio.sleep(0.25)
                            opened = stats.check_freeze(args.freeze)
                            if opened is not None:
                                out.line(
                                    f"[{stats.name}] ЗАМИРАНИЕ: данных нет с {stamp(opened.start)} "
                                    f"(через {stats.since_connect():.1f} с после подключения, получено {stats.bytes_since_connect / 1024:.0f} КБ)",
                                )
                            if stats.silent_for() >= args.dead:
                                stats.dead_reconnects += 1
                                out.line(f"[{stats.name}] соединение мёртвое ({args.dead:g} с без данных) — закрываю и переподключаюсь")
                                ours.append(True)
                                await ws.close()
                                return

                    tasks = [asyncio.create_task(pinger()), asyncio.create_task(watchdog())]
                    try:
                        async for msg in ws:
                            if stop.is_set():
                                break
                            if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                                data = msg.data if isinstance(msg.data, str) else msg.data.decode("utf-8", "replace")
                                now = time.monotonic()
                                if raw_is_pong(plan.exchange, data):
                                    if ping_sent:
                                        stats.ping_rtts.append((now - ping_sent.pop(0)) * 1000)
                                    continue  # pong — не данные: соединение может отвечать на ping, а котировки не слать
                                resumed = stats.touch(now, len(data))
                                if resumed is not None:
                                    out.line(f"[{stats.name}] данные пошли снова после {resumed.duration():.1f} с тишины")
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                stats.errors.append(f"ws error: {ws.exception()}")
                                break
                        if not stop.is_set() and not closed_by_us:
                            code = ws.close_code
                            out.line(f"[{stats.name}] соединение закрыто биржей (код {code})")
                            stats.errors.append(f"закрыто, код {code}")
                    finally:
                        for task in tasks:
                            task.cancel()
                        stats.close_open_freeze()
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                stats.errors.append(f"{type(ex).__name__}: {ex}")
                out.line(f"[{stats.name}] ошибка подключения: {type(ex).__name__}: {str(ex)[:200]}")
            if stop.is_set():
                break
            attempt += 1
            await asyncio.sleep(min(10, attempt))


# ------------------------- те же подписки кодом робота (ccxt.pro) -------------------------


async def ccxt_channel(
    plan: ExchangePlan, stats: ChannelStats, md: Any, refresh_ms: int, args: argparse.Namespace, out: Output, stop: asyncio.Event
) -> None:
    from futures_arb_bot.infrastructure.quote_stream import CHUNK_SIZE

    connector = plan.connector
    plan.log.stats = stats
    connector.update_market_data(md, refresh_ms)  # keepAlive — как у робота
    stream = connector._stream
    if stream is None or not stream.supported:
        out.line(f"[{stats.name}] у биржи в ccxt нет потока лучших цен — канал пропущен")
        return
    added = connector.subscribe_quotes(plan.symbols)
    stats.on_connect()
    reconnect_after = md.reconnect_after_ms / 1000
    keep_alive = (getattr(connector._api, "streaming", {}) or {}).get("keepAlive")
    out.line(
        f"[{stats.name}] подписка кодом робота: {stream.method}, пар {len(added)} (пачками по {CHUNK_SIZE}), "
        f"keepAlive {keep_alive} мс, сброс зависшего потока через {reconnect_after:g} с" + (" (выкл.)" if reconnect_after <= 0 else "")
    )
    seen_messages = 0
    while not stop.is_set():
        await asyncio.sleep(0.25)
        last = getattr(stream, "_last_message", None)
        if stream.messages != seen_messages and last is not None:
            resumed = stats.touch(last, count=stream.messages - seen_messages)
            seen_messages = stream.messages
            if resumed is not None:
                out.line(f"[{stats.name}] данные пошли снова после {resumed.duration():.1f} с тишины")
        opened = stats.check_freeze(args.freeze)
        if opened is not None:
            out.line(
                f"[{stats.name}] ЗАМИРАНИЕ: данных нет с {stamp(opened.start)} (через {stats.since_connect():.1f} с после (пере)подключения)",
            )
        silent = stream.silent_for()
        if reconnect_after > 0 and silent is not None and silent >= reconnect_after:
            reset = stream.force_reconnect(silent, reconnect_after)
            if reset > 0:
                stats.forced_reconnects += 1
                out.line(f"[{stats.name}] поток молчит {silent:.0f} с — сбрасываю соединение (как сторожевой таймер робота)")


# ------------------------- REST-проба и цикл событий -------------------------


async def rest_probe(url: str, stats: RestStats, args: argparse.Namespace, out: Output, stop: asyncio.Event) -> None:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(trust_env=args.trust_env, timeout=timeout) as session:
        while not stop.is_set():
            started = time.monotonic()
            try:
                async with session.get(url, proxy=args.proxy) as response:
                    await response.read()
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
                elapsed = (time.monotonic() - started) * 1000
                stats.latencies.append(elapsed)
                if elapsed > 2000:
                    out.line(f"[{stats.name}] медленный ответ REST: {elapsed:.0f} мс")
            except Exception as ex:
                stats.failures.append(f"{type(ex).__name__}: {ex}")
                out.line(f"[{stats.name}] REST не ответил: {type(ex).__name__}: {str(ex)[:200]}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), args.rest_interval)


async def loop_lag(stats: LagStats, threshold_ms: float, out: Output, stop: asyncio.Event) -> None:
    while not stop.is_set():
        started = time.monotonic()
        await asyncio.sleep(0.1)
        now = time.monotonic()
        lag = max(0.0, (now - started - 0.1) * 1000)
        stats.samples.append(lag)
        stats.count += 1
        stats.max_ms = max(stats.max_ms, lag)
        if lag >= threshold_ms:
            stats.events.append(LagEvent(now, lag))
            out.line(f"[цикл событий скрипта] задержка {lag:.0f} мс", mono=now)


# ------------------------- робот как дочерний процесс -------------------------

RE_SESSION = re.compile(r"Сеанс запущен: режим (?P<mode>[^,\s]+)")
RE_SILENT = re.compile(r"\[(?P<ex>[^\]]+)\] поток котировок молчит (?P<s>\d+) с — котировки по REST")
RE_FORCED = re.compile(r"\[(?P<ex>[^\]]+)\] поток котировок молчит (?P<s>\d+) с — переподключаю принудительно")
RE_BROKEN = re.compile(r"\[(?P<ex>[^\]]+)\] поток котировок прерван: (?P<msg>.*)")
RE_BACK = re.compile(r"\[(?P<ex>[^\]]+)\] котировки снова из потока WebSocket")
RE_RESTORED = re.compile(r"\[(?P<ex>[^\]]+)\] поток котировок восстановлен")
RE_SUBS = re.compile(r"\[(?P<ex>[^\]]+)\] котировки по WebSocket: \+\d+ пар, всего в подписке (?P<n>\d+)")
LOUD = ("Фатальная ошибка", "Traceback", "не подключилась", "Ошибка конфигурации", "Сеанс запущен", "Сеанс завершён", "Сканер запущен")


class BotProcess:
    def __init__(self, args: argparse.Namespace, out: Output, log_path: Path | None, names: dict[str, str]) -> None:
        self._args = args
        self._out = out
        self.log_path = log_path
        self._file: TextIO | None = open(log_path, "a", encoding="utf-8", buffering=1) if log_path else None  # noqa: SIM115
        self._names = names
        self.proc: asyncio.subprocess.Process | None = None
        self._pumps: list[asyncio.Task[None]] = []
        self.exchanges: dict[str, BotExchange] = {}
        self.lag = LagStats("робот")
        self.mode: str | None = None
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.unsafe = False
        self.lines = 0

    def exchange(self, display: str) -> BotExchange:
        key = self._names.get(display.lower(), display.lower())
        found = self.exchanges.get(key)
        if found is None:
            found = self.exchanges[key] = BotExchange(display, key)
        return found

    async def start(self) -> None:
        script = str(Path(__file__).resolve())
        bot_args = shlex.split(self._args.bot_args or "", posix=os.name != "nt")
        command = [sys.executable, "-u", script, "--bot-child", str(self._args.lag_ms), *bot_args]
        if self._args.bot_cmd:  # только для отладки самого скрипта
            command = shlex.split(self._args.bot_cmd, posix=os.name != "nt")
        env = dict(os.environ)
        env.update(
            {
                "ARB_GENERAL__NETWORK_MODE": "DryRun",
                "ARB_NOTIFICATIONS__ENABLED": "false",
                "PYTHONUNBUFFERED": "1",
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "COLUMNS": "250",
            }
        )
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # Ctrl+Break уйдёт только роботу
        else:
            kwargs["start_new_session"] = True  # Ctrl+C терминала не попадёт в робота: остановим его сами
        self.proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=4 * 1024 * 1024,
            **kwargs,
        )
        self.started_at = time.monotonic()
        self._out.line(f"[бот] запущен в DryRun (pid {self.proc.pid}), вывод — {self.log_path or 'не сохраняется'}")
        assert self.proc.stdout is not None and self.proc.stderr is not None
        self._pumps = [asyncio.create_task(self._pump(self.proc.stdout, False)), asyncio.create_task(self._pump(self.proc.stderr, True))]

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _pump(self, stream: asyncio.StreamReader, is_err: bool) -> None:
        while True:
            try:
                raw = await stream.readline()
            except ValueError:
                continue  # слишком длинная строка — пропускаем
            if not raw:
                return
            now = time.monotonic()
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith(CHILD_MARK):
                self._on_child_mark(line, now)
                continue
            self.lines += 1
            if self._file is not None:
                self._file.write(f"{stamp(now)} {'E ' if is_err else ''}{line}\n")
            if line.strip():
                self._on_line(line, now)

    def _on_child_mark(self, line: str, now: float) -> None:
        parts = line.split()
        with contextlib.suppress(Exception):
            if parts[1] == "lag":
                ms = float(parts[2])
                self.lag.events.append(LagEvent(now, ms))
                self.lag.max_ms = max(self.lag.max_ms, ms)
                self._out.line(f"[бот: цикл событий] задержка {ms:.0f} мс", mono=now)
            elif parts[1] == "stat":
                count, median, maximum = int(parts[2]), float(parts[3]), float(parts[4])
                self.lag.count += count
                self.lag.medians.append(median)
                self.lag.max_ms = max(self.lag.max_ms, maximum)

    def _on_line(self, line: str, now: float) -> None:
        loud = any(word in line for word in LOUD)
        if (m := RE_SESSION.search(line)) is not None:
            self.mode = m["mode"]
            if self.mode.lower().replace("_", "") != "dryrun":
                self.unsafe = True
                self._out.line(f"[бот] !!! робот сообщил режим {self.mode}, а не DryRun — немедленно останавливаю")
                asyncio.get_running_loop().create_task(self.stop())
        if (m := RE_SILENT.search(line)) is not None:
            ex = self.exchange(m["ex"])
            if ex.open_freeze is None:
                ex.open_freeze = Interval(now - float(m["s"]))
                ex.freezes.append(ex.open_freeze)
            self._out.line(f"[бот] ЗАМИРАНИЕ {ex.exchange} (данных нет с ~{stamp(ex.open_freeze.start)}): {line.split(']', 1)[-1].strip()}", mono=now)
            return
        if (m := RE_FORCED.search(line)) is not None:
            ex = self.exchange(m["ex"])
            ex.forced += 1
            self._out.line(f"[бот] {ex.exchange}: сторожевой сброс потока (молчал {m['s']} с)", mono=now)
            return
        if (m := RE_BROKEN.search(line)) is not None:
            ex = self.exchange(m["ex"])
            ex.broken.append(m["msg"].strip())
            self._out.line(f"[бот] {ex.exchange}: поток прерван: {m['msg'].strip()[:200]}", mono=now)
            return
        if (m := RE_BACK.search(line)) is not None:
            ex = self.exchange(m["ex"])
            if ex.open_freeze is not None:
                ex.open_freeze.end = now
                self._out.line(f"[бот] {ex.exchange}: поток ожил после {ex.open_freeze.duration():.1f} с", mono=now)
                ex.open_freeze = None
            return
        if (m := RE_RESTORED.search(line)) is not None:
            self._out.line(f"[бот] {self.exchange(m['ex']).exchange}: поток котировок восстановлен", mono=now, console=False)
            return
        if (m := RE_SUBS.search(line)) is not None:
            ex = self.exchange(m["ex"])
            ex.subscribed = int(m["n"])
            self._out.line(f"[бот] {ex.exchange}: в подписке {ex.subscribed} пар", mono=now)
            return
        if loud:
            self._out.line(f"[бот] {line.strip()[:300]}", mono=now)

    async def stop(self, timeout: float = 60.0) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.returncode is None:
            self._out.line("[бот] останавливаю (как по Ctrl+C) и жду итоговый отчёт робота…")
            with contextlib.suppress(Exception):
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except TimeoutError:
                self._out.line(f"[бот] не остановился за {timeout:.0f} с — завершаю принудительно")
                self.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 10)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(*self._pumps, return_exceptions=True), 5)
        self.exit_code = proc.returncode
        now = time.monotonic()
        for ex in self.exchanges.values():
            if ex.open_freeze is not None:
                ex.open_freeze.end = now
                ex.open_freeze = None
        if self._file is not None:
            self._file.close()
            self._file = None

    def kill(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(Exception):
                self.proc.kill()


_BOT: BotProcess | None = None
"""Ссылка на робота — чтобы убить его, если скрипт прервут повторным Ctrl+C."""


# ------------------------- режим дочернего процесса: робот + замер его цикла событий -------------------------


def run_bot_child(lag_ms: float, bot_argv: list[str]) -> int:
    import futures_arb_bot.__main__ as bot_main

    original = bot_main._main_async

    async def monitor() -> None:
        samples: list[float] = []
        period_started = time.monotonic()

        def flush() -> None:
            nonlocal samples, period_started
            if samples:
                sys.stderr.write(f"{CHILD_MARK} stat {len(samples)} {statistics.median(samples):.1f} {max(samples):.1f}\n")
                sys.stderr.flush()
            samples = []
            period_started = time.monotonic()

        try:
            while True:
                started = time.monotonic()
                await asyncio.sleep(0.1)
                lag = max(0.0, (time.monotonic() - started - 0.1) * 1000)
                samples.append(lag)
                if lag >= lag_ms:
                    sys.stderr.write(f"{CHILD_MARK} lag {lag:.0f}\n")
                    sys.stderr.flush()
                if time.monotonic() - period_started >= 30:
                    flush()
        finally:
            flush()

    async def wrapped(cli: Any) -> int:
        task = asyncio.create_task(monitor())
        try:
            return await original(cli)
        finally:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    bot_main._main_async = wrapped
    argv = list(bot_argv)
    if "--no-ui" not in argv:
        argv.insert(0, "--no-ui")
    return bot_main.main(argv)


# ------------------------- сводка -------------------------


def fmt(values: list[float], unit: str = "с", digits: int = 1) -> str:
    if not values:
        return "—"
    return f"медиана {statistics.median(values):.{digits}f} {unit}, макс {max(values):.{digits}f} {unit}"


def max_lag(lag: LagStats, start: float, end: float) -> float:
    return max((e.ms for e in lag.events if e.overlaps(start, end)), default=0.0)


def summary(
    channels: list[ChannelStats],
    rests: list[RestStats],
    lag: LagStats,
    bot: BotProcess | None,
    seconds: float,
    args: argparse.Namespace,
    out: Output,
) -> None:
    r = out.report
    r()
    r("=" * 100)
    r(f"СВОДКА за {seconds / 60:.1f} мин (замирание — нет данных {args.freeze:g}+ с; сырое соединение мёртвое — {args.dead:g}+ с)")
    r("=" * 100)
    for c in channels:
        rate = c.messages / seconds if seconds > 0 else 0
        total_frozen = sum(f.duration() for f in c.freezes)
        r(f"\n[{c.name}]  пар {c.symbols}")
        if c.kind == "raw":
            r(f"  подключений: {c.connects}, сообщений: {c.messages} ({rate:.1f}/с), получено {c.bytes / 1024 / 1024:.1f} МБ")
        else:
            r(f"  (пере)подключений: {c.connects}, обновлений от ccxt: {c.messages} ({rate:.1f}/с)")
        r(
            f"  замираний: {len(c.freezes)}; длительность: {fmt([f.duration() for f in c.freezes])}; "
            f"суммарно {total_frozen:.0f} с ({total_frozen / seconds * 100 if seconds else 0:.1f}% времени)"
        )
        if c.first_freeze_after:
            r(f"  первое замирание после подключения: {fmt(c.first_freeze_after)}")
        if c.kind == "raw" and c.bytes_before_first_freeze:
            r(f"  получено до первого замирания: {fmt([b / 1024 for b in c.bytes_before_first_freeze], 'КБ', 0)}")
        if c.dead_reconnects:
            r(f"  мёртвых соединений (переподключено скриптом): {c.dead_reconnects}")
        if c.forced_reconnects:
            r(f"  сбросов сторожевым таймером: {c.forced_reconnects}")
        if c.ping_rtts:
            r(f"  ping-pong: {fmt(c.ping_rtts, 'мс', 0)}")
        if c.errors:
            r(f"  ошибок: {len(c.errors)}; последние: {' | '.join(c.errors[-3:])[:300]}")
    for rest in rests:
        r(
            f"\n[{rest.name}]  запросов {len(rest.latencies) + len(rest.failures)}, ошибок {len(rest.failures)}; задержка: {fmt(rest.latencies, 'мс', 0)}"
        )
    if lag.samples:
        r(
            f"\n[цикл событий скрипта] задержка: {fmt(lag.samples, 'мс', 0)}; больше {args.lag_ms:g} мс — {len(lag.events)} раз"
            + (f" (в {', '.join(stamp(e.at)[:8] for e in lag.events[:8])}{'…' if len(lag.events) > 8 else ''})" if lag.events else "")
        )

    episodes: list[tuple[BotExchange, Interval, str, str]] = []
    if bot is not None:
        r(
            f"\n[робот, DryRun]  режим по журналу робота: {bot.mode or 'не дошёл до старта сеанса'}; строк вывода {bot.lines}; код выхода {bot.exit_code}"
        )
        if bot.lag.count:
            r(
                f"  цикл событий робота: медиана {statistics.median(bot.lag.medians):.0f} мс, макс {bot.lag.max_ms:.0f} мс; "
                f"больше {args.lag_ms:g} мс — {len(bot.lag.events)} раз, больше 1 с — {sum(1 for e in bot.lag.events if e.ms >= 1000)} раз"
            )
        else:
            r("  цикл событий робота: замеров нет (робот не запустился или вышел сразу)")
        if not bot.exchanges:
            r("  событий потоков котировок в выводе робота нет: замираний не было (или робот не дошёл до подписки)")
        for ex in bot.exchanges.values():
            durations = [f.duration() for f in ex.freezes]
            plan_count = next((c.symbols for c in channels if c.exchange == ex.exchange), None)
            subs = f"в подписке {ex.subscribed} пар" if ex.subscribed is not None else "подписка в выводе не найдена"
            if ex.subscribed is not None and plan_count is not None and ex.subscribed != plan_count:
                subs += f" (у скрипта {plan_count} — наборы немного разошлись, это не страшно)"
            r(
                f"  {ex.exchange}: {subs}; замираний {len(ex.freezes)} ({fmt(durations)}; суммарно {sum(durations):.0f} с), сторожевых сбросов {ex.forced}, обрывов {len(ex.broken)}"
            )
            if ex.broken:
                r(f"    последние обрывы: {' | '.join(ex.broken[-3:])[:300]}")

        by_name = {c.name: c for c in channels}
        all_bot = sorted(((ex, f) for ex in bot.exchanges.values() for f in ex.freezes), key=lambda x: x[1].start)
        if all_bot:
            r("\n  РАЗБОР ЗАМИРАНИЙ РОБОТА (что происходило в то же время):")
            r("    начало        биржа  длит.   сырое WS   ccxt скрипта   цикл робота   цикл скрипта   → вероятная причина")
        for ex, freeze in all_bot:
            end = freeze.end if freeze.end is not None else time.monotonic()
            window = (freeze.start - 1.0, end + 0.5)
            raw = by_name.get(f"{ex.exchange} raw")
            via = by_name.get(f"{ex.exchange} ccxt")
            raw_hit = raw is not None and any(f.overlaps(*window) for f in raw.freezes)
            via_hit = via is not None and any(f.overlaps(*window) for f in via.freezes)
            bot_lag = max_lag(bot.lag, freeze.start - 2.0, end)
            own_lag = max_lag(lag, freeze.start - 2.0, end)
            if raw_hit:
                cause = "сеть/биржа"
            elif bot_lag >= 1000:
                cause = "робот: цикл событий занят"
            elif via_hit:
                cause = "ccxt"
            elif raw is None:
                cause = "нет сырого канала для сравнения"
            else:
                cause = "только у робота"
            episodes.append((ex, freeze, cause, f"{bot_lag:.0f}"))

            def yn(hit: bool, present: bool) -> str:
                return "—" if not present else ("ЗАМЕРЛО" if hit else "норма")

            r(
                f"    {stamp(freeze.start)}  {ex.exchange:<5}  {freeze.duration():5.1f} с  {yn(raw_hit, raw is not None):<9}  "
                f"{yn(via_hit, via is not None):<13}  {bot_lag:6.0f} мс     {own_lag:6.0f} мс      → {cause}"
            )

    # ------------------------- вывод -------------------------
    raw_channels = [c for c in channels if c.kind == "raw"]
    via_channels = [c for c in channels if c.kind == "ccxt"]
    raw_freezes = sum(len(c.freezes) for c in raw_channels)
    via_freezes = sum(len(c.freezes) for c in via_channels)
    r("\nВЫВОД:")
    if args.interrupted:
        r(f"  * Проверка прервана досрочно ({seconds / 60:.1f} мин) — выводы по короткому отрезку менее надёжны.")
    if lag.max_ms > 1000:
        r(f"  * Даже цикл событий скрипта подвисал до {lag.max_ms:.0f} мс — серверу не хватает CPU/памяти (или его душит соседний процесс).")
    if bot is not None and bot.unsafe:
        r(f"  * Робот сообщил режим {bot.mode}, а не DryRun, и был остановлен — результаты по роботу неполные. Проверьте .env и конфиг.")

    if bot is not None and episodes:
        counts: dict[str, int] = {}
        for _, _, cause, _ in episodes:
            counts[cause] = counts.get(cause, 0) + 1
        r(f"  * Замираний у робота: {len(episodes)}; причины: " + ", ".join(f"{k} — {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
        top = max(counts.items(), key=lambda x: x[1])[0]
        if top == "сеть/биржа":
            r("    В те же секунды замирали «сырые» соединения скрипта — проблема в сети сервера или на стороне биржи, не в коде робота.")
        elif top == "робот: цикл событий занят":
            r("    В эти моменты цикл событий робота был занят больше секунды — робот сам не успевает читать сокеты.")
            r("    Пришлите оба файла журнала: будем искать, что именно блокирует цикл (сканирование, REST-тикеры, разбор данных).")
        elif top == "ccxt":
            r("    «Сырые» соединения работали, а ccxt замирал и у скрипта — проблема в стеке ccxt (кандидат на свой WS-клиент).")
        elif top == "только у робота":
            r("    Сеть и ccxt скрипта в эти моменты работали нормально, цикл событий робота заметно не подвисал — дело в")
            r("    самом роботе (подписки/обработка). Пришлите файлы журнала: -bot.log покажет, что робот делал в эти секунды.")
    elif bot is not None:
        if bot.lag.count == 0:
            r("  * Робот не проработал под наблюдением (см. -bot.log) — проверьте запуск: uv run futures-arb-bot --no-ui.")
        else:
            r("  * У робота за время проверки замираний потоков не было.")
            if raw_freezes == 0 and via_freezes == 0:
                r(
                    "    Скрипт тоже ничего не поймал — сбой не воспроизвёлся. Запустите проверку дольше (--minutes 60) или в то время суток, когда были сбои."
                )

    if raw_freezes == 1:
        hosts = ", ".join(c.exchange for c in raw_channels if c.freezes)
        r(f"  * Одно замирание «сырого» соединения ({hosts}) — единичный случай: само по себе ещё не приговор сети, смотрите разбор выше.")
    elif raw_freezes > 1:
        hosts = ", ".join(c.exchange for c in raw_channels if c.freezes)
        r(f"  * Замирали даже «сырые» соединения без ccxt ({hosts}: {raw_freezes} раз) — признак проблем сети сервера/биржи")
        r("    (провайдер, страна размещения, VPN/прокси, фильтрация). Код робота это не исправит; спасает сторожевой сброс и REST-запас.")
        if any(rest.failures for rest in rests):
            r("  * REST тоже давал сбои — связь с биржами нестабильна в целом.")
    elif via_freezes >= 2:
        r(f"  * «Сырые» соединения работали без замираний, а ccxt у скрипта замирал {via_freezes} раз — похоже на проблему стека ccxt.")
    elif via_freezes == 1:
        r("  * У ccxt скрипта было одно замирание при чистых «сырых» соединениях — единичный случай, для вывода мало данных.")
    elif bot is None:
        r("  * Ни один канал скрипта не замирал. Главный тест — запустить с --with-bot, чтобы сравнить с роботом в те же секунды.")


# ------------------------- запуск -------------------------


async def main(args: argparse.Namespace) -> int:
    global _BOT
    started_wall = datetime.now()
    files = not args.no_files
    out_dir = Path(args.out_dir)
    if files:
        out_dir.mkdir(parents=True, exist_ok=True)
    base = f"ws-diag-{started_wall:%Y%m%d-%H%M%S}"
    out = Output(out_dir / f"{base}.log" if files else None, out_dir / f"{base}-summary.txt" if files else None)
    bot_log_path = out_dir / f"{base}-bot.log" if files and args.with_bot else None

    try:
        import ccxt

        from futures_arb_bot.infrastructure import config as config_loader

        ccxt_version = ccxt.__version__
    except Exception as ex:
        print(f"Не удалось импортировать робота/ccxt ({ex}). Запускайте из каталога проекта: uv run python scripts/ws_diag.py", file=sys.stderr)
        return 2

    provider = config_loader.load(args.config_dir)
    options = provider.current
    md = options.general.market_data
    out.line(
        f"Диагностика WebSocket: {args.minutes:g} мин, Python {sys.version.split()[0]}, aiohttp {aiohttp.__version__}, ccxt {ccxt_version}; "
        f"время местное ({wall():%z}); робот: {'запускается (DryRun)' if args.with_bot else 'не запускается'}"
        + (f"; прокси {args.proxy}" if args.proxy else "")
    )
    out.line(
        f"настройки робота: keep_alive_ms {md.keep_alive_ms}, reconnect_after_ms {md.reconnect_after_ms}, stale_after_ms {md.stale_after_ms}, "
        f"scan_interval_ms {md.scan_interval_ms}"
    )
    if files:
        out.line(f"журнал: {out.main_path}; сводка: {out.summary_path}" + (f"; вывод робота: {bot_log_path}" if bot_log_path else ""))

    names = {(e.name if e.name.strip() else e.id).lower(): e.id.lower() for e in options.exchanges.items}
    bot: BotProcess | None = None
    stop = asyncio.Event()
    channels: list[ChannelStats] = []
    rests: list[RestStats] = []
    lag = LagStats("скрипт")
    tasks: list[asyncio.Task[Any]] = []
    plans: list[ExchangePlan] = []
    started = time.monotonic()
    args.interrupted = False

    try:
        if args.with_bot:
            bot = _BOT = BotProcess(args, out, bot_log_path, names)
            await bot.start()

        out.line("выбираю биржи и пары так же, как робот (загрузка рынков и тикеров по REST)…")
        plans = await build_plan(args, provider, out)
        if not plans:
            out.line("нет ни одной биржи с парами — проверять нечего")
            return 2
        for plan in plans:
            preview = ", ".join(s.split("/")[0] for s in plan.symbols[:12]) + ("…" if len(plan.symbols) > 12 else "")
            out.line(f"[{plan.exchange}] пар {len(plan.symbols)}: {preview}")

        tasks.append(asyncio.create_task(loop_lag(lag, args.lag_ms, out, stop)))
        for plan in plans:
            url = RAW_WS.get(plan.exchange)
            if url is not None and plan.raw_ids:
                raw = ChannelStats(f"{plan.exchange} raw", plan.exchange, "raw", len(plan.raw_ids))
                channels.append(raw)
                tasks.append(asyncio.create_task(raw_channel(plan, url, raw, args, out, stop)))
            else:
                out.line(f"[{plan.exchange}] «сырой» канал для этой биржи не реализован — только ccxt")
            if not args.no_ccxt:
                via = ChannelStats(f"{plan.exchange} ccxt", plan.exchange, "ccxt", len(plan.symbols))
                channels.append(via)
                tasks.append(asyncio.create_task(ccxt_channel(plan, via, md, options.general.refresh_interval_ms, args, out, stop)))
            if not args.no_rest and plan.exchange in REST_PROBES:
                rest = RestStats(f"{plan.exchange} REST")
                rests.append(rest)
                tasks.append(asyncio.create_task(rest_probe(REST_PROBES[plan.exchange], rest, args, out, stop)))

        started = time.monotonic()
        deadline = started + args.minutes * 60
        next_progress = started + PROGRESS_EVERY_SECONDS
        while (now := time.monotonic()) < deadline:
            if bot is not None and not bot.running:
                out.line(f"[бот] робот завершился сам (код {bot.proc.returncode if bot.proc else '?'}) — см. {bot_log_path or 'его вывод'}")
                break
            if now >= next_progress:
                next_progress += PROGRESS_EVERY_SECONDS
                parts = [f"{c.name}: {len(c.freezes)}" for c in channels]
                if bot is not None:
                    parts += [f"бот {e.exchange}: {len(e.freezes)}" for e in bot.exchanges.values()] or ["бот: 0"]
                out.line(f"прошло {(now - started) / 60:.0f} мин из {args.minutes:g}; замираний — " + ", ".join(parts))
            await asyncio.sleep(min(1.0, deadline - now))
    except asyncio.CancelledError:
        args.interrupted = True
        task = asyncio.current_task()
        if task is not None:
            while task.uncancel() > 0:
                pass
        out.line("остановлено (Ctrl+C) — завершаю и пишу сводку")
    finally:
        seconds = time.monotonic() - started
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for c in channels:
            c.close_open_freeze()
        for plan in plans:
            with contextlib.suppress(Exception):
                await plan.connector.aclose()
        if bot is not None:
            await bot.stop()
        if channels or bot is not None:
            summary(channels, rests, lag, bot, seconds, args, out)
        if files:
            out.report("")
            out.report(f"Файлы: {out.main_path}, {out.summary_path}" + (f", {bot_log_path}" if bot_log_path else ""))
        out.close()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Диагностика WebSocket-потоков: сеть сервера, ccxt или нагрузка внутри робота.")
    parser.add_argument("--minutes", type=float, default=30.0, help="длительность проверки, мин (по умолчанию 30)")
    parser.add_argument(
        "--with-bot", action="store_true", help="параллельно запустить робота в DryRun и сопоставить его замирания с каналами скрипта"
    )
    parser.add_argument("--bot-args", default="", help='доп. аргументы робота, например "--config-dir ./config" (--no-ui добавляется сам)')
    parser.add_argument("--symbols", default="auto", help="auto — как у робота (общие пары с оборотом); или базовые монеты через запятую: BTC,ETH")
    parser.add_argument("--exchanges", default="", help="биржи через запятую (по умолчанию — включённые в exchanges.yaml)")
    parser.add_argument("--config-dir", default=None, help="каталог конфигурации робота (по умолчанию ./config)")
    parser.add_argument("--freeze", type=float, default=3.0, help="тишина дольше этого — замирание, с (как stale_after_ms у робота)")
    parser.add_argument("--dead", type=float, default=30.0, help="«сырое» соединение молчит дольше — переподключить, с")
    parser.add_argument("--ping", type=float, default=15.0, help="период ping на «сырых» соединениях, с")
    parser.add_argument("--lag-ms", type=float, default=200.0, help="писать каждую задержку цикла событий больше этого, мс")
    parser.add_argument("--rest-interval", type=float, default=5.0, help="период REST-пробы, с")
    parser.add_argument("--no-ccxt", action="store_true", help="не открывать подписки кодом робота (ccxt) в самом скрипте")
    parser.add_argument("--no-rest", action="store_true", help="без REST-пробы")
    parser.add_argument("--proxy", default=None, help="HTTP-прокси для соединений скрипта (если робот работает через прокси)")
    parser.add_argument("--trust-env", action="store_true", help="брать прокси из переменных окружения HTTP(S)_PROXY")
    parser.add_argument("--out-dir", default="logs", help="куда писать файлы журнала и сводки (по умолчанию logs)")
    parser.add_argument("--no-files", action="store_true", help="не писать файлы, только консоль")
    parser.add_argument("--bot-cmd", default=None, help=argparse.SUPPRESS)  # отладка скрипта: своя команда вместо робота
    return parser.parse_args(argv)


def entry() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--bot-child":
        return run_bot_child(float(sys.argv[2]), sys.argv[3:])
    args = parse_args()
    try:
        return asyncio.run(main(args))
    except KeyboardInterrupt:
        if _BOT is not None:
            _BOT.kill()  # повторный Ctrl+C: робот не должен остаться висеть
        return 130


if __name__ == "__main__":
    sys.exit(entry())
