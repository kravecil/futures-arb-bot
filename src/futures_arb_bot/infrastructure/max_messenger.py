"""
Транспорт уведомлений: официальный MAX Bot API v2 (по умолчанию https://platform-api2.max.ru).

Авторизация — заголовок ``Authorization: <токен>`` без префикса Bearer. Отправка —
``POST /messages?chat_id=…`` с телом ``{text, format?, notify?}``; запасной путь
``POST /chats/{chat_id}/message`` выбирается настройкой endpoint. Повторы (429/5xx/таймаут)
выполняются здесь же с экспоненциальной задержкой и уважением Retry-After; ошибки
400/401/403/404 считаются постоянными и не повторяются.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import aiohttp

from futures_arb_bot.core.abstractions import ConfigProvider
from futures_arb_bot.core.cancellation import CancellationToken, OperationCanceledError, delay
from futures_arb_bot.core.clock import Clock
from futures_arb_bot.core.domain.enums import MaxSendEndpoint, NotificationOutcome
from futures_arb_bot.core.domain.notifications import NotificationDelivery
from futures_arb_bot.core.domain.options import NotificationOptions
from futures_arb_bot.core.engine.logging_bridge import get_logger
from futures_arb_bot.core.errors import message_of

MAX_RETRY_AFTER = timedelta(seconds=30)
"""Больше этого значения пауза по Retry-After не ждёт — очередь не должна вставать надолго."""


@dataclass(frozen=True, slots=True)
class MaxBotInfo:
    name: str | None
    username: str | None
    user_id: str | None


@dataclass(frozen=True, slots=True)
class MaxVerifyResult:
    success: bool
    detail: str
    bot: MaxBotInfo | None = None


@dataclass(frozen=True, slots=True)
class MaxChatHit:
    """Диалог, замеченный в апдейтах MAX (для поиска chat_id администратора)."""

    chat_id: str
    update_type: str | None
    text: str | None


@dataclass(frozen=True, slots=True)
class MaxUpdatesPage:
    marker: str | None
    chats: list[MaxChatHit]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Ответ HTTP в виде, удобном для разбора (и для подмены в тестах)."""

    status: int
    body: str
    headers: dict[str, str]


HttpSender = Callable[[str, str, dict[str, str], bytes | None, float], Awaitable[HttpResponse]]
"""(метод, url, заголовки, тело, таймаут в секундах) → ответ."""


class _JsonNumber(str):
    """Число из JSON в исходной записи (как JsonElement.ToString() для чисел)."""


def _loads(body: str) -> Any:
    return json.loads(body, parse_int=_JsonNumber, parse_float=_JsonNumber)


class MaxApiError(Exception):
    """Неуспешный ответ MAX Bot API (используется режимом --notify-chat-id)."""


