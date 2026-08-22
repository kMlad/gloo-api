from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from supabase_auth.types import User, UserResponse

from app.dependencies import get_table_service
from app.env import Env, get_env
from app.main import create_app
from app.supabase_client import get_supabase
from app.tables.sheriff import SheriffUnavailableError
from app.tables.service import TableNotFoundError, TableService, TableValidationError


def _env() -> Env:
    return Env(
        supabase_url="http://127.0.0.1:54321",
        supabase_secret_key=SecretStr("secret"),
        smartlead_api_key=SecretStr("smartlead"),
        leadmagic_api_key=SecretStr("leadmagic"),
        prospeo_api_key=SecretStr("prospeo"),
        airscale_api_key=SecretStr("airscale"),
        fullenrich_api_key=SecretStr("fullenrich"),
        internal_api_token=SecretStr("test-internal-token-with-32-characters"),
        public_api_base_url="https://api.example.com",
        fullenrich_webhook_token=SecretStr(
            "test-fullenrich-webhook-token-32-characters"
        ),
    )


def _user() -> User:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    return User(
        id=str(uuid4()),
        app_metadata={"provider": "email", "providers": ["email"], "role": "sdr"},
        user_metadata={},
        aud="authenticated",
        email="person@example.com",
        created_at=now,
    )


class AuthStub:
    def __init__(self, *, current_user: User) -> None:
        self.current_user = current_user

    async def get_user(self, jwt: str | None = None) -> UserResponse | None:
        return UserResponse(user=self.current_user)


class SupabaseStub:
    def __init__(self) -> None:
        self.auth = AuthStub(current_user=_user())


class SheriffServiceStub:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.fail_unavailable = False
        self.fail_validation: str | None = None
        self.run_id = str(uuid4())
        self.column_id = str(uuid4())
        self.table_id = str(uuid4())

    async def expand_sheriff_prompt(self, table_id: str, payload) -> dict[str, Any]:
        if self.fail_unavailable:
            raise SheriffUnavailableError("Sheriff is not configured")
        if self.fail_validation:
            raise TableValidationError(self.fail_validation)
        return {
            "user_prompt": payload.goal,
            "enhanced_prompt": "Research {{Company}} carefully.",
            "outputs": [{"key": "first_name", "type": "text"}],
            "input_columns": [{"id": str(uuid4()), "name": "Company"}],
        }

    async def get_column(self, table_id: str, column_id: str) -> dict[str, Any]:
        return {
            "id": column_id,
            "table_id": table_id,
            "name": "CEO",
            "type": "sheriff",
        }

    async def start_sheriff_run(
        self, table_id, column_id, payload, *, created_by: str
    ) -> dict[str, Any]:
        if self.fail_unavailable:
            raise SheriffUnavailableError("Sheriff is not configured")
        now = datetime.now(UTC).isoformat()
        return {
            "id": self.run_id,
            "table_id": table_id,
            "column_id": column_id,
            "created_by": created_by,
            "status": "queued",
            "row_ids": [str(item) for item in payload.row_ids or []],
            "overwrite": payload.overwrite,
            "total_count": 1,
            "succeeded_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "not_found_count": 0,
            "items": [
                {
                    "id": str(uuid4()),
                    "row_id": str(uuid4()),
                    "status": "queued",
                    "error_message": None,
                    "model_response": None,
                    "created_at": now,
                    "updated_at": now,
                }
            ],
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }

    async def execute_sheriff_run(self, run_id: str) -> None:
        self.executed.append(run_id)

    async def get_column_run(self, table_id, column_id, run_id) -> dict[str, Any]:
        return await self.get_sheriff_run(table_id, column_id, run_id)

    async def get_sheriff_run(self, table_id, column_id, run_id) -> dict[str, Any]:
        if run_id != self.run_id:
            raise TableNotFoundError("Run not found")
        now = datetime.now(UTC).isoformat()
        return {
            "id": run_id,
            "table_id": table_id,
            "column_id": column_id,
            "created_by": str(uuid4()),
            "status": "succeeded",
            "row_ids": None,
            "overwrite": False,
            "total_count": 1,
            "succeeded_count": 1,
            "failed_count": 0,
            "skipped_count": 0,
            "not_found_count": 0,
            "items": [],
            "created_at": now,
            "updated_at": now,
            "completed_at": now,
        }


def _app(service: SheriffServiceStub | TableService) -> Any:
    application = create_app(use_lifespan=False)
    application.dependency_overrides[get_env] = _env
    application.dependency_overrides[get_supabase] = lambda: SupabaseStub()
    application.dependency_overrides[get_table_service] = lambda: service
    return application


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer user-jwt"}


@pytest.mark.asyncio
async def test_expand_and_run_routes() -> None:
    service = SheriffServiceStub()
    app = _app(service)
    table_id = uuid4()
    column_id = uuid4()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        expanded = await client.post(
            f"/api/v1/tables/{table_id}/sheriff/prompts/expand",
            headers=_headers(),
            json={"goal": "Find the CEO of {{Company}}"},
        )
        assert expanded.status_code == 200
        assert expanded.json()["outputs"][0]["key"] == "first_name"

        started = await client.post(
            f"/api/v1/tables/{table_id}/columns/{column_id}/runs",
            headers=_headers(),
            json={"row_ids": [str(uuid4())]},
        )
        assert started.status_code == 202
        assert started.json()["status"] == "queued"
        assert started.json()["items"][0]["status"] == "queued"

        fetched = await client.get(
            f"/api/v1/tables/{table_id}/columns/{column_id}/runs/{service.run_id}",
            headers=_headers(),
        )
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "succeeded"

    assert service.executed == [service.run_id]


@pytest.mark.asyncio
async def test_expand_unknown_placeholder_and_missing_key() -> None:
    service = SheriffServiceStub()
    app = _app(service)
    table_id = uuid4()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        service.fail_validation = "Unknown column placeholder {{Nope}}"
        unknown = await client.post(
            f"/api/v1/tables/{table_id}/sheriff/prompts/expand",
            headers=_headers(),
            json={"goal": "Find {{Nope}}"},
        )
        assert unknown.status_code == 422

        service.fail_validation = None
        service.fail_unavailable = True
        missing = await client.post(
            f"/api/v1/tables/{table_id}/sheriff/prompts/expand",
            headers=_headers(),
            json={"goal": "Find {{Company}}"},
        )
        assert missing.status_code == 503

        run = await client.post(
            f"/api/v1/tables/{table_id}/columns/{uuid4()}/runs",
            headers=_headers(),
            json={},
        )
        assert run.status_code == 503
