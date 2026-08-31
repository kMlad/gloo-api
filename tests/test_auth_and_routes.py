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
        self.statuses: list[str | None] = []
        self.detail_ids: list[str] = []
        self.detail_scopes: list[str | None] = []
        self.updated_leads: list[tuple[str, dict]] = []
        self.update_scopes: list[str | None] = []
        self.list_scopes: list[dict] = []
        self.assignment_calls: list[tuple[list[str], str, str]] = []
        self.assignment_updates: list[tuple[str, str | None, str]] = []
        self.assignable_ids: set[str] | None = None

    async def list_leads(
        self,
        *,
        limit: int,
        offset: int,
        reply_types=None,
        status=None,
        campaign_id=None,
        import_run_id=None,
        assignment_status=None,
        assigned_sdr_id=None,
        visible_to_sdr_id=None,
    ):
        reply_type = reply_types[0] if reply_types else None
        self.reply_types.append(reply_type)
        self.statuses.append(status)
        self.list_scopes.append(
            {
                "campaign_id": campaign_id,
                "import_run_id": import_run_id,
                "assignment_status": assignment_status,
                "assigned_sdr_id": assigned_sdr_id,
                "visible_to_sdr_id": visible_to_sdr_id,
            }
        )
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
                    "status": status or "new",
                    "notes": None,
                    "positive_conversation_count": 1,
                    "ooo_conversation_count": 1 if reply_type == "ooo" else 0,
                    "latest_reply_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
                }
            ],
            1,
        )

    async def get_lead_detail(
        self, lead_id: str, *, assigned_sdr_id: str | None = None
    ):
        self.detail_ids.append(lead_id)
        self.detail_scopes.append(assigned_sdr_id)
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

    async def update_lead(
        self, lead_id: str, values: dict, *, assigned_sdr_id: str | None = None
    ):
        self.updated_leads.append((lead_id, values))
        self.update_scopes.append(assigned_sdr_id)
        if lead_id == "00000000-0000-0000-0000-000000000000":
            return None
        return {
            "id": lead_id,
            "email": "person@example.com",
            "status": values.get("status", "new"),
            "notes": values.get("notes"),
        }

    async def upsert_reply(self, values):
        return values

    async def assign_leads(self, lead_ids, *, sdr_id, assigned_by):
        self.assignment_calls.append((lead_ids, sdr_id, assigned_by))
        if self.assignable_ids is None:
            return lead_ids
        return [lead_id for lead_id in lead_ids if lead_id in self.assignable_ids]

    async def set_lead_assignment(self, lead_id, *, sdr_id, assigned_by):
        self.assignment_updates.append((lead_id, sdr_id, assigned_by))
        if lead_id == "00000000-0000-0000-0000-000000000000":
            return None
        now = datetime.now(UTC).isoformat() if sdr_id is not None else None
        return {
            "id": lead_id,
            "assigned_sdr_id": sdr_id,
            "assigned_by": assigned_by if sdr_id is not None else None,
            "assigned_at": now,
        }


