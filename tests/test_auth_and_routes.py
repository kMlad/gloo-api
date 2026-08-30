from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from supabase_auth.types import User, UserResponse

from app.dependencies import (
    get_phone_enrichment_service,
    get_repository,
    get_smartlead_client,
)
from app.env import Env, get_env
from app.main import create_app
from app.supabase_client import get_supabase
from supabase import AuthApiError


class LeadRepositoryStub:
    def __init__(self) -> None:
        self.reply_types: list[str | None] = []
        self.detail_ids: list[str] = []

    async def list_leads(self, *, limit: int, offset: int, reply_type=None):
        self.reply_types.append(reply_type)
        return (
            [
                {
                    "id": str(uuid4()),
                    "email": "person@example.com",
                    "first_name": "Pat",
                    "last_name": "Lee",
                    "smartlead_phone_number": None,
                    "company_name": "Acme",
                    "location": "London",
                    "website": "https://acme.example",
                    "company_url": "https://acme.example",
                    "linkedin_profile": None,
                    "enriched_phone_number": None,
                    "phone_source": None,
                    "positive_conversation_count": 1,
                    "ooo_conversation_count": 1 if reply_type == "ooo" else 0,
                    "latest_reply_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
                }
            ],
            1,
        )

    async def get_lead_detail(self, lead_id: str):
        self.detail_ids.append(lead_id)
        if lead_id == "00000000-0000-0000-0000-000000000000":
            return None
        return {
            "lead": {
                "id": lead_id,
                "email": "person@example.com",
                "chat_refreshed_at": datetime.now(UTC).isoformat(),
            },
            "conversations": [
                {
                    "id": str(uuid4()),
                    "smartlead_campaign_id": 10,
                    "smartlead_lead_id": "99",
                    "replies": [
                        {
                            "id": str(uuid4()),
                            "direction": "outbound",
                            "body": "Cold email",
                        },
                        {
                            "id": str(uuid4()),
                            "direction": "inbound",
                            "body": "Sounds good",
                        },
                    ],
                }
            ],
        }

    async def mark_chat_refreshed(self, lead_id: str) -> None:
        return None

    async def upsert_reply(self, values):
        return values


class PhoneEnrichmentServiceStub:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def run(self, payload, idempotency_key):
        self.calls.append((payload, idempotency_key))
        now = datetime.now(UTC).isoformat()
        return {
            "id": str(uuid4()),
            "idempotency_key": idempotency_key,
            "request_fingerprint": "fingerprint",
            "selection_mode": "eligible",
            "requested_lead_ids": [],
            "requested_limit": payload.limit,
            "status": "succeeded",
            "leads_selected": 0,
            "leads_enriched": 0,
            "leads_not_found": 0,
            "leads_skipped": 0,
            "leads_failed": 0,
            "fullenrich_job_id": None,
            "errors": [],
            "last_reconciled_at": None,
            "started_at": now,
            "completed_at": now,
            "created_at": now,
            "updated_at": now,
            "items": [],
        }


class CampaignRepositoryStub:
    def __init__(self) -> None:
        self.campaign = None

    async def upsert_campaign(self, campaign_id, name, enabled, reply_types):
        now = datetime.now(UTC).isoformat()
        self.campaign = {
            "smartlead_campaign_id": campaign_id,
            "name": name,
            "enabled": enabled,
            "reply_types": reply_types,
            "created_at": now,
            "updated_at": now,
        }
        return self.campaign

    async def update_campaign(self, campaign_id, *, enabled, reply_types):
        if (
            self.campaign is None
            or self.campaign["smartlead_campaign_id"] != campaign_id
        ):
            return None
        if enabled is not None:
            self.campaign["enabled"] = enabled
        if reply_types is not None:
            self.campaign["reply_types"] = reply_types
        self.campaign["updated_at"] = datetime.now(UTC).isoformat()
        return self.campaign


class SmartLeadCampaignStub:
    async def get_campaign(self, campaign_id):
        return {"id": campaign_id, "name": "Campaign"}

    async def get_lead_message_history(self, *, campaign_id, lead_id):
        return []


def _env(*, internal_token: str = "test-internal-token-with-32-characters") -> Env:
    return Env(
        supabase_url="http://127.0.0.1:54321",
        supabase_secret_key=SecretStr("secret"),
        smartlead_api_key=SecretStr("smartlead"),
        leadmagic_api_key=SecretStr("leadmagic"),
        prospeo_api_key=SecretStr("prospeo"),
        airscale_api_key=SecretStr("airscale"),
        fullenrich_api_key=SecretStr("fullenrich"),
        internal_api_token=SecretStr(internal_token),
        public_api_base_url="https://api.example.com",
        fullenrich_webhook_token=SecretStr(
            "test-fullenrich-webhook-token-32-characters"
        ),
    )


def _user() -> User:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    return User(
        id=str(uuid4()),
        app_metadata={"provider": "email", "providers": ["email"]},
        user_metadata={},
        aud="authenticated",
        email="person@example.com",
        created_at=now,
    )


