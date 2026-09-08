import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from supabase_auth.types import User, UserResponse

from app.dependencies import get_speed_to_lead_service
from app.env import Env, get_env
from app.main import create_app
from app.smartlead.client import SmartLeadError
from app.speed_to_lead.service import SpeedToLeadNotFoundError
from app.supabase_client import get_supabase
from supabase import AuthApiError

WEBHOOK_TOKEN = "test-smartlead-webhook-token-32-characters"


def _env() -> Env:
    return Env(
        supabase_url="http://127.0.0.1:54321",
        supabase_secret_key=SecretStr("secret"),
        smartlead_api_key=SecretStr("smartlead"),
        heyreach_api_key=SecretStr("heyreach"),
        leadmagic_api_key=SecretStr("leadmagic"),
        prospeo_api_key=SecretStr("prospeo"),
        airscale_api_key=SecretStr("airscale"),
        fullenrich_api_key=SecretStr("fullenrich"),
        internal_api_token=SecretStr("test-internal-token-with-32-characters"),
        public_api_base_url="https://api.example.com",
        fullenrich_webhook_token=SecretStr(
            "test-fullenrich-webhook-token-32-characters"
        ),
        smartlead_webhook_token=SecretStr(WEBHOOK_TOKEN),
    )


def _user(*, role: str | None, user_id: str | None = None) -> User:
    metadata: dict = {"provider": "email", "providers": ["email"]}
    if role is not None:
        metadata["role"] = role
    return User(
        id=user_id or str(uuid4()),
        app_metadata=metadata,
        user_metadata={},
        aud="authenticated",
        email="person@example.com",
        created_at=datetime(2026, 8, 16, tzinfo=UTC),
    )


class AuthAdminStub:
    def __init__(self, users: list[User]) -> None:
        self.users = users

    async def get_user_by_id(self, user_id: str) -> UserResponse:
        user = next((item for item in self.users if item.id == user_id), None)
        if user is None:
            raise AuthApiError("User not found", 404, "user_not_found")
        return UserResponse(user=user)


class AuthStub:
    def __init__(self, current_user: User | None, admin_users: list[User]) -> None:
        self.current_user = current_user
        self.admin = AuthAdminStub(admin_users)

    async def get_user(self, jwt: str | None = None) -> UserResponse | None:
        if self.current_user is None:
            raise AuthApiError("bad token", 401, "bad_jwt")
        return UserResponse(user=self.current_user)


class SupabaseStub:
    def __init__(self, auth: AuthStub) -> None:
        self.auth = auth


class SpeedToLeadServiceStub:
    def __init__(self) -> None:
        self.webhook_payloads: list[dict] = []
        self.configure_calls: list[dict] = []
        self.configure_error: Exception | None = None
        self.processed = asyncio.Event()

    async def process_smartlead_webhook(self, payload: dict) -> None:
        self.webhook_payloads.append(payload)
        self.processed.set()

    async def list_events(
        self, *, limit, offset, include_handled=False, visible_to_sdr_id=None
    ):
        self.list_calls = getattr(self, "list_calls", [])
        self.list_calls.append(
            {
                "limit": limit,
                "offset": offset,
                "include_handled": include_handled,
                "visible_to_sdr_id": visible_to_sdr_id,
            }
        )
        now = datetime.now(UTC).isoformat()
        lead_id = str(uuid4())
        run_id = str(uuid4())
        return (
            [
                {
                    "id": str(uuid4()),
                    "platform": "smartlead",
                    "lead_id": lead_id,
                    "smartlead_campaign_id": 10,
                    "heyreach_campaign_id": None,
                    "conversation_id": str(uuid4()),
                    "category_id": 1,
                    "category_name": "Interested",
                    "reply_excerpt": "Sure",
                    "replied_at": now,
                    "dedupe_key": "abc",
                    "enrichment_run_id": run_id,
                    "notification_status": "sent",
                    "notification_error": None,
                    "slack_message_ts": "1.0",
                    "created_at": now,
                    "updated_at": now,
                    "campaign_name": "Campaign",
                    "lead": {
                        "id": lead_id,
                        "email": "pat@example.com",
                        "first_name": "Pat",
                        "last_name": "Lee",
                        "smartlead_phone_number": None,
                        "company_name": "Acme",
                        "location": None,
                        "website": None,
                        "company_url": None,
                        "linkedin_profile": None,
                        "enriched_phone_number": "+14155552671",
                        "phone_source": "prospeo",
                        "status": "new",
                        "notes": None,
                        "positive_conversation_count": 1,
                        "ooo_conversation_count": 0,
                        "latest_reply_at": now,
                        "source_campaigns": [],
                        "assigned_sdr_id": visible_to_sdr_id,
                        "assigned_by": None,
                        "assigned_at": now if visible_to_sdr_id else None,
                        "speed_to_lead_at": now,
                    },
                    "enrichment": {
                        "id": run_id,
                        "status": "succeeded",
                        "selection_mode": "selected",
                        "leads_selected": 1,
                        "leads_enriched": 1,
                        "started_at": now,
                        "completed_at": now,
                        "created_at": now,
                        "updated_at": now,
                    },
                }
            ],
            1,
        )

    async def configure_smartlead_campaign(self, campaign_id, *, enabled, sdr_id):
        if self.configure_error is not None:
            raise self.configure_error
        self.configure_calls.append(
            {"campaign_id": campaign_id, "enabled": enabled, "sdr_id": sdr_id}
        )
        now = datetime.now(UTC).isoformat()
        return {
            "smartlead_campaign_id": campaign_id,
            "name": "Campaign",
            "enabled": True,
            "reply_types": ["positive"],
            "speed_to_lead_enabled": enabled,
            "speed_to_lead_sdr_id": sdr_id,
            "smartlead_webhook_id": "555" if enabled else None,
            "created_at": now,
            "updated_at": now,
        }


