from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.dependencies import get_phone_enrichment_service, get_repository
from app.env import Env, get_env
from app.main import create_app


class LeadRepositoryStub:
    async def list_leads(self, *, limit: int, offset: int):
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
                    "latest_reply_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
                }
            ],
            1,
        )


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


@pytest.mark.asyncio
async def test_internal_routes_require_a_valid_bearer_token() -> None:
    internal_token = "test-internal-token-with-32-characters"
    app = create_app(use_lifespan=False)
    app.dependency_overrides[get_env] = lambda: Env(
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
    app.dependency_overrides[get_repository] = lambda: LeadRepositoryStub()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        assert (await client.get("/api/v1/leads")).status_code == 401
        invalid_response = await client.get(
            "/api/v1/leads", headers={"Authorization": "Bearer incorrect"}
        )
        assert invalid_response.status_code == 401

        response = await client.get(
            "/api/v1/leads", headers={"Authorization": f"Bearer {internal_token}"}
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["company_name"] == "Acme"


@pytest.mark.asyncio
async def test_phone_enrichment_route_requires_auth_and_idempotency_key() -> None:
    internal_token = "test-internal-token-with-32-characters"
    service = PhoneEnrichmentServiceStub()
    app = create_app(use_lifespan=False)
    app.dependency_overrides[get_env] = lambda: Env(
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
    app.dependency_overrides[get_phone_enrichment_service] = lambda: service
    headers = {"Authorization": f"Bearer {internal_token}"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        assert (await client.post("/api/v1/phone-enrichments", json={})).status_code == 401
        assert (
            await client.post(
                "/api/v1/phone-enrichments", headers=headers, json={}
            )
        ).status_code == 422
        response = await client.post(
            "/api/v1/phone-enrichments",
            headers={**headers, "Idempotency-Key": "manual-run-key"},
            json={"limit": 10},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "succeeded"
    assert service.calls[0][1] == "manual-run-key"