class PhoneEnrichmentServiceStub:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def start(self, payload, idempotency_key, *, created_by=None):
        self.calls.append((payload, idempotency_key))
        now = datetime.now(UTC).isoformat()
        return {
            "id": str(uuid4()),
            "idempotency_key": idempotency_key,
            "request_fingerprint": "fingerprint",
            "selection_mode": "eligible",
            "requested_lead_ids": [],
            "source_import_run_id": None,
            "created_by": created_by,
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

    async def get_run_detail(self, run_id: str):
        now = datetime.now(UTC).isoformat()
        return {
            "id": run_id,
            "idempotency_key": "manual-run-key",
            "request_fingerprint": "fingerprint",
            "selection_mode": "eligible",
            "requested_lead_ids": [],
            "source_import_run_id": None,
            "created_by": None,
            "requested_limit": 10,
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

    async def reconcile(self, run_id: str):
        return await self.get_run_detail(run_id)


class CampaignRepositoryStub:
    def __init__(self) -> None:
        self.campaign = None
        self.import_run = None

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

    async def list_campaigns(self, *, enabled_only: bool = False):
        if self.campaign is None:
            return []
        if enabled_only and not self.campaign["enabled"]:
            return []
        return [self.campaign]

    async def sync_campaign_catalog(self, campaigns):
        return campaigns

    async def get_campaign_import_stats(self, campaign_ids):
        return {}

    async def get_campaigns_by_ids(self, campaign_ids):
        if self.campaign is None:
            return []
        return (
            [self.campaign]
            if self.campaign["smartlead_campaign_id"] in campaign_ids
            else []
        )

    async def get_import_run_by_idempotency_key(self, key):
        if self.import_run and self.import_run["idempotency_key"] == key:
            return self.import_run
        return None

    async def create_import_run(self, **values):
        now = datetime.now(UTC).isoformat()
        self.import_run = {
            "id": str(uuid4()),
            "status": "succeeded",
            **values,
            "resolved_categories": {},
            "qualifying_conversation_count": 0,
            "leads_processed": 0,
            "conversations_processed": 0,
            "replies_processed": 0,
            "errors": [],
            "started_at": now,
            "completed_at": now,
        }
        return self.import_run


class SmartLeadCampaignStub:
    async def get_campaign(self, campaign_id):
        return {"id": campaign_id, "name": "Campaign"}

    async def list_campaigns(self):
        return []

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


def _user(
    *,
    role: str | None = None,
    user_id: str | None = None,
    email: str = "person@example.com",
) -> User:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    metadata = {"provider": "email", "providers": ["email"]}
    if role is not None:
        metadata["role"] = role
    return User(
        id=user_id or str(uuid4()),
        app_metadata=metadata,
        user_metadata={},
        aud="authenticated",
        email=email,
        created_at=now,
    )


class AuthAdminStub:
    def __init__(self, users: list[User] | None = None) -> None:
        self.users = users or []

    async def get_user_by_id(self, user_id: str) -> UserResponse:
        user = next((item for item in self.users if item.id == user_id), None)
        if user is None:
            raise AuthApiError("User not found", 404, "user_not_found")
        return UserResponse(user=user)

    async def list_users(self, page=None, per_page=None):
        return self.users


class AuthStub:
    def __init__(
        self,
        *,
        current_user: User | None,
        get_user_error: AuthApiError | None = None,
        admin_users: list[User] | None = None,
    ) -> None:
        self.current_user = current_user
        self.get_user_error = get_user_error
        self.admin = AuthAdminStub(admin_users)

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
        supabase.auth.current_user = _user(role="sales_lead")
        response = await client.get("/api/v1/leads", headers=headers)
        assert response.status_code == 200
        assert response.json()["items"][0]["company_name"] == "Acme"
        filtered = await client.get("/api/v1/leads?reply_type=ooo", headers=headers)
        status_filtered = await client.get(
            "/api/v1/leads?status=needs_follow_up", headers=headers
        )
        invalid_filter = await client.get(
            "/api/v1/leads?reply_type=negative", headers=headers
        )
        invalid_status = await client.get(
            "/api/v1/leads?status=contacted", headers=headers
        )
        lead_id = "11111111-1111-1111-1111-111111111111"
        detail = await client.get(f"/api/v1/leads/{lead_id}", headers=headers)
        missing = await client.get(
            "/api/v1/leads/00000000-0000-0000-0000-000000000000", headers=headers
        )
        updated = await client.patch(
            f"/api/v1/leads/{lead_id}",
            headers=headers,
            json={"status": "needs_follow_up", "notes": "Call again Tuesday"},
        )
        missing_update = await client.patch(
            "/api/v1/leads/00000000-0000-0000-0000-000000000000",
            headers=headers,
            json={"status": "attempted"},
        )
        empty_update = await client.patch(
            f"/api/v1/leads/{lead_id}", headers=headers, json={}
        )

    assert filtered.status_code == 200
    assert filtered.json()["items"][0]["ooo_conversation_count"] == 1
    assert invalid_filter.status_code == 422
    assert status_filtered.status_code == 200
    assert status_filtered.json()["items"][0]["status"] == "needs_follow_up"
    assert invalid_status.status_code == 422
    assert repository.reply_types == [None, "ooo", None]
    assert repository.statuses == [None, None, "needs_follow_up"]
    assert detail.status_code == 200
    assert detail.json()["lead"]["id"] == lead_id
    assert [item["direction"] for item in detail.json()["conversations"][0]["replies"]] == [
        "outbound",
        "inbound",
    ]
    assert missing.status_code == 404
    assert updated.status_code == 200
    assert updated.json()["status"] == "needs_follow_up"
    assert updated.json()["notes"] == "Call again Tuesday"
    assert missing_update.status_code == 404
    assert empty_update.status_code == 422
    assert repository.updated_leads == [
        (
            lead_id,
            {"status": "needs_follow_up", "notes": "Call again Tuesday"},
        ),
        (
            "00000000-0000-0000-0000-000000000000",
            {"status": "attempted"},
        ),
    ]
    assert repository.detail_ids == [
        lead_id,
        "00000000-0000-0000-0000-000000000000",
    ]
    assert repository.detail_scopes == [None, None]
    assert repository.update_scopes == [None, None]


@pytest.mark.asyncio
async def test_sales_lead_filters_and_assigns_exact_unassigned_leads() -> None:
    manager_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    sdr_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    lead_ids = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    repository = LeadRepositoryStub()
    repository.assignable_ids = {lead_ids[0]}
    auth = AuthStub(
        current_user=_user(role="sales_lead", user_id=manager_id),
        admin_users=[_user(role="sdr", user_id=sdr_id, email="sdr@example.com")],
    )
    app = create_app(use_lifespan=False)
    app.dependency_overrides[get_env] = _env
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_smartlead_client] = lambda: SmartLeadCampaignStub()
    app.dependency_overrides[get_supabase] = lambda: SupabaseStub(auth)
    headers = {"Authorization": "Bearer user-jwt"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        candidates = await client.get(
            "/api/v1/leads"
            "?reply_type=positive&campaign_id=10&assignment_status=unassigned",
            headers=headers,
        )
        assigned = await client.post(
            "/api/v1/leads/assignments",
            headers=headers,
            json={"lead_ids": lead_ids, "sdr_id": sdr_id},
        )
        reassigned = await client.put(
            f"/api/v1/leads/{lead_ids[0]}/assignment",
            headers=headers,
            json={"sdr_id": sdr_id},
        )
        unassigned = await client.delete(
            f"/api/v1/leads/{lead_ids[0]}/assignment",
            headers=headers,
        )

    assert candidates.status_code == 200
    assert repository.list_scopes[0] == {
        "campaign_id": 10,
        "import_run_id": None,
        "assignment_status": "unassigned",
        "assigned_sdr_id": None,
        "visible_to_sdr_id": None,
    }
    assert assigned.status_code == 200
    assert assigned.json() == {
        "sdr_id": sdr_id,
        "assigned_lead_ids": [lead_ids[0]],
        "skipped_lead_ids": [lead_ids[1]],
        "assigned_count": 1,
        "skipped_count": 1,
    }
    assert repository.assignment_calls == [(lead_ids, sdr_id, manager_id)]
    assert reassigned.status_code == 200
    assert reassigned.json()["sdr_id"] == sdr_id
    assert unassigned.status_code == 200
    assert unassigned.json()["sdr_id"] is None
    assert repository.assignment_updates == [
        (lead_ids[0], sdr_id, manager_id),
        (lead_ids[0], None, manager_id),
    ]


@pytest.mark.asyncio
async def test_sdr_lead_access_is_owner_scoped_and_assignment_is_forbidden() -> None:
    sdr_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    lead_id = "11111111-1111-1111-1111-111111111111"
    repository = LeadRepositoryStub()
    app = create_app(use_lifespan=False)
    app.dependency_overrides[get_env] = _env
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_smartlead_client] = lambda: SmartLeadCampaignStub()
    app.dependency_overrides[get_supabase] = lambda: SupabaseStub(
        AuthStub(current_user=_user(role="sdr", user_id=sdr_id))
    )
    headers = {"Authorization": "Bearer user-jwt"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        listed = await client.get("/api/v1/leads", headers=headers)
        detail = await client.get(f"/api/v1/leads/{lead_id}", headers=headers)
        missing = await client.get(
            "/api/v1/leads/00000000-0000-0000-0000-000000000000",
            headers=headers,
        )
        updated = await client.patch(
            f"/api/v1/leads/{lead_id}",
            headers=headers,
            json={"status": "attempted", "notes": "Left voicemail"},
        )
        assign = await client.post(
            "/api/v1/leads/assignments",
            headers=headers,
            json={"lead_ids": [lead_id], "sdr_id": sdr_id},
        )

    assert listed.status_code == 200
    assert detail.status_code == 200
    assert missing.status_code == 404
    assert updated.status_code == 200
    assert assign.status_code == 403
    assert repository.list_scopes[0]["visible_to_sdr_id"] == sdr_id
    assert repository.detail_scopes == [sdr_id, sdr_id]
    assert repository.update_scopes == [sdr_id]


@pytest.mark.asyncio
async def test_unroled_user_cannot_discover_leads() -> None:
    app = create_app(use_lifespan=False)
    app.dependency_overrides[get_env] = _env
    app.dependency_overrides[get_repository] = LeadRepositoryStub
    app.dependency_overrides[get_supabase] = lambda: SupabaseStub(
        AuthStub(current_user=_user(role=None))
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/v1/leads", headers={"Authorization": "Bearer user-jwt"}
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_phone_enrichment_route_requires_auth_and_idempotency_key() -> None:
    internal_token = "test-internal-token-with-32-characters"
    service = PhoneEnrichmentServiceStub()
    app = create_app(use_lifespan=False)
    app.dependency_overrides[get_env] = lambda: _env(internal_token=internal_token)
    app.dependency_overrides[get_phone_enrichment_service] = lambda: service
    app.dependency_overrides[get_supabase] = lambda: SupabaseStub(
        AuthStub(current_user=None)
    )
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


def _dual_auth_app(
    *,
    current_user: User | None = None,
    repository: CampaignRepositoryStub | None = None,
    service: PhoneEnrichmentServiceStub | None = None,
):
    app = create_app(use_lifespan=False)
    app.dependency_overrides[get_env] = _env
    app.dependency_overrides[get_supabase] = lambda: SupabaseStub(
        AuthStub(current_user=current_user)
    )
    if repository is not None:
        app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_smartlead_client] = lambda: SmartLeadCampaignStub()
    if service is not None:
        app.dependency_overrides[get_phone_enrichment_service] = lambda: service
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "sales_lead"])
async def test_admin_and_sales_lead_can_list_campaigns_and_enrich_phones(
    role: str,
) -> None:
    repository = CampaignRepositoryStub()
    now = datetime.now(UTC).isoformat()
    repository.campaign = {
        "smartlead_campaign_id": 10,
        "name": "Campaign",
        "enabled": True,
        "reply_types": ["positive"],
        "created_at": now,
        "updated_at": now,
    }
    service = PhoneEnrichmentServiceStub()
    app = _dual_auth_app(
        current_user=_user(role=role), repository=repository, service=service
    )
    headers = {"Authorization": "Bearer user-jwt"}
    run_id = "11111111-1111-1111-1111-111111111111"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        campaigns = await client.get("/api/v1/smartlead/campaigns", headers=headers)
        create = await client.post(
            "/api/v1/phone-enrichments",
            headers={**headers, "Idempotency-Key": "manual-run-key"},
            json={"limit": 10},
        )
        detail = await client.get(f"/api/v1/phone-enrichments/{run_id}", headers=headers)
        reconcile = await client.post(
            f"/api/v1/phone-enrichments/{run_id}/reconcile", headers=headers
        )
        imported = await client.post(
            "/api/v1/smartlead/imports",
            headers={**headers, "Idempotency-Key": "sales-import-key"},
            json={"campaign_ids": [10], "reply_types": ["positive", "ooo"]},
        )
        write_campaign = await client.post(
            "/api/v1/smartlead/campaigns",
            headers=headers,
            json={"smartlead_campaign_id": 11},
        )

    assert campaigns.status_code == 200
    assert campaigns.json()[0]["smartlead_campaign_id"] == 10
    assert create.status_code == 202
    assert detail.status_code == 200
    assert reconcile.status_code == 200
    assert imported.status_code == 202
    assert imported.json()["reply_types"] == ["positive", "ooo"]
    assert write_campaign.status_code == 401
    assert service.calls[0][1] == "manual-run-key"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["sdr", None])