def _app(*, actor: User | None, sdrs: list[User] | None = None):
    app = create_app(use_lifespan=False)
    service = SpeedToLeadServiceStub()
    app.dependency_overrides[get_env] = _env
    app.dependency_overrides[get_speed_to_lead_service] = lambda: service
    app.dependency_overrides[get_supabase] = lambda: SupabaseStub(
        AuthStub(actor, sdrs or [])
    )
    return app, service


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_token() -> None:
    app, service = _app(actor=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/smartlead/webhooks/not-the-token", json={"campaign_id": 1}
        )

    assert response.status_code == 401
    assert service.webhook_payloads == []


@pytest.mark.asyncio
async def test_webhook_accepts_token_and_processes_in_background() -> None:
    app, service = _app(actor=None)
    payload = {"event_type": "LEAD_CATEGORY_UPDATED", "campaign_id": 10}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/smartlead/webhooks/{WEBHOOK_TOKEN}", json=payload
        )

    assert response.status_code == 204
    await asyncio.wait_for(service.processed.wait(), timeout=2)
    assert service.webhook_payloads == [payload]


@pytest.mark.asyncio
async def test_opt_in_without_sdr_is_rejected() -> None:
    admin = _user(role="admin")
    app, service = _app(actor=admin)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/api/v1/smartlead/campaigns/10/speed-to-lead",
            json={"enabled": True},
            headers={"Authorization": "Bearer user-jwt"},
        )

    assert response.status_code == 422
    assert service.configure_calls == []


@pytest.mark.asyncio
async def test_opt_in_with_unknown_or_non_sdr_user_is_rejected() -> None:
    admin = _user(role="admin")
    sales_lead = _user(role="sales_lead")
    app, service = _app(actor=admin, sdrs=[sales_lead])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.patch(
            "/api/v1/smartlead/campaigns/10/speed-to-lead",
            json={"enabled": True, "sdr_id": str(uuid4())},
            headers={"Authorization": "Bearer user-jwt"},
        )
        wrong_role = await client.patch(
            "/api/v1/smartlead/campaigns/10/speed-to-lead",
            json={"enabled": True, "sdr_id": sales_lead.id},
            headers={"Authorization": "Bearer user-jwt"},
        )

    assert missing.status_code == 422
    assert wrong_role.status_code == 422
    assert missing.json()["detail"] == "Target user is not an active SDR"
    assert service.configure_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "sales_lead"])
