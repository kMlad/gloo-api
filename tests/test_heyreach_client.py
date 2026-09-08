import json

import httpx
import pytest

from app.heyreach.client import HeyReachClient, HeyReachError


@pytest.mark.asyncio
async def test_lists_all_campaign_pages() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if body["offset"] == 0:
            return httpx.Response(
                200,
                json={
                    "totalCount": 101,
                    "items": [{"id": index, "name": f"C{index}"} for index in range(100)],
                },
            )
        return httpx.Response(
            200,
            json={"totalCount": 101, "items": [{"id": 100, "name": "Last"}]},
        )

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        campaigns = await HeyReachClient(
            http_client, "secret", max_retries=0
        ).list_campaigns()

    assert len(campaigns) == 101
    assert campaigns[-1]["name"] == "Last"
    assert requests[0].url.path == "/api/public/campaign/GetAll"
    assert requests[0].headers["x-api-key"] == "secret"
    assert json.loads(requests[0].content) == {"offset": 0, "limit": 100}


@pytest.mark.asyncio
async def test_get_campaign_uses_query_param() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={"id": 12, "name": "Outbound"})

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        campaign = await HeyReachClient(
            http_client, "secret", max_retries=0
        ).get_campaign(12)

    assert campaign["name"] == "Outbound"
    assert captured["url"].path == "/api/public/campaign/GetById"
    assert captured["url"].params["campaignId"] == "12"


@pytest.mark.asyncio
async def test_campaign_leads_include_last_action_time_filter() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"items": [], "totalCount": 0})

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        from datetime import UTC, datetime

        await HeyReachClient(http_client, "secret", max_retries=0).get_campaign_leads_page(
            campaign_id=9,
            offset=0,
            time_from=datetime(2026, 9, 1, tzinfo=UTC),
        )

    assert captured["body"]["campaignId"] == 9
    assert captured["body"]["timeFilter"] == "LastActionTakenTime"
    assert captured["body"]["timeFrom"].startswith("2026-09-01")


@pytest.mark.asyncio
async def test_retries_rate_limits_and_keeps_api_key_in_header() -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"id": 1, "name": "Campaign"})

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        campaign = await HeyReachClient(
            http_client, "api-secret", max_retries=1, sleeper=sleeper
        ).get_campaign(1)

    assert campaign["id"] == 1
    assert len(requests) == 2
    assert requests[0].headers["x-api-key"] == "api-secret"
    assert delays == [0]


