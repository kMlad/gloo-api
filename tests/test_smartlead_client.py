import json

import httpx
import pytest

from app.smartlead.client import SmartLeadClient


@pytest.mark.asyncio
async def test_retries_rate_limits_and_keeps_api_key_in_query() -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "slow"})
        return httpx.Response(
            200,
            json=[{"id": 1, "name": "Interested", "sentiment_type": "positive"}],
        )

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(
        base_url="https://server.smartlead.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = SmartLeadClient(
            http_client, "api-secret", max_retries=1, sleeper=sleeper
        )
        categories = await client.get_categories()

    assert categories[0]["sentiment_type"] == "positive"
    assert len(requests) == 2
    assert requests[0].url.path == "/api/v1/leads/fetch-categories"
    assert requests[0].url.params["api_key"] == "api-secret"
    assert delays == [0]


@pytest.mark.asyncio
async def test_inbox_request_contains_positive_filters_and_history_flag() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [], "total_count": 0})

    async with httpx.AsyncClient(
        base_url="https://server.smartlead.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = SmartLeadClient(http_client, "secret", max_retries=0)
        await client.get_inbox_page(
            campaign_ids=[10, 20],
            category_ids=[1, 3],
            offset=0,
            limit=20,
            fetch_message_history=True,
        )

    assert captured["url"].params["fetch_message_history"] == "true"
    assert captured["body"]["filters"]["campaignId"] == [10, 20]
    assert captured["body"]["filters"]["leadCategories"]["categoryIdsIn"] == [1, 3]


@pytest.mark.asyncio
async def test_inbox_normalizes_data_wrapper_used_by_smartlead() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [{"email_lead_map_id": "map-1"}],
                "offset": 0,
                "limit": 20,
            },
        )

    async with httpx.AsyncClient(
        base_url="https://server.smartlead.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = SmartLeadClient(http_client, "secret", max_retries=0)
        page = await client.get_inbox_page(
            campaign_ids=[10],
            category_ids=[1],
            offset=0,
            limit=20,
            fetch_message_history=False,
        )

    assert page["messages"] == [{"email_lead_map_id": "map-1"}]
    assert "total_count" not in page
