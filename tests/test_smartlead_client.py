import json

import httpx
import pytest

from app.smartlead.client import SmartLeadClient


@pytest.mark.asyncio
async def test_lists_campaign_catalog_with_tags() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(
            200,
            json=[
                {
                    "id": 10,
                    "name": "Campaign",
                    "status": "ACTIVE",
                    "tags": [{"tag_id": 1, "tag_name": "Outbound"}],
                }
            ],
        )

    async with httpx.AsyncClient(
        base_url="https://server.smartlead.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        campaigns = await SmartLeadClient(
            http_client, "secret", max_retries=0
        ).list_campaigns()

    assert captured["url"].path == "/api/v1/campaigns/"
    assert captured["url"].params["include_tags"] == "true"
    assert campaigns[0]["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_retries_rate_limits_and_keeps_api_key_in_query() -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "0"}, json={"error": "slow"}
            )
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


@pytest.mark.asyncio
async def test_lead_message_history_requests_plain_text_and_normalizes_wrappers() -> (
    None
):
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(
            200,
            json={
                "history": [
                    {"id": "msg-1", "direction": "outbound"},
                    "ignored",
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://server.smartlead.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = SmartLeadClient(http_client, "secret", max_retries=0)
        messages = await client.get_lead_message_history(campaign_id=10, lead_id="99")

    assert captured["url"].path == "/api/v1/campaigns/10/leads/99/message-history"
    assert captured["url"].params["show_plain_text_response"] == "true"
    assert captured["url"].params["api_key"] == "secret"
    assert messages == [{"id": "msg-1", "direction": "outbound"}]


@pytest.mark.asyncio
async def test_lead_message_history_accepts_messages_wrapper() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"messages": [{"id": "msg-2", "direction": "inbound"}]},
        )

    async with httpx.AsyncClient(
        base_url="https://server.smartlead.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = SmartLeadClient(http_client, "secret", max_retries=0)
        messages = await client.get_lead_message_history(campaign_id=10, lead_id="99")

    assert messages == [{"id": "msg-2", "direction": "inbound"}]


@pytest.mark.asyncio
async def test_lead_message_history_accepts_data_wrapper() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "msg-3", "direction": "outbound"}]},
        )

    async with httpx.AsyncClient(
        base_url="https://server.smartlead.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = SmartLeadClient(http_client, "secret", max_retries=0)
        messages = await client.get_lead_message_history(campaign_id=10, lead_id="99")

    assert messages == [{"id": "msg-3", "direction": "outbound"}]


@pytest.mark.asyncio
async def test_save_webhook_posts_campaign_webhook_and_returns_id() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = request.url
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"id": 555}})

    async with httpx.AsyncClient(
        base_url="https://server.smartlead.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        saved = await SmartLeadClient(http_client, "secret", max_retries=0).save_webhook(
            10,
            name="Gloo speed to lead",
            webhook_url="https://api.example.com/hook",
            event_types=["LEAD_CATEGORY_UPDATED"],
            categories=["Interested"],
        )

    assert captured["method"] == "POST"
    assert captured["url"].path == "/api/v1/campaigns/10/webhooks"
    assert captured["url"].params["api_key"] == "secret"
    assert captured["body"] == {
        "id": None,
        "name": "Gloo speed to lead",
        "webhook_url": "https://api.example.com/hook",
        "event_types": ["LEAD_CATEGORY_UPDATED"],
        "categories": ["Interested"],
    }
    assert saved["id"] == "555"


@pytest.mark.asyncio
async def test_save_webhook_falls_back_to_listing_when_no_id_returned() -> None:
    bodies: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": 1, "webhook_url": "https://other.example/hook"},
                    {"id": 2, "webhook_url": "https://api.example.com/hook"},
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://server.smartlead.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        saved = await SmartLeadClient(http_client, "secret", max_retries=0).save_webhook(
            10,
            name="Gloo speed to lead",
            webhook_url="https://api.example.com/hook",
            event_types=["LEAD_CATEGORY_UPDATED"],
            webhook_id="2",
        )

    assert bodies[0]["id"] == 2
    assert saved["id"] == "2"


@pytest.mark.asyncio
async def test_delete_webhook_sends_id_in_body() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = request.url
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        base_url="https://server.smartlead.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        await SmartLeadClient(http_client, "secret", max_retries=0).delete_webhook(
            10, "555"
        )

    assert captured["method"] == "DELETE"
    assert captured["url"].path == "/api/v1/campaigns/10/webhooks"
    assert captured["body"] == {"id": 555}


@pytest.mark.asyncio
async def test_list_webhooks_unwraps_data() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/campaigns/10/webhooks"
        return httpx.Response(200, json={"data": [{"id": 1, "event_types": []}]})

    async with httpx.AsyncClient(
        base_url="https://server.smartlead.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        webhooks = await SmartLeadClient(
            http_client, "secret", max_retries=0
        ).list_webhooks(10)

    assert webhooks == [{"id": 1, "event_types": []}]
