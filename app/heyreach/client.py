import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.utils import to_iso


class HeyReachError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HeyReachClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        api_key: str,
        *,
        max_retries: int = 3,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        request_limiter: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._http = http_client
        self._api_key = api_key
        self._max_retries = max_retries
        self._sleep = sleeper
        self._request_limiter = request_limiter

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-KEY": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        for attempt in range(self._max_retries + 1):
            if self._request_limiter is not None:
                await self._request_limiter()
            try:
                response = await self._http.request(
                    method,
                    path.lstrip("/"),
                    params=params,
                    json=json,
                    headers=self._headers(),
                )
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise HeyReachError("HeyReach request failed") from exc
                await self._sleep(min(2**attempt, 30))
                continue

            if response.status_code < 400:
                if not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError as exc:
                    raise HeyReachError(
                        "HeyReach returned invalid JSON",
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

            message = "HeyReach request was rejected"
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    message = str(
                        payload.get("message")
                        or payload.get("error")
                        or payload.get("detail")
                        or payload.get("title")
                        or message
                    )
            except ValueError:
                pass
            raise HeyReachError(message, status_code=response.status_code)

        raise HeyReachError("HeyReach request failed")

    @staticmethod
    def _page(payload: Any) -> dict[str, Any]:
        if isinstance(payload, list):
            return {"items": [item for item in payload if isinstance(item, dict)]}
        if not isinstance(payload, dict):
            raise HeyReachError("HeyReach returned an invalid page")
        items = payload.get("items")
        if not isinstance(items, list):
            items = payload.get("data", [])
        if not isinstance(items, list):
            raise HeyReachError("HeyReach returned an invalid page")
        page = {**payload, "items": [item for item in items if isinstance(item, dict)]}
        if "totalCount" not in page:
            for key in ("total_count", "total"):
                if page.get(key) is not None:
                    page = {**page, "totalCount": page[key]}
                    break
        return page

    async def get_campaign(self, campaign_id: int) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            "/campaign/GetById",
            params={"campaignId": campaign_id},
        )
        if isinstance(payload, dict) and isinstance(payload.get("id"), int | str):
            return payload
        page = self._page(payload)
        if page["items"]:
            return page["items"][0]
        raise HeyReachError("HeyReach returned an invalid campaign")

    async def list_campaigns(self) -> list[dict[str, Any]]:
        campaigns: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._page(
                await self._request(
                    "POST",
                    "/campaign/GetAll",
                    json={"offset": offset, "limit": 100},
                )
            )
            items = page["items"]
            campaigns.extend(items)
            offset += len(items)
            total = page.get("totalCount")
            if not items or (
                total is not None and offset >= int(total)
            ) or len(items) < 100:
                break
        return campaigns

    async def get_campaign_leads_page(
        self,
        *,
        campaign_id: int,
        offset: int,
        limit: int = 100,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "campaignId": campaign_id,
            "offset": offset,
            "limit": limit,
        }
        if time_from is not None or time_to is not None:
            body["timeFilter"] = "LastActionTakenTime"
            if time_from is not None:
                body["timeFrom"] = to_iso(time_from)
            if time_to is not None:
                body["timeTo"] = to_iso(time_to)
        return self._page(
            await self._request("POST", "/campaign/GetLeadsFromCampaign", json=body)
        )

    async def get_conversations_page(
        self,
        *,
        campaign_ids: list[int] | None = None,
        lead_profile_url: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {}
        if campaign_ids:
            filters["campaignIds"] = campaign_ids
        if lead_profile_url:
            filters["leadProfileUrl"] = lead_profile_url
        return self._page(
            await self._request(
                "POST",
                "/inbox/GetConversationsV2",
                json={"offset": offset, "limit": limit, "filters": filters},
            )
        )

    async def get_chatroom(
        self, *, account_id: int, conversation_id: str
    ) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            "/inbox/GetChatroom/"
            f"{quote(str(account_id), safe='')}/"
            f"{quote(conversation_id, safe='')}",
        )
        if isinstance(payload, dict):
            return payload
        raise HeyReachError("HeyReach returned an invalid chatroom")

    async def get_linkedin_account(self, account_id: int) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            "/li_account/GetById",
            params={"accountId": account_id},
        )
        if isinstance(payload, dict) and isinstance(payload.get("id"), int | str):
            return payload
        page = self._page(payload)
        if page["items"]:
            return page["items"][0]
        raise HeyReachError("HeyReach returned an invalid LinkedIn account")

    async def list_linkedin_accounts(self) -> list[dict[str, Any]]:
        accounts: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._page(
                await self._request(
                    "POST",
                    "/li_account/GetAll",
                    json={"offset": offset, "limit": 100},
                )
            )
            items = page["items"]
            accounts.extend(items)
            offset += len(items)
            total = page.get("totalCount")
            if not items or (
                total is not None and offset >= int(total)
            ) or len(items) < 100:
                break
        return accounts

    async def list_webhooks(self) -> list[dict[str, Any]]:
        webhooks: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._page(
                await self._request(
                    "POST",
                    "/webhooks/GetAllWebhooks",
                    json={"offset": offset, "limit": 100},
                )
            )
            items = page["items"]
            webhooks.extend(items)
            offset += len(items)
            total = page.get("totalCount")
            if not items or (
                total is not None and offset >= int(total)
            ) or len(items) < 100:
                break
        return webhooks

    async def save_webhook(
        self,
        *,
        name: str,
        webhook_url: str,
        event_type: str,
        campaign_ids: list[int],
        webhook_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a webhook subscription and return a record with its id."""
        body: dict[str, Any] = {
            "webhookName": name,
            "webhookUrl": webhook_url,
            "eventType": event_type,
            "campaignIds": campaign_ids,
        }
        payload: Any = None
        if webhook_id not in (None, ""):
            try:
                payload = await self._request(
                    "PATCH",
                    "/webhooks/UpdateWebhook",
                    json={**body, "webhookId": webhook_id, "isActive": True},
                )
            except HeyReachError as exc:
                if exc.status_code != 404:
                    raise
                payload = None
            else:
                return {**self._webhook_record(payload), "id": str(webhook_id)}
        payload = await self._request("POST", "/webhooks/CreateWebhook", json=body)
        record = self._webhook_record(payload)
        identifier = record.get("id") or record.get("webhookId")
        if identifier is None:
            for existing in await self.list_webhooks():
                if (
                    str(existing.get("webhookUrl") or existing.get("webhook_url") or "")
                    == webhook_url
                    and str(existing.get("eventType") or "") == event_type
                ):
                    identifier = existing.get("id") or existing.get("webhookId")
                    record = {**existing, **record}
                    break
        if identifier is None:
            raise HeyReachError("HeyReach did not return a webhook id")
        return {**record, "id": str(identifier)}

    async def delete_webhook(self, webhook_id: str) -> None:
        await self._request(
            "DELETE",
            "/webhooks/DeleteWebhook",
            params={"webhookId": webhook_id},
        )

    @staticmethod
    def _webhook_record(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        record = payload.get("data", payload)
        if isinstance(record, list):
            record = next((item for item in record if isinstance(item, dict)), {})
        return record if isinstance(record, dict) else {}
