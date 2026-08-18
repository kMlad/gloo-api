from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from supabase import AuthApiError
from supabase_auth.types import User, UserResponse

from app.dependencies import get_table_service
from app.env import Env, get_env
from app.main import create_app
from app.models import AppRole
from app.supabase_client import get_supabase
from app.tables.schemas import TableFilter
from app.tables.service import TableService


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


def _user(*, role: AppRole | None = "sdr") -> User:
    metadata: dict[str, Any] = {"provider": "email", "providers": ["email"]}
    if role is not None:
        metadata["role"] = role
    now = datetime(2026, 8, 16, tzinfo=UTC)
    return User(
        id=str(uuid4()),
        app_metadata=metadata,
        user_metadata={},
        aud="authenticated",
        email="person@example.com",
        created_at=now,
    )


class AuthStub:
    def __init__(self, *, current_user: User | None, get_user_error: AuthApiError | None = None) -> None:
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


class TableServiceStub:
    def __init__(self) -> None:
        self.listed = False

    async def list_tables(self) -> dict[str, Any]:
        self.listed = True
        now = datetime.now(UTC).isoformat()
        return {
            "items": [
                {
                    "id": str(uuid4()),
                    "name": "Outbound",
                    "column_count": 2,
                    "row_count": 3,
                    "created_at": now,
                    "updated_at": now,
                }
            ]
        }


def _app(supabase: SupabaseStub, service: TableServiceStub | TableService) -> Any:
    application = create_app(use_lifespan=False)
    application.dependency_overrides[get_env] = _env
    application.dependency_overrides[get_supabase] = lambda: supabase
    application.dependency_overrides[get_table_service] = lambda: service
    return application


@pytest.mark.asyncio
async def test_table_routes_require_a_valid_user_jwt() -> None:
    service = TableServiceStub()
    supabase = SupabaseStub(AuthStub(current_user=None))
    app = _app(supabase, service)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        missing = await client.get("/api/v1/tables")
        assert missing.status_code == 401

        supabase.auth.get_user_error = AuthApiError("invalid JWT", 401, "bad_jwt")
        invalid = await client.get(
            "/api/v1/tables", headers={"Authorization": "Bearer not-a-jwt"}
        )
        assert invalid.status_code == 401

        supabase.auth.get_user_error = None
        supabase.auth.current_user = _user()
        ok = await client.get(
            "/api/v1/tables", headers={"Authorization": "Bearer user-jwt"}
        )
        assert ok.status_code == 200
        assert ok.json()["items"][0]["name"] == "Outbound"

    assert service.listed is True


def test_table_filter_schema_validates_operators() -> None:
    column_id = uuid4()
    assert TableFilter(column_id=column_id, operator="is_empty").value is None
    assert TableFilter(
        column_id=column_id, operator="contains", value="acme"
    ).operator == "contains"
    assert TableFilter(column_id=column_id, operator="eq", value=True).value is True
    with pytest.raises(ValidationError):
        TableFilter(column_id=column_id, operator="is_empty", value="x")
    with pytest.raises(ValidationError):
        TableFilter(column_id=column_id, operator="contains", value="")
    with pytest.raises(ValidationError):
        TableFilter(column_id=column_id, operator="eq")
