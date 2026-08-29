import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

_AUDIT_HEADERS = {
    "retry-after",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-credits-remaining",
    "x-credits-cost",
    "x-daily-request-left",
    "x-minute-request-left",
    "x-daily-reset-seconds",
    "x-minute-reset-seconds",
    "x-request-id",
}


class FixedWindowRateLimiter:
    """Coordinates a provider's fixed calendar-minute request budget."""

    def __init__(
        self,
        max_calls: int,
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._max_calls = max_calls
        self._clock = clock
        self._sleep = sleeper
        self._window = -1
        self._calls = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            wait_seconds = 0.0
            async with self._lock:
                now = self._clock()
                window = int(now // 60)
                if window != self._window:
                    self._window = window
                    self._calls = 0
                if self._calls < self._max_calls:
                    self._calls += 1
                    return
                wait_seconds = max(((window + 1) * 60) - now, 0.05)
            await self._sleep(wait_seconds)


@dataclass(slots=True)
class ProviderResult:
    status: str
    request_payload: dict[str, Any]
    response_payload: Any = None
    response_headers: dict[str, str] | None = None
    http_status: int | None = None
    phone: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    external_request_id: str | None = None


class BaseProviderClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        max_retries: int,
        concurrency: int,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        request_limiter: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._http = http_client
        self._max_retries = max_retries
        self._semaphore = asyncio.Semaphore(concurrency)
        self._sleep = sleeper
        self._request_limiter = request_limiter

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        request_payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ProviderResult:
        async with self._semaphore:
            for attempt in range(self._max_retries + 1):
                try:
                    if self._request_limiter is not None:
                        await self._request_limiter()
                    response = await self._http.request(
                        method,
                        path.lstrip("/"),
                        json=request_payload if method.upper() != "GET" else None,
                        headers=headers,
                        params=params,
                    )
                except httpx.ConnectTimeout:
                    if attempt < self._max_retries:
                        await self._sleep(min(2**attempt, 30))
                        continue
                    return ProviderResult(
                        status="timed_out",
                        request_payload=request_payload,
                        error_code="connect_timeout",
                        error_message="Provider connection timed out",
                    )
                except httpx.ReadTimeout:
                    return ProviderResult(
                        status="timed_out",
                        request_payload=request_payload,
                        error_code="read_timeout",
                        error_message="Provider response timed out",
                    )
                except httpx.TimeoutException:
                    return ProviderResult(
                        status="timed_out",
                        request_payload=request_payload,
                        error_code="request_timeout",
                        error_message="Provider request timed out",
                    )
                except httpx.ConnectError:
                    if attempt < self._max_retries:
                        await self._sleep(min(2**attempt, 30))
                        continue
                    return ProviderResult(
                        status="failed",
                        request_payload=request_payload,
                        error_code="connection_error",
                        error_message="Provider connection failed",
                    )
                except httpx.RequestError:
                    return ProviderResult(
                        status="failed",
                        request_payload=request_payload,
                        error_code="request_error",
                        error_message="Provider request failed",
                    )

                audit_headers = {
                    key.casefold(): value
                    for key, value in response.headers.items()
                    if key.casefold() in _AUDIT_HEADERS
                }
                try:
                    payload: Any = response.json()
                except ValueError:
                    payload = {"raw_text": response.text}
                    if response.status_code < 400:
                        return ProviderResult(
                            status="failed",
                            request_payload=request_payload,
                            response_payload=payload,
                            response_headers=audit_headers,
                            http_status=response.status_code,
                            error_code="invalid_json",
                            error_message="Provider returned invalid JSON",
                        )

                retryable = response.status_code == 429 or response.status_code >= 500
                if retryable and attempt < self._max_retries:
                    await self._sleep(self._retry_delay(response, attempt))
                    continue

                error_code, error_message = self._safe_error(payload)
                if response.status_code >= 400:
                    return ProviderResult(
                        status="rate_limited" if response.status_code == 429 else "failed",
                        request_payload=request_payload,
                        response_payload=payload,
                        response_headers=audit_headers,
                        http_status=response.status_code,
                        error_code=error_code,
                        error_message=error_message,
                        external_request_id=audit_headers.get("x-request-id"),
                    )
                return ProviderResult(
                    status="not_found",
                    request_payload=request_payload,
                    response_payload=payload,
                    response_headers=audit_headers,
                    http_status=response.status_code,
                    external_request_id=audit_headers.get("x-request-id"),
                )

        return ProviderResult(
            status="failed",
            request_payload=request_payload,
            error_code="request_error",
            error_message="Provider request failed",
        )

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        for header in (
            "Retry-After",
            "RateLimit-Reset",
            "X-Minute-Reset-Seconds",
            "X-Daily-Reset-Seconds",
        ):
            value = response.headers.get(header)
            if value is None:
                continue
            try:
                return min(max(float(value), 0), 60)
            except ValueError:
                if header == "Retry-After":
                    try:
                        delay = (
                            parsedate_to_datetime(value)
                            - parsedate_to_datetime(response.headers.get("Date", ""))
                        ).total_seconds()
                        return min(max(delay, 0), 60)
                    except (TypeError, ValueError, OverflowError):
                        pass
        return min(2**attempt, 60)

    @staticmethod
    def _safe_error(payload: Any) -> tuple[str | None, str]:
        fallback = "Provider rejected the request"
        if not isinstance(payload, dict):
            return None, fallback
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            item = errors[0]
            return (
                str(item.get("code")) if item.get("code") is not None else None,
                str(item.get("title") or item.get("detail") or fallback),
            )
        code = payload.get("error_code") or payload.get("code")
        message = payload.get("message") or payload.get("detail") or payload.get("error")
        return (
            str(code) if code is not None else None,
            str(message) if isinstance(message, (str, int, float)) else fallback,
        )
