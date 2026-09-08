import json

import httpx
import pytest

from app.notifications.slack import SlackClient, SlackError


def _client(handler, *, max_retries: int = 1, delays: list[float] | None = None):
    async def sleeper(delay: float) -> None:
        if delays is not None:
            delays.append(delay)

    http = httpx.AsyncClient(
        base_url="https://slack.com/api/", transport=httpx.MockTransport(handler)
    )
    return http, SlackClient(
        http, "xoxb-secret", max_retries=max_retries, sleeper=sleeper
    )


@pytest.mark.asyncio
async def test_post_message_sends_bearer_token_and_returns_ts() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "ts": "1725700000.000100"})

    http, client = _client(handler)
    async with http:
        ts = await client.post_message(
            "C123", "hello", blocks=[{"type": "divider"}]
        )

    assert ts == "1725700000.000100"
    assert captured["url"].path == "/api/chat.postMessage"
    assert captured["auth"] == "Bearer xoxb-secret"
    assert captured["body"] == {
        "channel": "C123",
        "text": "hello",
        "blocks": [{"type": "divider"}],
    }


@pytest.mark.asyncio
async def test_thread_reply_includes_thread_ts() -> None:
    bodies: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "ts": "2.0"})

    http, client = _client(handler)
    async with http:
        await client.post_message("C123", "reply", thread_ts="1.0")

    assert bodies[0]["thread_ts"] == "1.0"
    assert "blocks" not in bodies[0]


@pytest.mark.asyncio
async def test_rate_limits_are_retried_with_retry_after() -> None:
    calls = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429, headers={"Retry-After": "3"}, json={"ok": False}
            )
        if calls == 2:
            return httpx.Response(200, json={"ok": False, "error": "ratelimited"})
        return httpx.Response(200, json={"ok": True, "ts": "3.0"})

    http, client = _client(handler, max_retries=2, delays=delays)
    async with http:
        ts = await client.post_message("C123", "hello")

    assert ts == "3.0"
    assert calls == 3
    assert delays == [3.0, 2.0]


@pytest.mark.asyncio
async def test_api_error_raises_slack_error_with_code() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    http, client = _client(handler, max_retries=0)
    async with http:
        with pytest.raises(SlackError) as excinfo:
            await client.post_message("C123", "hello")

    assert excinfo.value.error == "channel_not_found"


@pytest.mark.asyncio
async def test_http_error_and_missing_ts_raise() -> None:
    async def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False})

    async def missing_ts(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    http, client = _client(forbidden, max_retries=0)
    async with http:
        with pytest.raises(SlackError) as excinfo:
            await client.post_message("C123", "hello")
    assert excinfo.value.status_code == 403

    http, client = _client(missing_ts, max_retries=0)
    async with http:
        with pytest.raises(SlackError):
            await client.post_message("C123", "hello")