@pytest.mark.asyncio
async def test_conversations_and_chatroom_paths() -> None:
    captured: list[tuple[str, str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        captured.append((request.method, str(request.url), body))
        if "GetConversationsV2" in str(request.url):
            return httpx.Response(200, json={"items": [{"id": "conv-1"}]})
        return httpx.Response(
            200, json={"id": "conv-1", "messages": [{"body": "Hi", "isFromMe": False}]}
        )

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = HeyReachClient(http_client, "secret", max_retries=0)
        page = await client.get_conversations_page(
            campaign_ids=[10],
            lead_profile_url="https://www.linkedin.com/in/pat",
        )
        chatroom = await client.get_chatroom(account_id=7, conversation_id="conv-1")

    assert page["items"][0]["id"] == "conv-1"
    assert chatroom["messages"][0]["body"] == "Hi"
    assert captured[0][1].endswith("/inbox/GetConversationsV2")
    assert captured[0][2]["filters"]["campaignIds"] == [10]
    assert "/inbox/GetChatroom/7/conv-1" in captured[1][1]


@pytest.mark.asyncio
async def test_lists_linkedin_accounts() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/public/li_account/GetAll"
        return httpx.Response(
            200,
            json={
                "totalCount": 1,
                "items": [{"id": 7, "firstName": "Alex", "lastName": "Sender"}],
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        accounts = await HeyReachClient(
            http_client, "secret", max_retries=0
        ).list_linkedin_accounts()

    assert accounts[0]["lastName"] == "Sender"


@pytest.mark.asyncio
async def test_get_linkedin_account_uses_query_param() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(
            200, json={"id": 7, "firstName": "Alex", "lastName": "Sender"}
        )

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        account = await HeyReachClient(
            http_client, "secret", max_retries=0
        ).get_linkedin_account(7)

    assert account["firstName"] == "Alex"
    assert captured["url"].path == "/api/public/li_account/GetById"
    assert captured["url"].params["accountId"] == "7"


@pytest.mark.asyncio
async def test_invalid_json_is_reported() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        with pytest.raises(HeyReachError, match="invalid JSON"):
            await HeyReachClient(http_client, "secret", max_retries=0).get_campaign(1)


@pytest.mark.asyncio
async def test_create_webhook_posts_subscription_and_returns_id() -> None:
    captured: list[tuple[str, str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"id": 42, "webhookName": "Gloo"})

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        saved = await HeyReachClient(http_client, "secret", max_retries=0).save_webhook(
            name="Gloo",
            webhook_url="https://api.example.com/hook",
            event_type="LEAD_TAG_UPDATED",
            campaign_ids=[10],
        )

    assert saved["id"] == "42"
    assert captured == [
        (
            "POST",
            "/api/public/webhooks/CreateWebhook",
            {
                "webhookName": "Gloo",
                "webhookUrl": "https://api.example.com/hook",
                "eventType": "LEAD_TAG_UPDATED",
                "campaignIds": [10],
            },
        )
    ]


@pytest.mark.asyncio
async def test_create_webhook_falls_back_to_listing_when_id_is_missing() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/CreateWebhook"):
            return httpx.Response(200, content=b"")
        return httpx.Response(
            200,
            json={
                "totalCount": 2,
                "items": [
                    {"id": 1, "webhookUrl": "https://other", "eventType": "X"},
                    {
                        "id": 9,
                        "webhookUrl": "https://api.example.com/hook",
                        "eventType": "LEAD_TAG_UPDATED",
                    },
                ],
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        saved = await HeyReachClient(http_client, "secret", max_retries=0).save_webhook(
            name="Gloo",
            webhook_url="https://api.example.com/hook",
            event_type="LEAD_TAG_UPDATED",
            campaign_ids=[10],
        )

    assert saved["id"] == "9"
    assert paths == [
        "/api/public/webhooks/CreateWebhook",
        "/api/public/webhooks/GetAllWebhooks",
    ]


@pytest.mark.asyncio
async def test_update_webhook_patches_and_recreates_when_missing() -> None:
    captured: list[tuple[str, str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        captured.append((request.method, request.url.path, body))
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": 5})
        return httpx.Response(200, json={"id": 6})

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = HeyReachClient(http_client, "secret", max_retries=0)
        updated = await client.save_webhook(
            name="Gloo",
            webhook_url="https://api.example.com/hook",
            event_type="LEAD_TAG_UPDATED",
            campaign_ids=[10],
            webhook_id="5",
        )

    assert updated["id"] == "5"
    assert captured[0][0] == "PATCH"
    assert captured[0][1] == "/api/public/webhooks/UpdateWebhook"
    assert captured[0][2]["webhookId"] == "5"
    assert captured[0][2]["isActive"] is True
    assert captured[0][2]["campaignIds"] == [10]

    captured.clear()

    async def missing(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        captured.append((request.method, request.url.path, body))
        if request.method == "PATCH":
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json={"id": 6})

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(missing),
    ) as http_client:
        client = HeyReachClient(http_client, "secret", max_retries=0)
        recreated = await client.save_webhook(
            name="Gloo",
            webhook_url="https://api.example.com/hook",
            event_type="LEAD_TAG_UPDATED",
            campaign_ids=[10],
            webhook_id="5",
        )

    assert recreated["id"] == "6"
    assert [item[0] for item in captured] == ["PATCH", "POST"]


@pytest.mark.asyncio
async def test_delete_webhook_uses_query_param_and_surfaces_errors() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = request.url
        return httpx.Response(204)

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        await HeyReachClient(http_client, "secret", max_retries=0).delete_webhook("42")

    assert captured["method"] == "DELETE"
    assert captured["url"].path == "/api/public/webhooks/DeleteWebhook"
    assert captured["url"].params["webhookId"] == "42"

    async def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Webhook not found"})

    async with httpx.AsyncClient(
        base_url="https://api.heyreach.io/api/public/",
        transport=httpx.MockTransport(gone),
    ) as http_client:
        with pytest.raises(HeyReachError) as excinfo:
            await HeyReachClient(http_client, "secret", max_retries=0).delete_webhook(
                "42"
            )

    assert excinfo.value.status_code == 404
