from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from supabase_auth.types import User, UserResponse

from app.env import Env, get_env
from app.main import create_app
from app.models import AppRole
from app.supabase_client import get_supabase
from supabase import AuthApiError

INVITE_URL = "/api/v1/users/invites"


def _env(**overrides: Any) -> Env:
    values: dict[str, Any] = {
        "supabase_url": "http://127.0.0.1:54321",
        "supabase_secret_key": SecretStr("secret"),
        "smartlead_api_key": SecretStr("smartlead"),
        "leadmagic_api_key": SecretStr("leadmagic"),
        "prospeo_api_key": SecretStr("prospeo"),
        "airscale_api_key": SecretStr("airscale"),
        "fullenrich_api_key": SecretStr("fullenrich"),
        "internal_api_token": SecretStr("test-internal-token-with-32-characters"),
        "public_api_base_url": "https://api.example.com",
        "fullenrich_webhook_token": SecretStr(
            "test-fullenrich-webhook-token-32-characters"
        ),
    }
    values.update(overrides)
    return Env(**values)


def _user(
    *,
    role: AppRole | None,
    email: str = "inviter@example.com",
    user_id: str | None = None,
    app_metadata: dict[str, Any] | None = None,
    invited_at: datetime | None = None,
) -> User:
    metadata = {"provider": "email", "providers": ["email"]}
    if app_metadata:
        metadata.update(app_metadata)
    if role is not None:
        metadata["role"] = role
    now = datetime(2026, 8, 16, tzinfo=UTC)
    return User(
        id=user_id or str(uuid4()),
        app_metadata=metadata,
        user_metadata={},
        aud="authenticated",
        email=email,
        created_at=now,
        invited_at=invited_at or now,
    )


class AuthAdminStub:
    def __init__(self, invited: User) -> None:
        self.invited = invited
        self.invites: list[tuple[str, dict[str, Any] | None]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.invite_error: AuthApiError | None = None
        self.update_error: AuthApiError | None = None

    async def invite_user_by_email(
        self, email: str, options: dict[str, Any] | None = None
    ) -> UserResponse:
        if self.invite_error is not None:
            raise self.invite_error
        self.invites.append((email, options))
        self.invited.email = email
        return UserResponse(user=self.invited)

    async def update_user_by_id(
        self, uid: str, attributes: dict[str, Any]
    ) -> UserResponse:
        if self.update_error is not None:
            raise self.update_error
        self.updates.append((uid, attributes))
        self.invited.app_metadata = {
            **(self.invited.app_metadata or {}),
            **(attributes.get("app_metadata") or {}),
        }
        return UserResponse(user=self.invited)


class AuthStub:
    def __init__(
        self,
        *,
        current_user: User | None,
        invited: User | None = None,
        get_user_error: AuthApiError | None = None,
    ) -> None:
        self.current_user = current_user
        self.get_user_error = get_user_error
        self.admin = AuthAdminStub(
            invited or _user(role="sdr", email="new@example.com")
        )

    async def get_user(self, jwt: str | None = None) -> UserResponse | None:
        if self.get_user_error is not None:
            raise self.get_user_error
        if self.current_user is None:
            return None
        return UserResponse(user=self.current_user)


class SupabaseStub:
    def __init__(self, auth: AuthStub) -> None:
        self.auth = auth


def _app(supabase: SupabaseStub, env: Env | None = None):
    application = create_app(use_lifespan=False)
    application.dependency_overrides[get_env] = lambda: env or _env()
    application.dependency_overrides[get_supabase] = lambda: supabase
    return application


async def _post(app, *, token: str | None, json: dict[str, Any]):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.post(INVITE_URL, headers=headers, json=json)


@pytest.mark.asyncio
async def test_invite_requires_a_valid_user_jwt() -> None:
    supabase = SupabaseStub(AuthStub(current_user=None))
    app = _app(supabase)

    missing = await _post(
        app, token=None, json={"email": "a@example.com", "role": "sdr"}
    )
    assert missing.status_code == 401

    supabase.auth.get_user_error = AuthApiError("invalid JWT", 401, "bad_jwt")
    invalid = await _post(
        app, token="not-a-jwt", json={"email": "a@example.com", "role": "sdr"}
    )
    assert invalid.status_code == 401

    supabase.auth.get_user_error = None
    supabase.auth.current_user = None
    missing_user = await _post(
        app, token="valid-looking", json={"email": "a@example.com", "role": "sdr"}
    )
    assert missing_user.status_code == 401


@pytest.mark.asyncio
async def test_sdr_cannot_invite_anyone() -> None:
    supabase = SupabaseStub(AuthStub(current_user=_user(role="sdr")))
    app = _app(supabase)
    response = await _post(
        app, token="jwt", json={"email": "new@example.com", "role": "sdr"}
    )
    assert response.status_code == 403
    assert supabase.auth.admin.invites == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "sales_lead"])
