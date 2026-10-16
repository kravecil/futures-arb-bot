"""
Заплатка ccxt.pro: WebSocket-соединение перестаёт читать сокет при потоке сообщений пачками.

Причина. ``Client.receive_loop`` в ccxt (проверено на 4.5.85) ради скорости выгребает сообщения
прямо из внутренней очереди aiohttp (``_payload._buffer``) и сам поправляет её счётчик размера,
прибавляя последнему сообщению размеры выгребенных. С aiohttp 3.14 очередь учитывает каждое
сообщение с надбавкой ``MSG_SIZE_OVERHEAD`` (128 байт), а ccxt её не вычитает: на каждом
сообщении, пришедшем в пачке, счётчик «утекает» на 128 байт. Когда он дорастает до лимита
очереди, aiohttp ставит чтение сокета на паузу и уже не снимает — очередь пуста, а счётчик
остаётся выше порога. Соединение не закрыто, биржа шлёт данные, но их никто не читает:
у робота поток котировок «молчит» каждые 10–30 с (тем чаще, чем больше пар), а ccxt замечает это
только по пропущенному ping-pong.

Исправление. Чтение идёт только через публичный ``connection.receive()`` — по одному сообщению,
без доступа к внутренностям aiohttp; учёт размера очереди целиком остаётся за aiohttp. Остальное
поведение — как в оригинале: обработка сообщения, перезапуск цикла, закрытие и ошибки чтения.

Заплатка ставится один раз при импорте фабрики коннекторов (до создания бирж). Когда ccxt исправит
receive_loop, её можно убрать: проверка — tests/test_ccxt_ws_patch.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ccxt.async_support.base.ws.client import Client
from ccxt.base.errors import NetworkError

_original_receive_loop = Client.receive_loop
_applied = False


def _receive_loop(self: Any) -> None:
    if self.closed():
        # соединение оборвалось между подключением и первым чтением
        self.on_close(1006)
        return

    task = self.asyncio_loop.create_task(self.receive())

    def after_receive(resolved: asyncio.Future[Any]) -> None:
        if resolved.cancelled():
            return  # чтение отменено (остановка или переподключение)
        exception = resolved.exception()
        if exception is None:
            self.handle_message(resolved.result())
            self.asyncio_loop.call_soon(self.receive_loop)
        else:
            self.reject(NetworkError(str(exception)))

    task.add_done_callback(after_receive)


def apply() -> bool:
    """Поставить заплатку (повторный вызов ничего не делает). Возвращает True, если она действует."""
    global _applied
    if not _applied:
        Client.receive_loop = _receive_loop
        _applied = True
    return Client.receive_loop is _receive_loop


def is_applied() -> bool:
    return Client.receive_loop is _receive_loop


def original_receive_loop() -> Any:
    """Исходная реализация ccxt — для теста, который проверяет, нужна ли ещё заплатка."""
    return _original_receive_loop
