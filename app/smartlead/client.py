import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from app.utils import to_iso


class SmartLeadError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SmartLeadClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        api_key: str,
        *,
        max_retries: int = 3,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http_client
        self._api_key = api_key
        self._max_retries = max_retries
        self._sleep = sleeper

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        safe_params = {**(params or {}), "api_key": self._api_key}

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http.request(
                    method,
                    path.lstrip("/"),
                    params=safe_params,
                    json=json,
                )
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise SmartLeadError("SmartLead request failed") from exc
                await self._sleep(min(2**attempt, 30))
                continue

            if response.status_code < 400:
                try:
                    return response.json()
                except ValueError as exc:
                    raise SmartLeadError(
                        "SmartLead returned invalid JSON",
                        status_code=response.status_code,
                    ) from exc

            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < self._max_retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = (
                        float(retry_after) if retry_after is not None else 2**attempt
                    )
                except ValueError:
                    delay = 2**attempt
                await self._sleep(min(max(delay, 0), 60))
                continue

            message = "SmartLead request was rejected"
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    message = str(
                        payload.get("message")
                        or payload.get("error")
                        or payload.get("detail")
                        or message
                    )
            except ValueError:
                pass
            raise SmartLeadError(message, status_code=response.status_code)

        raise SmartLeadError("SmartLead request failed")

    async def get_campaign(self, campaign_id: int) -> dict[str, Any]:
        payload = await self._request("GET", f"/campaigns/{campaign_id}")
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        if isinstance(payload, dict):
            return payload
        raise SmartLeadError("SmartLead returned an invalid campaign")

    async def get_categories(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/leads/fetch-categories")
        if isinstance(payload, dict):
            payload = payload.get("data", payload.get("categories", []))
        if not isinstance(payload, list):
            raise SmartLeadError("SmartLead returned invalid lead categories")
        return [item for item in payload if isinstance(item, dict)]

    async def get_inbox_page(
        self,
        *,
        campaign_ids: list[int],
        category_ids: list[int],
        offset: int,
        limit: int,
        fetch_message_history: bool,
        reply_time_from: datetime | None = None,
        reply_time_to: datetime | None = None,
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {
            "emailStatus": "Replied",
            "campaignId": campaign_ids,
            "leadCategories": {"categoryIdsIn": category_ids},
        }
        if reply_time_from is not None or reply_time_to is not None:
            lower = reply_time_from or datetime(1970, 1, 1, tzinfo=UTC)
            upper = reply_time_to or datetime.now(UTC)
            filters["replyTimeBetween"] = [to_iso(lower), to_iso(upper)]

        payload = await self._request(
            "POST",
            "/master-inbox/inbox-replies",
            params={"fetch_message_history": str(fetch_message_history).lower()},
            json={
                "offset": offset,
                "limit": limit,
                "filters": filters,
                "sortBy": "REPLY_TIME_DESC",
            },
        )
        if not isinstance(payload, dict):
            raise SmartLeadError("SmartLead returned an invalid inbox page")

        # Master Inbox currently returns records in `data` and does not always
        # include a total. Keep accepting the older/documented `messages`
        # shape so callers have one stable interface.
        if "messages" not in payload and isinstance(payload.get("data"), list):
            payload = {**payload, "messages": payload["data"]}
        if "total_count" not in payload:
            for key in ("totalCount", "total"):
                if payload.get(key) is not None:
                    payload = {**payload, "total_count": payload[key]}
                    break
        return payload

    async def get_campaign_leads_page(
        self,
        *,
        campaign_id: int,
        category_id: int,
        offset: int,
        limit: int = 100,
    ) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            f"/campaigns/{campaign_id}/leads",
            params={
                "offset": offset,
                "limit": limit,
                "emailStatus": "is_replied",
                "lead_category_id": category_id,
            },
        )
        if not isinstance(payload, dict):
            raise SmartLeadError("SmartLead returned an invalid campaign lead page")
        return payload

    async def get_lead_message_history(
        self, *, campaign_id: int, lead_id: str
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/campaigns/{campaign_id}/leads/{lead_id}/message-history",
            params={"show_plain_text_response": "true"},
        )
        messages: Any
        if isinstance(payload, list):
            messages = payload
        elif isinstance(payload, dict):
            messages = payload.get("messages")
            if not isinstance(messages, list):
                messages = payload.get("data")
            if not isinstance(messages, list):
                messages = payload.get("history")
        else:
            messages = None
        if not isinstance(messages, list):
            raise SmartLeadError("SmartLead returned invalid lead message history")
        return [item for item in messages if isinstance(item, dict)]
