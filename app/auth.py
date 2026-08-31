import hmac
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.env import Env, get_env
from app.models import AppRole
from app.supabase_client import get_supabase
from supabase import AsyncClient, AuthApiError, AuthError

bearer_scheme = HTTPBearer(auto_error=False)
ADMIN_OR_SALES_LEAD_ROLES: frozenset[AppRole] = frozenset({"admin", "sales_lead"})


def _unauthorized_internal() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing internal API token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _unauthorized_user() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing access token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden_role() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to perform this action",
    )


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str | None
    role: AppRole | None
    app_metadata: dict[str, Any]


def parse_app_role(app_metadata: dict[str, Any] | None) -> AppRole | None:
    role = (app_metadata or {}).get("role")
    if role in ("admin", "sales_lead", "sdr"):
        return role
    return None


def _is_internal_bearer(
    credentials: HTTPAuthorizationCredentials | None, expected: str
) -> bool:
    supplied = credentials.credentials if credentials else ""
    is_bearer = credentials is not None and credentials.scheme.lower() == "bearer"
    return is_bearer and hmac.compare_digest(supplied, expected)


async def require_internal_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    env: Env = Depends(get_env),
) -> None:
    expected = env.internal_api_token.get_secret_value()
    if not _is_internal_bearer(credentials, expected):
        raise _unauthorized_internal()


async def authenticate_user(
    credentials: HTTPAuthorizationCredentials | None,
    supabase: AsyncClient,
) -> AuthenticatedUser:
    is_bearer = credentials is not None and credentials.scheme.lower() == "bearer"
    token = credentials.credentials if credentials else ""
    if not is_bearer or not token:
        raise _unauthorized_user()

    try:
        response = await supabase.auth.get_user(token)
    except (AuthApiError, AuthError):
        raise _unauthorized_user() from None

    user = response.user if response is not None else None
    if user is None:
        raise _unauthorized_user()

    return AuthenticatedUser(
        id=user.id,
        email=user.email,
        role=parse_app_role(user.app_metadata),
        app_metadata=user.app_metadata or {},
    )


async def require_authenticated_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    supabase: AsyncClient = Depends(get_supabase),
) -> AuthenticatedUser:
    return await authenticate_user(credentials, supabase)


def require_app_roles(
    *allowed_roles: AppRole,
) -> Callable[..., Coroutine[Any, Any, AuthenticatedUser]]:
    allowed = frozenset(allowed_roles)

    async def dependency(
        user: AuthenticatedUser = Depends(require_authenticated_user),
    ) -> AuthenticatedUser:
        if user.role not in allowed:
            raise _forbidden_role()
        return user

    return dependency


def require_internal_token_or_app_roles(
    *allowed_roles: AppRole,
) -> Callable[..., Coroutine[Any, Any, AuthenticatedUser | None]]:
    allowed = frozenset(allowed_roles)

    async def dependency(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
        env: Env = Depends(get_env),
        supabase: AsyncClient = Depends(get_supabase),
    ) -> AuthenticatedUser | None:
        expected = env.internal_api_token.get_secret_value()
        if _is_internal_bearer(credentials, expected):
            return None

        user = await authenticate_user(credentials, supabase)
        if user.role not in allowed:
            raise _forbidden_role()
        return user

    return dependency


require_admin_or_sales_lead = require_app_roles(*ADMIN_OR_SALES_LEAD_ROLES)
require_lead_user = require_app_roles("admin", "sales_lead", "sdr")
require_internal_or_admin_or_sales_lead = require_internal_token_or_app_roles(
    *ADMIN_OR_SALES_LEAD_ROLES
)