async def test_sales_lead_cannot_invite_admin_or_sales_lead(role: str) -> None:
    supabase = SupabaseStub(AuthStub(current_user=_user(role="sales_lead")))
    app = _app(supabase)
    response = await _post(
        app, token="jwt", json={"email": "new@example.com", "role": role}
    )
    assert response.status_code == 403
    assert supabase.auth.admin.invites == []


@pytest.mark.asyncio
async def test_sales_lead_can_invite_sdr() -> None:
    invited = _user(role=None, email="sdr@example.com")
    supabase = SupabaseStub(
        AuthStub(current_user=_user(role="sales_lead"), invited=invited)
    )
    app = _app(supabase)
    response = await _post(
        app, token="jwt", json={"email": "SDR@example.com", "role": "sdr"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "sdr@example.com"
    assert body["role"] == "sdr"
    assert supabase.auth.admin.invites == [("sdr@example.com", None)]
    assert supabase.auth.admin.updates[0][1]["app_metadata"]["role"] == "sdr"
    assert supabase.auth.admin.updates[0][1]["app_metadata"]["provider"] == "email"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "sales_lead", "sdr"])
async def test_admin_can_invite_each_role(role: AppRole) -> None:
    invited = _user(role=None, email=f"{role}@example.com")
    supabase = SupabaseStub(AuthStub(current_user=_user(role="admin"), invited=invited))
    app = _app(supabase)
    response = await _post(
        app, token="jwt", json={"email": f"{role}@example.com", "role": role}
    )
    assert response.status_code == 201
    assert response.json()["role"] == role
    assert supabase.auth.admin.updates[0][1]["app_metadata"]["role"] == role


@pytest.mark.asyncio
async def test_user_without_app_role_cannot_invite() -> None:
    supabase = SupabaseStub(AuthStub(current_user=_user(role=None)))
    app = _app(supabase)
    response = await _post(
        app, token="jwt", json={"email": "new@example.com", "role": "sdr"}
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_invite_maps_auth_errors() -> None:
    supabase = SupabaseStub(AuthStub(current_user=_user(role="admin")))
    app = _app(supabase)

    supabase.auth.admin.invite_error = AuthApiError(
        "User already registered", 422, "email_exists"
    )
    exists = await _post(
        app, token="jwt", json={"email": "dup@example.com", "role": "sdr"}
    )
    assert exists.status_code == 409

    supabase.auth.admin.invite_error = AuthApiError(
        "Unable to validate email address", 400, "email_address_invalid"
    )
    invalid = await _post(
        app, token="jwt", json={"email": "ok@example.com", "role": "sdr"}
    )
    assert invalid.status_code == 400

    supabase.auth.admin.invite_error = AuthApiError(
        "email rate limit exceeded", 429, "over_email_send_rate_limit"
    )
    limited = await _post(
        app, token="jwt", json={"email": "ok@example.com", "role": "sdr"}
    )
    assert limited.status_code == 429


@pytest.mark.asyncio
async def test_invite_returns_500_when_role_assignment_fails() -> None:
    supabase = SupabaseStub(AuthStub(current_user=_user(role="admin")))
    supabase.auth.admin.update_error = AuthApiError("update failed", 500, None)
    app = _app(supabase)
    response = await _post(
        app, token="jwt", json={"email": "new@example.com", "role": "sdr"}
    )
    assert response.status_code == 500
    assert supabase.auth.admin.invites


@pytest.mark.asyncio
async def test_invite_passes_configured_redirect_url() -> None:
    invited = _user(role=None, email="new@example.com")
    supabase = SupabaseStub(AuthStub(current_user=_user(role="admin"), invited=invited))
    app = _app(supabase, _env(invite_redirect_url="http://localhost:5173/welcome"))
    response = await _post(
        app, token="jwt", json={"email": "new@example.com", "role": "sdr"}
    )
    assert response.status_code == 201
    assert supabase.auth.admin.invites == [
        ("new@example.com", {"redirect_to": "http://localhost:5173/welcome"})
    ]


@pytest.mark.asyncio
async def test_invite_rejects_unknown_role() -> None:
    supabase = SupabaseStub(AuthStub(current_user=_user(role="admin")))
    app = _app(supabase)
    response = await _post(
        app, token="jwt", json={"email": "new@example.com", "role": "manager"}
    )
    assert response.status_code == 422