class MaxMessengerClient:
    def __init__(self, config: ConfigProvider, clock: Clock, sender: HttpSender | None = None) -> None:
        self._config = config
        self._clock = clock
        self._logger = get_logger("MaxMessengerClient")
        self._sender = sender
        self._session: aiohttp.ClientSession | None = None

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------- публичные операции -------------------------

    async def send(self, text: str, ct: CancellationToken | None = None) -> NotificationDelivery:
        ct = ct or CancellationToken.none()
        options = self._config.current.notifications
        if not options.bot_token.strip():
            return NotificationDelivery.permanent("Notifications:BotToken не задан")

        if not options.admin_chat_id.strip():
            return NotificationDelivery.permanent("Notifications:AdminChatId не задан (запустите --notify-chat-id)")

        last = NotificationDelivery.transient("отправка не выполнялась")
        attempts = min(max(options.max_retries, 0), 10) + 1

        for attempt in range(1, attempts + 1):
            ct.throw_if_cancellation_requested()
            delivery, retry_after = await self._try_send(options, text, ct)
            last = delivery

            if last.is_delivered or last.outcome == NotificationOutcome.PERMANENT:
                return last

            self._logger.warning(f"MAX Messenger: попытка {attempt}/{attempts} не удалась: {last.error}")
            if attempt == attempts:
                break

            await self._delay(retry_after if retry_after is not None else _backoff(attempt), ct)

        return last

    async def verify(self, ct: CancellationToken | None = None) -> MaxVerifyResult:
        """Проверка токена (GET /me) — используется режимами --notify-*."""
        ct = ct or CancellationToken.none()
        options = self._config.current.notifications
        if not options.bot_token.strip():
            return MaxVerifyResult(False, "Notifications:BotToken не задан")

        try:
            response = await self._request("GET", _build_url(options, "me"), options.bot_token, None, max(options.timeout_ms, 5_000), ct)
            if not 200 <= response.status < 300:
                code, message = _parse_error(response.body)
                return MaxVerifyResult(False, _join_status(response.status, code, message))

            bot = _parse_bot(response.body)
            return MaxVerifyResult(True, f"токен принят, бот «{bot.name or bot.username or '?'}»", bot)
        except TimeoutError:
            return MaxVerifyResult(False, f"таймаут {options.timeout_ms} мс")
        except Exception as ex:
            return MaxVerifyResult(False, message_of(ex))

    async def poll_updates(self, marker: str | None, ct: CancellationToken | None = None) -> MaxUpdatesPage:
        """
        Одна длинная подписка GET /updates: вернуть замеченные chat_id. Так бот узнаёт адресата:
        первым пишет администратор, робот его только читает. Таймаут — OperationCanceledError.
        """
        ct = ct or CancellationToken.none()
        options = self._config.current.notifications
        query = f"updates?limit=50&timeout={min(max(options.timeout_ms, 500), 60_000)}"
        if marker and marker.strip():
            query += f"&marker={quote(marker, safe='')}"

        try:
            response = await self._request("GET", _build_url(options, query), options.bot_token, None, options.timeout_ms + 5_000, ct)
        except TimeoutError as ex:
            raise OperationCanceledError("A task was canceled.") from ex

        if not 200 <= response.status < 300:
            code, message = _parse_error(response.body)
            raise MaxApiError(f"updates: {_join_status(response.status, code, message)}")

        return _parse_updates(response.body)

    # ------------------------- HTTP -------------------------

    async def _try_send(self, options: NotificationOptions, text: str, ct: CancellationToken) -> tuple[NotificationDelivery, timedelta | None]:
        chat_id = quote(options.admin_chat_id.strip(), safe="")
        if options.endpoint == MaxSendEndpoint.CHATS_MESSAGE:
            path = f"chats/{chat_id}/message"
        else:
            path = f"messages?chat_id={chat_id}&disable_link_preview=true"

        payload: dict[str, Any] = {"text": text}
        if options.use_markdown:
            payload["format"] = "markdown"
        if options.silent:
            payload["notify"] = False
        body = json.dumps(payload).encode("utf-8")

        try:
            response = await self._request("POST", _build_url(options, path), options.bot_token, body, options.timeout_ms, ct)
            if 200 <= response.status < 300:
                return NotificationDelivery.delivered(), None
            return _classify(response), self._retry_after(response)
        except OperationCanceledError:
            raise  # остановка процесса — не наша ошибка
        except TimeoutError:
            return NotificationDelivery.transient(f"таймаут {options.timeout_ms} мс"), None
        except Exception as ex:
            return NotificationDelivery.transient(f"сеть: {message_of(ex)}"), None

    async def _request(self, method: str, url: str, token: str, body: bytes | None, timeout_ms: int, ct: CancellationToken) -> HttpResponse:
        """Запрос с таймаутом (500 мс…120 с) и прерыванием по токену отмены."""
        timeout = min(max(timeout_ms, 500), 120_000) / 1000
        headers = {"Authorization": token.strip()}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        sender = self._sender or self._send_http
        return await _race(asyncio.wait_for(sender(method, url, headers, body, timeout), timeout), ct)

    async def _send_http(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> HttpResponse:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ttl_dns_cache=600))
        client_timeout = aiohttp.ClientTimeout(total=timeout, sock_connect=5)
        async with self._session.request(method, url, headers=headers, data=body, timeout=client_timeout) as response:
            try:
                text = await response.text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""  # тело необязательно: статус важнее
            return HttpResponse(response.status, text, {k: v for k, v in response.headers.items()})

    def _retry_after(self, response: HttpResponse) -> timedelta | None:
        """Заголовок Retry-After (секунды или дата) — сколько ждать перед повтором."""
        hint = next((v for k, v in response.headers.items() if k.lower() == "retry-after"), None)
        if hint is None:
            return None

        hint = hint.strip()
        delay_value: timedelta | None
        if hint.isdigit():
            delay_value = timedelta(seconds=int(hint))
        else:
            try:
                moment = parsedate_to_datetime(hint)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=UTC)
                delay_value = moment - self._clock.now()
            except TypeError, ValueError:
                delay_value = None

        if delay_value is None:
            return None
        return min(max(delay_value, timedelta(0)), MAX_RETRY_AFTER)

    async def _delay(self, pause: timedelta, ct: CancellationToken) -> None:
        try:
            await delay(pause.total_seconds(), ct)
        except OperationCanceledError:
            pass  # процесс останавливается — пауза прервана, это штатно


# ------------------------- разбор ответов -------------------------


