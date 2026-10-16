"""
Заплатка чтения WebSocket ccxt (infrastructure/ccxt_ws_patch.py).

Локальный сервер шлёт сообщения пачками — как биржа при десятках пар в подписке. Исходный
receive_loop ccxt на aiohttp 3.14 «утекает» в счётчике очереди и навсегда ставит чтение на паузу;
с заплаткой клиент получает всё.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from ccxt.async_support.base.ws.client import Client

from futures_arb_bot.infrastructure import ccxt_ws_patch

TOTAL = 8_000
BURST = 50
MESSAGE = json.dumps({"arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"}, "data": [{"bids": [["1", "1"]], "asks": [["1", "1"]]}]})


@pytest.fixture
async def server_url() -> AsyncIterator[str]:
    async def handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(compress=False)
        await ws.prepare(request)
        for i in range(TOTAL):
            await ws.send_str(MESSAGE)
            if i % BURST == BURST - 1:
                await asyncio.sleep(0.001)  # пауза между пачками: внутри пачки сообщения идут подряд
        async for _ in ws:  # держим соединение открытым, пока клиент не закроет
            pass
        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app, shutdown_timeout=0.2)  # сервер, упёршийся в паузу клиента, не держит тест
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"ws://127.0.0.1:{port}/"
    finally:
        await runner.cleanup()


async def receive_all(url: str, receive_loop: Callable[..., Any]) -> int:
    """Сколько сообщений получит клиент ccxt с данной реализацией receive_loop (ждём, пока поток идёт)."""
    received = 0

    def on_message(client: Any, message: Any) -> None:
        nonlocal received
        received += 1

    saved = Client.receive_loop
    Client.receive_loop = receive_loop
    session = aiohttp.ClientSession()
    client = Client(
        url,
        on_message,
        lambda c, e: None,
        lambda c, code: None,
        lambda c: None,
        {"asyncio_loop": asyncio.get_running_loop(), "keepAlive": 0, "options": {"compress": 0}},
    )
    try:
        await client.connect(session)
        last, last_change = -1, time.monotonic()
        while received < TOTAL and time.monotonic() - last_change < 1.0:
            await asyncio.sleep(0.05)
            if received != last:
                last, last_change = received, time.monotonic()
        return received
    finally:
        Client.receive_loop = saved
        # «уснувший» клиент не прочитает ответный close-кадр сервера: не ждём 10 с тайм-аута aiohttp
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(client.close(), 0.5)
        await session.close()


def test_factory_import_applies_patch():
    import futures_arb_bot.infrastructure.ccxt_factory  # noqa: F401

    assert ccxt_ws_patch.is_applied()
    assert ccxt_ws_patch.apply()  # повторный вызов безопасен


async def test_patched_client_reads_bursts_without_stalling(server_url: str):
    assert await receive_all(server_url, ccxt_ws_patch._receive_loop) == TOTAL


async def test_original_ccxt_receive_loop_still_stalls(server_url: str):
    """
    Документирует ошибку ccxt. Если тест упал — исходный receive_loop больше не «засыпает»
    (ccxt исправили или сменилась aiohttp): заплатку ccxt_ws_patch можно убрать.
    """
    received = await receive_all(server_url, ccxt_ws_patch.original_receive_loop())
    assert received < TOTAL, "ccxt больше не теряет чтение сокета — заплатку можно удалить"
