from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.dependencies import get_repository
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


@pytest.mark.asyncio
async def test_internal_routes_require_a_valid_bearer_token() -> None:
    internal_token = "test-internal-token-with-32-characters"
    app = create_app(use_lifespan=False)
    app.dependency_overrides[get_env] = lambda: Env(
        supabase_url="http://127.0.0.1:54321",
        supabase_secret_key=SecretStr("secret"),
        smartlead_api_key=SecretStr("smartlead"),
        internal_api_token=SecretStr(internal_token),
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