async def _race[T](awaitable: Awaitable[T], ct: CancellationToken) -> T:
    """Выполнить операцию, но прервать её, если сработал токен отмены (как связанный CancellationToken)."""
    task = asyncio.ensure_future(awaitable)
    if ct is CancellationToken.none():
        return await task
    ct.throw_if_cancellation_requested()
    waiter = asyncio.ensure_future(ct.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    if task in done:
        return task.result()
    task.cancel()
    try:
        await task
    except BaseException:
        pass
    raise OperationCanceledError


def _build_url(options: NotificationOptions, path: str) -> str:
    """Абсолютный URL: базовый адрес без завершающего слэша + относительный путь."""
    return options.api_base_url.strip().rstrip("/") + "/" + path


def _join_status(status: int, code: str | None, message: str | None) -> str:
    return f"{status} {code or ''} {message or ''}".strip()


def _classify(response: HttpResponse) -> NotificationDelivery:
    """Постоянные ошибки (4xx кроме 429) не повторяем, временные — да."""
    code, message = _parse_error(response.body)
    status = response.status
    description = f"{status} {code or ''} {message or ''}".strip()

    if status in (400, 401, 403, 404):
        return NotificationDelivery.permanent(description)
    if status == 429:
        return NotificationDelivery.transient(f"лимит запросов: {description}")
    if status >= 500:
        return NotificationDelivery.transient(description)
    return NotificationDelivery.permanent(description)


def _truncate(text: str) -> str:
    return text[:300] + "…" if len(text) > 300 else text


def _read_string(element: Any, prop: str) -> str | None:
    if not isinstance(element, dict) or prop not in element:
        return None
    value = element[prop]
    if isinstance(value, str) and value.strip():
        return str(value)
    return None


def _parse_error(body: str) -> tuple[str | None, str | None]:
    """Код и текст ошибки MAX: поля code/message лежат в корне или в meta."""
    if not body.strip():
        return None, None
    try:
        root = _loads(body)
    except ValueError:
        return None, _truncate(body)

    source = root["meta"] if isinstance(root, dict) and isinstance(root.get("meta"), dict) else root
    preview = _truncate(body)
    message = _read_string(source, "message") or _read_string(source, "description") or preview
    return _read_string(source, "code"), message


def _parse_bot(body: str) -> MaxBotInfo:
    try:
        root = _loads(body)
    except ValueError:
        return MaxBotInfo(None, None, None)
    if isinstance(root, dict) and isinstance(root.get("bot"), dict):
        root = root["bot"]
    return MaxBotInfo(_read_string(root, "name"), _read_string(root, "username"), _read_string(root, "user_id"))


def _parse_updates(body: str) -> MaxUpdatesPage:
    """Разбор GET /updates: собираем любые chat_id рекурсивно — форма апдейта различается по типам."""
    hits: list[MaxChatHit] = []
    try:
        root = _loads(body)
    except ValueError:
        return MaxUpdatesPage(None, hits)

    marker = _read_string(root, "marker")
    updates = root.get("updates") if isinstance(root, dict) else None
    if not isinstance(updates, list):
        return MaxUpdatesPage(marker, hits)

    for update in updates:
        update_type = _read_string(update, "update_type")
        text = _find_string(update, "text")
        for chat_id in _find_chat_ids(update):
            hits.append(MaxChatHit(chat_id, update_type, text))
    return MaxUpdatesPage(marker, hits)


def _find_string(element: Any, prop: str) -> str | None:
    """Первое попавшееся строковое (не числовое) поле с указанным именем."""
    if isinstance(element, dict):
        for name, value in element.items():
            if name == prop and isinstance(value, str) and not isinstance(value, _JsonNumber) and value.strip():
                return str(value)
        for value in element.values():
            found = _find_string(value, prop)
            if found is not None:
                return found
    elif isinstance(element, list):
        for item in element:
            found = _find_string(item, prop)
            if found is not None:
                return found
    return None


def _find_chat_ids(element: Any) -> list[str]:
    ids: list[str] = []
    _collect_chat_ids(element, ids)
    return list(dict.fromkeys(ids))


def _collect_chat_ids(element: Any, sink: list[str]) -> None:
    if isinstance(element, dict):
        for name, value in element.items():
            if name == "chat_id" and isinstance(value, str):
                found = _read_string(element, "chat_id")
                if found is not None:
                    sink.append(found)
            _collect_chat_ids(value, sink)
    elif isinstance(element, list):
        for item in element:
            _collect_chat_ids(item, sink)


def _backoff(attempt: int) -> timedelta:
    """Экспоненциальная пауза между попытками: 0,3 с → 0,6 с → 1,2 с … не дольше 5 с."""
    return timedelta(milliseconds=min(5_000, 300 * 2 ** max(0, attempt - 1)))


__all__ = [
    "HttpResponse",
    "MaxApiError",
    "MaxBotInfo",
    "MaxChatHit",
    "MaxMessengerClient",
    "MaxUpdatesPage",
    "MaxVerifyResult",
]
