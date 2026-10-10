"""Порт MaxMessengerClientTests.cs на httpx MockTransport."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
from fakes import FakeConfigProvider

from futures_arb_bot.domain.enums import MaxSendEndpoint, NotificationOutcome
from futures_arb_bot.notifications.max_client import MaxMessengerClient, _parse_updates

D = Decimal


def build_client(handler):
    config = FakeConfigProvider()
    notifications = config.Current.Notifications
    notifications.Enabled = True
    notifications.BotToken = "secret-token"
    notifications.AdminChatId = "chat-42"
    notifications.MaxRetries = 2
    notifications.TimeoutMs = 1000
    client = MaxMessengerClient(config, transport=httpx.MockTransport(handler))
    return client, config


def send(client):
    async def run():
        return await client.SendAsync("текст")

    return asyncio.run(run())


def test_success_sends_v2_messages_endpoint_without_bearer():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client, _ = build_client(handler)
    delivery = send(client)

    assert delivery.IsDelivered
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/messages"
    assert request.url.params["chat_id"] == "chat-42"
    assert request.headers["authorization"] == "secret-token"
    assert json.loads(request.content)["text"] == "текст"


def test_markdown_and_silent_are_serialized():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    client, config = build_client(handler)
    config.Current.Notifications.UseMarkdown = True
    config.Current.Notifications.Silent = True
    send(client)

    body = json.loads(requests[0].content)
    assert body["format"] == "markdown"
    assert body["notify"] is False


def test_fallback_endpoint_chats_message():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    client, config = build_client(handler)
    config.Current.Notifications.Endpoint = MaxSendEndpoint.ChatsMessage
    send(client)

    assert requests[0].url.path == "/chats/chat-42/message"


def test_401_is_permanent_no_retries():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"code": "UNAUTHORIZED", "message": "bad token"})

    client, _ = build_client(handler)
    delivery = send(client)

    assert delivery.Outcome == NotificationOutcome.Permanent
    assert len(calls) == 1
    assert "401" in delivery.Error and "bad token" in delivery.Error


def test_5xx_is_retried_and_reported_transient():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    client, config = build_client(handler)
    config.Current.Notifications.MaxRetries = 1
    delivery = send(client)

    assert delivery.Outcome == NotificationOutcome.Transient
    assert len(calls) == 2  # первая + одна ретрай-повтор


def test_429_with_retry_after_eventually_delivers():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200)

    client, _ = build_client(handler)
    delivery = send(client)

    assert delivery.IsDelivered
    assert state["n"] == 2


def test_missing_token_or_chat_is_permanent():
    config = FakeConfigProvider()
    config.Current.Notifications.Enabled = True
    client = MaxMessengerClient(config, transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    async def run():
        return await client.SendAsync("x")

    delivery = asyncio.run(run())
    assert delivery.Outcome == NotificationOutcome.Permanent
    assert "BotToken" in delivery.Error


def test_verify_accepts_token_and_parses_bot():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/me"
        return httpx.Response(200, json={"bot": {"name": "ArbBot", "username": "arb_bot", "user_id": "777"}})

    client, _ = build_client(handler)

    async def run():
        return await client.VerifyAsync(None)

    result = asyncio.run(run())
    assert result.Success
    assert "ArbBot" in result.Detail
    assert result.Bot.UserId == "777"


def test_parse_updates_collects_chat_ids_recursively():
    body = json.dumps(
        {
            "marker": "m-1",
            "updates": [
                {
                    "update_id": "1",
                    "update_type": "message_created",
                    "payload": {"message_info": {"chat_id": 55, "text": "привет"}},
                }
            ],
        }
    )

    page = _parse_updates(body)

    assert page.Marker == "m-1"
    assert page.Chats[0].ChatId == "55"
    assert page.Chats[0].Text == "привет"