async def test_admin_and_sales_lead_can_enable_and_disable(role: str) -> None:
    actor = _user(role=role)
    sdr = _user(role="sdr")
    app, service = _app(actor=actor, sdrs=[sdr])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        enabled = await client.patch(
            "/api/v1/smartlead/campaigns/10/speed-to-lead",
            json={"enabled": True, "sdr_id": sdr.id},
            headers={"Authorization": "Bearer user-jwt"},
        )
        disabled = await client.patch(
            "/api/v1/smartlead/campaigns/10/speed-to-lead",
            json={"enabled": False},
            headers={"Authorization": "Bearer user-jwt"},
        )

    assert enabled.status_code == 200
    body = enabled.json()
    assert body["speed_to_lead_enabled"] is True
    assert body["speed_to_lead_sdr_id"] == sdr.id
    assert disabled.status_code == 200
    assert disabled.json()["speed_to_lead_enabled"] is False
    assert service.configure_calls == [
        {"campaign_id": 10, "enabled": True, "sdr_id": sdr.id},
        {"campaign_id": 10, "enabled": False, "sdr_id": None},
    ]


@pytest.mark.asyncio
async def test_sdr_cannot_configure_speed_to_lead() -> None:
    sdr = _user(role="sdr")
    app, service = _app(actor=sdr, sdrs=[sdr])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/api/v1/smartlead/campaigns/10/speed-to-lead",
            json={"enabled": True, "sdr_id": sdr.id},
            headers={"Authorization": "Bearer user-jwt"},
        )

    assert response.status_code == 403
    assert service.configure_calls == []


@pytest.mark.asyncio
async def test_unknown_campaign_and_smartlead_failures_map_to_http_errors() -> None:
    admin = _user(role="admin")
    sdr = _user(role="sdr")
    app, service = _app(actor=admin, sdrs=[sdr])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        service.configure_error = SpeedToLeadNotFoundError("missing")
        not_found = await client.patch(
            "/api/v1/smartlead/campaigns/10/speed-to-lead",
            json={"enabled": True, "sdr_id": sdr.id},
            headers={"Authorization": "Bearer user-jwt"},
        )
        service.configure_error = SmartLeadError("down", status_code=500)
        upstream = await client.patch(
            "/api/v1/smartlead/campaigns/10/speed-to-lead",
            json={"enabled": True, "sdr_id": sdr.id},
            headers={"Authorization": "Bearer user-jwt"},
        )

    assert not_found.status_code == 404
    assert upstream.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "sales_lead"])
async def test_managers_list_all_speed_to_lead_events(role: str) -> None:
    actor = _user(role=role)
    app, service = _app(actor=actor)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/speed-to-lead?limit=10&offset=5&include_handled=true",
            headers={"Authorization": "Bearer user-jwt"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["limit"] == 10
    assert body["offset"] == 5
    item = body["items"][0]
    assert item["campaign_name"] == "Campaign"
    assert item["lead"]["enriched_phone_number"] == "+14155552671"
    assert item["lead"]["speed_to_lead_at"] is not None
    assert item["enrichment"]["status"] == "succeeded"
    assert "dedupe_key" in item
    assert service.list_calls == [
        {
            "limit": 10,
            "offset": 5,
            "include_handled": True,
            "visible_to_sdr_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_sdr_list_is_scoped_to_own_leads_and_defaults_to_unhandled() -> None:
    sdr = _user(role="sdr")
    app, service = _app(actor=sdr)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/speed-to-lead", headers={"Authorization": "Bearer user-jwt"}
        )

    assert response.status_code == 200
    assert service.list_calls == [
        {
            "limit": 50,
            "offset": 0,
            "include_handled": False,
            "visible_to_sdr_id": sdr.id,
        }
    ]
    assert response.json()["items"][0]["lead"]["assigned_sdr_id"] == sdr.id


@pytest.mark.asyncio
async def test_speed_to_lead_list_requires_a_lead_role() -> None:
    app, _ = _app(actor=_user(role=None))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unroled = await client.get(
            "/api/v1/speed-to-lead", headers={"Authorization": "Bearer user-jwt"}
        )
        anonymous = await client.get("/api/v1/speed-to-lead")
        too_large = await client.get(
            "/api/v1/speed-to-lead?limit=101",
            headers={"Authorization": "Bearer user-jwt"},
        )

    assert unroled.status_code == 403
    assert anonymous.status_code == 401
    assert too_large.status_code in {403, 422}
