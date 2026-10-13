"""
Диагностика WebSocket-потоков котировок на сервере — отдельно от робота.

Одновременно открывает к Bybit и OKX:
  * «сырые» WebSocket-соединения (aiohttp, без ccxt) на лучшие цены выбранных пар;
  * те же подписки через ccxt.pro (как у робота), если не указан --no-ccxt;
  * REST-пробу (запрос времени биржи раз в --rest-interval с);
  * замер задержек цикла событий asyncio.

Считает замирания (данные не приходят дольше --freeze с), «мёртвые» соединения (молчание дольше
--dead с — соединение закрывается и открывается заново), время ping-pong, объём данных до первого
замирания. В конце печатает сводку и вывод: виновата сеть сервера или стек робота.

Запуск на сервере из каталога проекта (10 минут, вывод заодно в файл):
    uv run python scripts/ws_diag.py --minutes 10 | tee ws-diag.log
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiohttp

DEFAULT_BASES = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX", "SUI", "LTC"]

BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"
OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"
REST_PROBES = {
    "bybit": "https://api.bybit.com/v5/market/time",
    "okx": "https://www.okx.com/api/v5/public/time",
}


def now_text() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]


class Output:
    def __init__(self, path: str | None) -> None:
        self._file = open(path, "a", encoding="utf-8") if path else None  # noqa: SIM115

    def line(self, text: str) -> None:
        stamped = f"{now_text()}  {text}"
        print(stamped, flush=True)
        if self._file is not None:
            self._file.write(stamped + "\n")
            self._file.flush()

    def raw(self, text: str) -> None:
        print(text, flush=True)
        if self._file is not None:
            self._file.write(text + "\n")
            self._file.flush()


@dataclass
class ChannelStats:
    """Статистика одного канала (например, «bybit raw» или «okx ccxt»)."""

    name: str
    connects: int = 0
    messages: int = 0
    bytes: int = 0
    freezes: list[float] = field(default_factory=list)
    """Длительности завершившихся замираний, с."""
    first_freeze_after: list[float] = field(default_factory=list)
    """Через сколько секунд после подключения началось первое замирание."""
    bytes_before_first_freeze: list[int] = field(default_factory=list)
    dead_reconnects: int = 0
    errors: list[str] = field(default_factory=list)
    ping_rtts: list[float] = field(default_factory=list)
    alive_seconds: float = 0.0
    total_seconds: float = 0.0

    # состояние текущего соединения
    connected_at: float | None = None
    last_message_at: float | None = None
    bytes_since_connect: int = 0
    freeze_started_at: float | None = None
    froze_this_connection: bool = False

    def on_connect(self) -> None:
        now = time.monotonic()
        self.connects += 1
        self.connected_at = now
        self.last_message_at = now
        self.bytes_since_connect = 0
        self.freeze_started_at = None
        self.froze_this_connection = False

    def on_message(self, size: int) -> float | None:
        """Учитывает сообщение; если оно прервало замирание — возвращает его длительность."""
        now = time.monotonic()
        self.messages += 1
        self.bytes += size
        self.bytes_since_connect += size
        self.last_message_at = now
        if self.freeze_started_at is not None:
            duration = now - self.freeze_started_at
            self.freezes.append(duration)
            self.freeze_started_at = None
            return duration
        return None

    def check_freeze(self, freeze_after: float) -> float | None:
        """Начало замирания: возвращает, сколько секунд прошло с подключения (иначе None)."""
        if self.last_message_at is None or self.connected_at is None or self.freeze_started_at is not None:
            return None
        now = time.monotonic()
        if now - self.last_message_at < freeze_after:
            return None
        self.freeze_started_at = self.last_message_at
        if not self.froze_this_connection:
            self.froze_this_connection = True
            self.first_freeze_after.append(self.last_message_at - self.connected_at)
            self.bytes_before_first_freeze.append(self.bytes_since_connect)
            return self.last_message_at - self.connected_at
        return self.last_message_at - self.connected_at

    def silent_for(self) -> float:
        return 0.0 if self.last_message_at is None else time.monotonic() - self.last_message_at

    def close_open_freeze(self) -> None:
        if self.freeze_started_at is not None:
            self.freezes.append(time.monotonic() - self.freeze_started_at)
            self.freeze_started_at = None


# ------------------------- «сырые» WebSocket -------------------------


def bybit_subscribe(bases: list[str]) -> list[str]:
    topics = [f"orderbook.1.{b}USDT" for b in bases]
    return [json.dumps({"op": "subscribe", "args": topics[i : i + 10]}) for i in range(0, len(topics), 10)]


def okx_subscribe(bases: list[str]) -> list[str]:
    args = [{"channel": "bbo-tbt", "instId": f"{b}-USDT-SWAP"} for b in bases]
    return [json.dumps({"op": "subscribe", "args": args})]


async def raw_channel(
    exchange: str, url: str, bases: list[str], stats: ChannelStats, args: argparse.Namespace, out: Output, stop: asyncio.Event
) -> None:
    subscribe = bybit_subscribe(bases) if exchange == "bybit" else okx_subscribe(bases)
    ping_text = json.dumps({"op": "ping"}) if exchange == "bybit" else "ping"
    attempt = 0
    async with aiohttp.ClientSession(trust_env=args.trust_env) as session:
        while not stop.is_set():
            try:
                async with session.ws_connect(url, proxy=args.proxy, heartbeat=None, timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                    attempt = 0
                    stats.on_connect()
                    out.line(f"[{stats.name}] подключено (#{stats.connects})")
                    for message in subscribe:
                        await ws.send_str(message)
                    ping_sent: list[float] = []
                    closed_by_us: list[bool] = []

                    async def pinger(ws: aiohttp.ClientWebSocketResponse = ws, sent: list[float] = ping_sent) -> None:
                        while not ws.closed:
                            await asyncio.sleep(args.ping)
                            sent.append(time.monotonic())
                            with contextlib.suppress(Exception):
                                await ws.send_str(ping_text)

                    async def watchdog(ws: aiohttp.ClientWebSocketResponse = ws, ours: list[bool] = closed_by_us) -> None:
                        while not ws.closed:
                            await asyncio.sleep(0.25)
                            since_connect = stats.check_freeze(args.freeze)
                            if since_connect is not None:
                                out.line(
                                    f"[{stats.name}] ЗАМИРАНИЕ: данных нет {args.freeze:.0f}+ с "
                                    f"(через {since_connect:.1f} с после подключения, получено {stats.bytes_since_connect / 1024:.1f} КБ)"
                                )
                            if stats.silent_for() >= args.dead:
                                stats.dead_reconnects += 1
                                out.line(f"[{stats.name}] соединение мёртвое ({args.dead:.0f} с без данных) — закрываю и переподключаюсь")
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
                                if "pong" in data and ping_sent:
                                    stats.ping_rtts.append((time.monotonic() - ping_sent.pop(0)) * 1000)
                                resumed = stats.on_message(len(data))
                                if resumed is not None:
                                    out.line(f"[{stats.name}] данные пошли снова после {resumed:.1f} с тишины")
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                stats.errors.append(f"ws error: {ws.exception()}")
                                break
                        if not stop.is_set() and not closed_by_us:
                            code = ws.close_code
                            out.line(f"[{stats.name}] соединение закрыто (код {code})")
                            stats.errors.append(f"закрыто, код {code}")
                    finally:
                        for task in tasks:
                            task.cancel()
                        stats.close_open_freeze()
                        if stats.connected_at is not None:
                            stats.alive_seconds += time.monotonic() - stats.connected_at
                            stats.connected_at = None
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                stats.errors.append(f"{type(ex).__name__}: {ex}")
                out.line(f"[{stats.name}] ошибка подключения: {type(ex).__name__}: {ex}")
            if stop.is_set():
                break
            attempt += 1
            await asyncio.sleep(min(10, attempt))


# ------------------------- те же подписки через ccxt.pro -------------------------


async def ccxt_channel(exchange: str, bases: list[str], stats: ChannelStats, args: argparse.Namespace, out: Output, stop: asyncio.Event) -> None:
    try:
        import ccxt.pro as ccxtpro
    except Exception as ex:
        out.line(f"[{stats.name}] ccxt.pro недоступен: {ex}")
        return

    options = {"defaultType": "swap"}
    if exchange == "bybit":
        options["defaultSubType"] = "linear"
    config: dict[str, Any] = {"options": options}
    if args.proxy:
        config["wsProxy"] = args.proxy
    api = getattr(ccxtpro, exchange)(config)
    symbols = [f"{b}/USDT:USDT" for b in bases]
    try:
        await api.load_markets()
        stats.on_connect()
        out.line(f"[{stats.name}] подписка через ccxt (keepAlive {api.streaming.get('keepAlive')} мс)")

        async def watchdog() -> None:
            while not stop.is_set():
                await asyncio.sleep(0.25)
                since_connect = stats.check_freeze(args.freeze)
                if since_connect is not None:
                    out.line(f"[{stats.name}] ЗАМИРАНИЕ: данных нет {args.freeze:.0f}+ с (через {since_connect:.1f} с после (пере)подключения)")

        guard = asyncio.create_task(watchdog())
        try:
            while not stop.is_set():
                try:
                    updates = await api.watch_bids_asks(symbols)
                    resumed = stats.on_message(len(updates) if isinstance(updates, dict) else 1)
                    if resumed is not None:
                        out.line(f"[{stats.name}] данные пошли снова после {resumed:.1f} с тишины")
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    stats.errors.append(f"{type(ex).__name__}: {ex}")
                    out.line(f"[{stats.name}] ошибка ccxt: {type(ex).__name__}: {str(ex)[:200]}")
                    stats.close_open_freeze()
                    await asyncio.sleep(1)
                    stats.on_connect()
        finally:
            guard.cancel()
            stats.close_open_freeze()
    finally:
        with contextlib.suppress(Exception):
            await api.close()


# ------------------------- REST-проба и цикл событий -------------------------


@dataclass
class RestStats:
    name: str
    latencies: list[float] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


async def rest_probe(exchange: str, url: str, stats: RestStats, args: argparse.Namespace, out: Output, stop: asyncio.Event) -> None:
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
                out.line(f"[{stats.name}] REST не ответил: {type(ex).__name__}: {ex}")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), args.rest_interval)


async def loop_lag(samples: list[float], stop: asyncio.Event) -> None:
    while not stop.is_set():
        started = time.monotonic()
        await asyncio.sleep(0.1)
        samples.append(max(0.0, (time.monotonic() - started - 0.1) * 1000))


# ------------------------- сводка -------------------------


def fmt_list(values: list[float], unit: str = "с", digits: int = 1) -> str:
    if not values:
        return "—"
    return f"медиана {statistics.median(values):.{digits}f} {unit}, макс {max(values):.{digits}f} {unit}"


def summary(channels: list[ChannelStats], rests: list[RestStats], lags: list[float], seconds: float, args: argparse.Namespace, out: Output) -> None:
    out.raw("")
    out.raw("=" * 78)
    out.raw(f"СВОДКА за {seconds / 60:.1f} мин (замирание — нет данных {args.freeze:.0f}+ с, мёртвое — {args.dead:.0f}+ с)")
    out.raw("=" * 78)
    for c in channels:
        rate = c.messages / seconds if seconds > 0 else 0
        out.raw(f"\n[{c.name}]")
        out.raw(f"  подключений: {c.connects}, сообщений: {c.messages} ({rate:.1f}/с), получено {c.bytes / 1024:.0f} КБ")
        out.raw(
            f"  замираний: {len(c.freezes)}; длительность: {fmt_list(c.freezes)}; суммарно {sum(c.freezes):.0f} с ({sum(c.freezes) / seconds * 100:.0f}% времени)"
        )
        out.raw(f"  первое замирание после подключения: {fmt_list(c.first_freeze_after)}")
        if c.bytes_before_first_freeze:
            kb = [b / 1024 for b in c.bytes_before_first_freeze]
            out.raw(f"  получено до первого замирания: {fmt_list(kb, 'КБ')}")
        if c.dead_reconnects:
            out.raw(f"  мёртвых соединений (переподключено скриптом): {c.dead_reconnects}")
        if c.ping_rtts:
            out.raw(f"  ping-pong: {fmt_list(c.ping_rtts, 'мс', 0)}")
        if c.errors:
            out.raw(f"  ошибок: {len(c.errors)}; последние: {' | '.join(c.errors[-3:])[:300]}")
    for r in rests:
        out.raw(f"\n[{r.name}]  запросов {len(r.latencies) + len(r.failures)}, ошибок {len(r.failures)}; задержка: {fmt_list(r.latencies, 'мс', 0)}")
    if lags:
        out.raw(f"\n[цикл событий] задержка: {fmt_list(lags, 'мс', 0)} (больше ~200 мс — процесс перегружен)")

    raw = [c for c in channels if c.name.endswith("raw")]
    via_ccxt = [c for c in channels if c.name.endswith("ccxt")]
    raw_freezes = sum(len(c.freezes) for c in raw)
    ccxt_freezes = sum(len(c.freezes) for c in via_ccxt)
    max_lag = max(lags) if lags else 0

    out.raw("\nВЫВОД:")
    if max_lag > 1000:
        out.raw("  * Цикл событий подвисал больше чем на 1 с — сначала разберитесь с нагрузкой на сервер (CPU/память).")
    if raw_freezes > 0:
        out.raw("  * Замирают даже «сырые» соединения без ccxt — проблема в сети сервера (провайдер, страна размещения,")
        out.raw("    VPN/прокси, фильтрация трафика). Код робота это не исправит: смените сервер/сеть или используйте REST.")
        if any(r.failures for r in rests):
            out.raw("  * REST тоже давал сбои — связь с биржами нестабильна в целом.")
        elif rests:
            out.raw("  * REST при этом отвечал — режим market_data.source: Rest будет работать надёжнее потоков.")
    elif ccxt_freezes > 0:
        out.raw("  * «Сырые» соединения работали без замираний, а через ccxt — замирали: дело в стеке ccxt/настройках,")
        out.raw("    а не в сети. Пришлите этот вывод — будем разбираться с ccxt (версия, keepAlive, подписки).")
    else:
        out.raw("  * Замираний не было ни в одном канале. Если робот при этом теряет потоки — проблема внутри робота")
        out.raw("    (нагрузка, число подписок). Повторите проверку в то же время суток, когда были сбои.")


# ------------------------- запуск -------------------------


async def main(args: argparse.Namespace) -> int:
    out = Output(args.out)
    bases = [b.strip().upper() for b in args.symbols.split(",") if b.strip()]
    exchanges = [e.strip().lower() for e in args.exchanges.split(",") if e.strip()]
    urls = {"bybit": args.bybit_url, "okx": args.okx_url}

    out.line(
        f"Диагностика WebSocket: биржи {', '.join(exchanges)}, пары {', '.join(bases)}, {args.minutes} мин; "
        f"Python {sys.version.split()[0]}, aiohttp {aiohttp.__version__}" + (f", прокси {args.proxy}" if args.proxy else "")
    )

    stop = asyncio.Event()
    channels: list[ChannelStats] = []
    rests: list[RestStats] = []
    lags: list[float] = []
    tasks: list[asyncio.Task[Any]] = [asyncio.create_task(loop_lag(lags, stop))]
    for exchange in exchanges:
        raw = ChannelStats(f"{exchange} raw")
        channels.append(raw)
        tasks.append(asyncio.create_task(raw_channel(exchange, urls[exchange], bases, raw, args, out, stop)))
        if not args.no_ccxt:
            via = ChannelStats(f"{exchange} ccxt")
            channels.append(via)
            tasks.append(asyncio.create_task(ccxt_channel(exchange, bases, via, args, out, stop)))
        if not args.no_rest:
            rest = RestStats(f"{exchange} REST")
            rests.append(rest)
            tasks.append(asyncio.create_task(rest_probe(exchange, REST_PROBES[exchange], rest, args, out, stop)))

    started = time.monotonic()
    try:
        await asyncio.wait_for(stop.wait(), args.minutes * 60)
    except TimeoutError:
        pass
    except KeyboardInterrupt, asyncio.CancelledError:
        out.line("остановлено")
    stop.set()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    seconds = time.monotonic() - started
    for c in channels:
        c.close_open_freeze()
    summary(channels, rests, lags, seconds, args, out)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Диагностика WebSocket-потоков Bybit/OKX (сырые соединения против ccxt).")
    parser.add_argument("--minutes", type=float, default=10.0, help="длительность проверки, мин (по умолчанию 10)")
    parser.add_argument("--symbols", default=",".join(DEFAULT_BASES), help="базовые монеты через запятую (USDT-перпы)")
    parser.add_argument("--exchanges", default="bybit,okx", help="биржи через запятую: bybit, okx")
    parser.add_argument("--freeze", type=float, default=3.0, help="тишина дольше этого — замирание, с")
    parser.add_argument("--dead", type=float, default=30.0, help="тишина дольше этого — соединение мёртвое, переподключиться, с")
    parser.add_argument("--ping", type=float, default=15.0, help="период ping на «сырых» соединениях, с")
    parser.add_argument("--rest-interval", type=float, default=5.0, help="период REST-пробы, с")
    parser.add_argument("--no-ccxt", action="store_true", help="не открывать параллельные подписки через ccxt")
    parser.add_argument("--no-rest", action="store_true", help="без REST-пробы")
    parser.add_argument("--proxy", default=None, help="HTTP-прокси для всех соединений (если робот работает через прокси)")
    parser.add_argument("--trust-env", action="store_true", help="брать прокси из переменных окружения HTTP(S)_PROXY")
    parser.add_argument("--out", default=None, help="дописывать вывод ещё и в этот файл")
    parser.add_argument("--bybit-url", default=BYBIT_WS, help=argparse.SUPPRESS)
    parser.add_argument("--okx-url", default=OKX_WS, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main(parse_args())))
    except KeyboardInterrupt:
        sys.exit(130)
