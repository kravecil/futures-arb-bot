"""
HTTP-слой MAX Bot API: путь и тело запроса, заголовок авторизации без Bearer, разбор ошибок
(meta.code), политика повторов и long-poll /updates.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest

from futures_arb_bot.core.domain.enums import MaxSendEndpoint, NotificationOutcome
from futures_arb_bot.core.domain.options import BotOptions, NotificationOptions
from futures_arb_bot.infrastructure.max_messenger import HttpResponse, MaxApiError, MaxMessengerClient

from .fakes import FakeConfigProvider, ManualClock

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@dataclass
class Request:
    method: str
    url: str
    headers: dict[str, str]
    body: str


@dataclass
class StubHttp:
    """Заготовленные ответы MAX Bot API: пишет входящие запросы и отдаёт что скажут."""

    respond: Callable[[Request], HttpResponse]
    requests: list[Request] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.requests)

    async def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> HttpResponse:
        request = Request(method, url, headers, body.decode("utf-8") if body else "")
        self.requests.append(request)
        return self.respond(request)


def options(tweak: Callable[[NotificationOptions], None] | None = None) -> BotOptions:
    result = BotOptions()
    result.general.write_log_file = False
    result.notifications.enabled = True
    result.notifications.bot_token = "top.secret.token"
    result.notifications.admin_chat_id = "42"
    result.notifications.max_retries = 0
    result.notifications.timeout_ms = 1_000
    if tweak is not None:
        tweak(result.notifications)
    return result


def create(opts: BotOptions, respond: Callable[[Request], HttpResponse]) -> tuple[MaxMessengerClient, StubHttp]:
    stub = StubHttp(respond)
    return MaxMessengerClient(FakeConfigProvider(opts), ManualClock(NOW), sender=stub), stub


def json_response(status: int, body: str, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status, body, headers or {"Content-Type": "application/json"})


def ok(body: str = '{"message":{"mid":"1"}}') -> HttpResponse:
    return json_response(200, body)


async def test_sends_plaintext_to_messages_endpoint_with_bare_token():
    client, stub = create(options(), lambda _: ok())

    delivery = await client.send("привет")

    assert delivery.is_delivered
    [request] = stub.requests
    url = urlsplit(request.url)
    assert request.method == "POST"
    assert url.path == "/messages"
    assert "chat_id=42" in url.query
    assert request.headers["Authorization"] == "top.secret.token"
    assert '"text":' in request.body
    assert "format" not in request.body


async def test_text_is_serialized_as_json_string_with_unicode_escapes():
    client, stub = create(options(), lambda _: ok())
    await client.send("спред +0.8%")
    assert json.loads(stub.requests[0].body)["text"] == "спред +0.8%"


async def test_markdown_and_silent_flags_reach_the_body():
    def tweak(n: NotificationOptions) -> None:
        n.use_markdown = True
        n.silent = True

    client, stub = create(options(tweak), lambda _: ok())
    await client.send("*жирный*")

    payload = json.loads(stub.requests[0].body)
    assert payload["format"] == "markdown"
    assert payload["notify"] is False


async def test_chats_message_endpoint_is_used_when_configured():
    client, stub = create(options(lambda n: setattr(n, "endpoint", MaxSendEndpoint.CHATS_MESSAGE)), lambda _: ok())
    await client.send("текст")
    assert urlsplit(stub.requests[0].url).path == "/chats/42/message"


async def test_custom_base_url_is_respected():
    client, stub = create(options(lambda n: setattr(n, "api_base_url", "https://example.test/v2/")), lambda _: ok())
    await client.send("текст")
    url = urlsplit(stub.requests[0].url)
    assert url.hostname == "example.test"
    assert url.path == "/v2/messages"


async def test_empty_token_or_chat_skips_http_with_permanent_error():
    no_token, stub_a = create(options(lambda n: setattr(n, "bot_token", " ")), lambda _: ok())
    no_chat, stub_b = create(options(lambda n: setattr(n, "admin_chat_id", "")), lambda _: ok())

    assert (await no_token.send("текст")).outcome == NotificationOutcome.PERMANENT
    assert (await no_chat.send("текст")).outcome == NotificationOutcome.PERMANENT
    assert stub_a.call_count == 0
    assert stub_b.call_count == 0


async def test_unauthorized_is_permanent_and_not_retried():
    client, stub = create(
        options(lambda n: setattr(n, "max_retries", 3)),
        lambda _: json_response(401, '{"meta":{"code":"verify.token","message":"Invalid access_token"}}'),
    )

    delivery = await client.send("текст")

    assert delivery.outcome == NotificationOutcome.PERMANENT
    assert "verify.token" in (delivery.error or "")
    assert stub.call_count == 1


async def test_rate_limit_is_retried_and_respects_retry_after():
    client, stub = create(
        options(lambda n: setattr(n, "max_retries", 2)),
        lambda _: json_response(429, '{"code":"common.rateLimited","message":"too many"}', {"Retry-After": "0"}),
    )

    delivery = await client.send("текст")

    assert delivery.outcome == NotificationOutcome.TRANSIENT
    assert "лимит запросов" in (delivery.error or "")
    assert stub.call_count == 3


async def test_server_error_is_retried_then_reported_as_transient():
    client, stub = create(options(lambda n: setattr(n, "max_retries", 1)), lambda _: json_response(502, "{}"))

    delivery = await client.send("текст")

    assert not delivery.is_delivered
    assert delivery.outcome == NotificationOutcome.TRANSIENT
    assert stub.call_count == 2


async def test_network_failure_is_transient():
    def fail(_: Request) -> HttpResponse:
        raise ConnectionError("нет сети")

    client, _ = create(options(), fail)

    delivery = await client.send("текст")

    assert delivery.outcome == NotificationOutcome.TRANSIENT
    assert "нет сети" in (delivery.error or "")


async def test_verify_reads_bot_from_nested_or_flat_payload():
    flat, _ = create(options(), lambda _: ok('{"user_id":"7","name":"Arb Bot"}'))
    nested, _ = create(options(), lambda _: ok('{"bot":{"user_id":"7","name":"Arb Bot"},"subscriptions":[]}'))
    broken, _ = create(options(), lambda _: json_response(403, '{"meta":{"code":"access.denied"}}'))

    assert (await flat.verify()).success

    info = await nested.verify()
    assert info.success
    assert info.bot is not None
    assert info.bot.name == "Arb Bot"

    failure = await broken.verify()
    assert not failure.success
    assert "access.denied" in failure.detail


async def test_poll_updates_collects_chat_ids_and_marker():
    payload = """
        {
          "updates": [
            { "update_type": "message_created", "message": { "recipient": { "chat_id": 42 }, "body": { "text": "привет" } } },
            { "update_type": "bot_started", "chat_id": "99" }
          ],
          "marker": 1234
        }
    """
    client, stub = create(options(), lambda _: ok(payload))

    page = await client.poll_updates("7")

    assert page.marker == "1234"
    assert [c.chat_id for c in page.chats] == ["42", "99"]
    assert page.chats[0].text == "привет"
    assert "marker=7" in urlsplit(stub.requests[0].url).query


async def test_poll_updates_timeout_is_in_seconds_and_client_waits_longer():
    timeouts: list[float] = []
    stub = StubHttp(lambda _: ok('{"updates":[],"marker":5}'))

    async def sender(method, url, headers, body, timeout):
        timeouts.append(timeout)
        return await stub(method, url, headers, body, timeout)

    client = MaxMessengerClient(FakeConfigProvider(options()), ManualClock(NOW), sender=sender)

    await client.poll_updates(None, wait_seconds=30)
    await client.poll_updates(None, wait_seconds=500)

    assert "timeout=30" in urlsplit(stub.requests[0].url).query  # секунды, а не миллисекунды
    assert "timeout=90" in urlsplit(stub.requests[1].url).query  # верхняя граница API
    assert timeouts[0] == 31.0  # ожидание сервера + timeout_ms (1 с) на передачу


async def test_poll_updates_client_timeout_returns_empty_page_with_same_marker():
    async def never(method, url, headers, body, timeout):
        await asyncio.sleep(10)

    client = MaxMessengerClient(FakeConfigProvider(options(lambda n: setattr(n, "timeout_ms", 500))), ManualClock(NOW), sender=never)

    page = await client.poll_updates("77", wait_seconds=0)

    assert page.marker == "77"
    assert page.chats == []


async def test_poll_updates_reports_http_error():
    client, _ = create(options(), lambda _: json_response(401, '{"meta":{"code":"verify.token"}}'))

    with pytest.raises(MaxApiError) as error:
        await client.poll_updates(None)
    assert "verify.token" in str(error.value)
