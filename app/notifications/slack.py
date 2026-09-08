import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx


class SlackError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error


class SlackClient:
    """Minimal Slack Web API client for ``chat.postMessage`` with a bot token."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        bot_token: str,
        *,
        max_retries: int = 2,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http_client
        self._bot_token = bot_token
        self._max_retries = max_retries
        self._sleep = sleeper

    async def post_message(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
    ) -> str:
        """Post a message and return its ``ts`` so callers can thread replies."""
        body: dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            body["blocks"] = blocks
        if thread_ts:
            body["thread_ts"] = thread_ts

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http.post(
                    "chat.postMessage",
                    json=body,
                    headers={"Authorization": f"Bearer {self._bot_token}"},
                )
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise SlackError("Slack request failed") from exc
                await self._sleep(min(2**attempt, 30))
                continue

            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < self._max_retries:
                await self._sleep(self._retry_delay(response, attempt))
                continue
            if response.status_code >= 400:
                raise SlackError(
                    f"Slack rejected the request with HTTP {response.status_code}",
                    status_code=response.status_code,
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise SlackError(
                    "Slack returned invalid JSON", status_code=response.status_code
                ) from exc
            if not isinstance(payload, dict):
                raise SlackError("Slack returned an invalid response")
            if not payload.get("ok"):
                error = str(payload.get("error") or "unknown_error")
                if error == "ratelimited" and attempt < self._max_retries:
                    await self._sleep(self._retry_delay(response, attempt))
                    continue
                raise SlackError(
                    f"Slack returned an error: {error}",
                    status_code=response.status_code,
                    error=error,
                )
            ts = payload.get("ts")
            if not ts:
                raise SlackError("Slack did not return a message timestamp")
            return str(ts)

        raise SlackError("Slack request failed")

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after is not None else 2**attempt
        except ValueError:
            delay = 2**attempt
        return min(max(delay, 0), 60)