async def test_sdr_and_unroled_users_cannot_list_campaigns_or_enrich_phones(
    role: str | None,
) -> None:
    app = _dual_auth_app(
        current_user=_user(role=role),
        repository=CampaignRepositoryStub(),
        service=PhoneEnrichmentServiceStub(),
    )
    headers = {"Authorization": "Bearer user-jwt"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        campaigns = await client.get("/api/v1/smartlead/campaigns", headers=headers)
        enrich = await client.post(
            "/api/v1/phone-enrichments",
            headers={**headers, "Idempotency-Key": "manual-run-key"},
            json={"limit": 10},
        )
        imported = await client.post(
            "/api/v1/smartlead/imports",
            headers={**headers, "Idempotency-Key": "sales-import-key"},
            json={"campaign_ids": [10], "reply_types": ["positive"]},
        )

    assert campaigns.status_code == 403
    assert enrich.status_code == 403
    assert imported.status_code == 403


@pytest.mark.asyncio
async def test_internal_token_can_still_list_campaigns() -> None:
    internal_token = "test-internal-token-with-32-characters"
    repository = CampaignRepositoryStub()
    app = _dual_auth_app(repository=repository)
    headers = {"Authorization": f"Bearer {internal_token}"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        unauthenticated = await client.get("/api/v1/smartlead/campaigns")
        response = await client.get("/api/v1/smartlead/campaigns", headers=headers)

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.json() == []