class AuthStub:
    def __init__(
        self, *, current_user: User | None, get_user_error: AuthApiError | None = None
    ) -> None:
        self.current_user = current_user
        self.get_user_error = get_user_error

    async def get_user(self, jwt: str | None = None) -> UserResponse | None:
        if self.get_user_error is not None:
            raise self.get_user_error
        if self.current_user is None:
            return None
        return UserResponse(user=self.current_user)


class SupabaseStub:
    def __init__(self, auth: AuthStub) -> None:
        self.auth = auth


@pytest.mark.asyncio
async def test_lead_routes_require_a_valid_user_jwt() -> None:
    app = create_app(use_lifespan=False)
    app.dependency_overrides[get_env] = _env
    repository = LeadRepositoryStub()
    supabase = SupabaseStub(AuthStub(current_user=None))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_smartlead_client] = lambda: SmartLeadCampaignStub()
    app.dependency_overrides[get_supabase] = lambda: supabase
    headers = {"Authorization": "Bearer user-jwt"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        assert (await client.get("/api/v1/leads")).status_code == 401
        supabase.auth.get_user_error = AuthApiError("invalid JWT", 401, "bad_jwt")
        invalid_response = await client.get("/api/v1/leads", headers=headers)
        assert invalid_response.status_code == 401

        supabase.auth.get_user_error = None
        supabase.auth.current_user = _user()
        response = await client.get("/api/v1/leads", headers=headers)
        assert response.status_code == 200
        assert response.json()["items"][0]["company_name"] == "Acme"
        filtered = await client.get("/api/v1/leads?reply_type=ooo", headers=headers)
        invalid_filter = await client.get(
            "/api/v1/leads?reply_type=negative", headers=headers
        )
        lead_id = "11111111-1111-1111-1111-111111111111"
        detail = await client.get(f"/api/v1/leads/{lead_id}", headers=headers)
        missing = await client.get(
            "/api/v1/leads/00000000-0000-0000-0000-000000000000", headers=headers
        )

    assert filtered.status_code == 200
    assert filtered.json()["items"][0]["ooo_conversation_count"] == 1
    assert invalid_filter.status_code == 422
    assert repository.reply_types == [None, "ooo"]
    assert detail.status_code == 200
    assert detail.json()["lead"]["id"] == lead_id
    assert [item["direction"] for item in detail.json()["conversations"][0]["replies"]] == [
        "outbound",
        "inbound",
    ]
    assert missing.status_code == 404
    assert repository.detail_ids == [
        lead_id,
        "00000000-0000-0000-0000-000000000000",
    ]


@pytest.mark.asyncio
async def test_phone_enrichment_route_requires_auth_and_idempotency_key() -> None:
    internal_token = "test-internal-token-with-32-characters"
    service = PhoneEnrichmentServiceStub()
    app = create_app(use_lifespan=False)
    app.dependency_overrides[get_env] = lambda: _env(internal_token=internal_token)
    app.dependency_overrides[get_phone_enrichment_service] = lambda: service
    headers = {"Authorization": f"Bearer {internal_token}"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        assert (
            await client.post("/api/v1/phone-enrichments", json={})
        ).status_code == 401
        assert (
            await client.post("/api/v1/phone-enrichments", headers=headers, json={})
        ).status_code == 422
        response = await client.post(
            "/api/v1/phone-enrichments",
            headers={**headers, "Idempotency-Key": "manual-run-key"},
            json={"limit": 10},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "succeeded"
    assert service.calls[0][1] == "manual-run-key"


@pytest.mark.asyncio
async def test_campaign_routes_persist_reply_type_configuration() -> None:
    internal_token = "test-internal-token-with-32-characters"
    repository = CampaignRepositoryStub()
    app = create_app(use_lifespan=False)
    app.dependency_overrides[get_env] = lambda: _env(internal_token=internal_token)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_smartlead_client] = lambda: SmartLeadCampaignStub()
    headers = {"Authorization": f"Bearer {internal_token}"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/smartlead/campaigns",
            headers=headers,
            json={
                "smartlead_campaign_id": 10,
                "reply_types": ["positive", "ooo"],
            },
        )
        updated = await client.patch(
            "/api/v1/smartlead/campaigns/10",
            headers=headers,
            json={"reply_types": ["ooo"]},
        )
        empty_update = await client.patch(
            "/api/v1/smartlead/campaigns/10", headers=headers, json={}
        )
        duplicate = await client.post(
            "/api/v1/smartlead/campaigns",
            headers=headers,
            json={"smartlead_campaign_id": 11, "reply_types": ["ooo", "ooo"]},
        )

    assert created.status_code == 200
    assert created.json()["reply_types"] == ["positive", "ooo"]
    assert updated.status_code == 200
    assert updated.json()["enabled"] is True
    assert updated.json()["reply_types"] == ["ooo"]
    assert empty_update.status_code == 422
    assert duplicate.status_code == 422
