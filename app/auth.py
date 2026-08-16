import hmac
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import AsyncClient, AuthApiError, AuthError

from app.env import Env, get_env
from app.models import AppRole
from app.supabase_client import get_supabase

bearer_scheme = HTTPBearer(auto_error=False)


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


async def require_internal_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    env: Env = Depends(get_env),
) -> None:
    expected = env.internal_api_token.get_secret_value()
    supplied = credentials.credentials if credentials else ""
    is_bearer = credentials is not None and credentials.scheme.lower() == "bearer"

    if not is_bearer or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_authenticated_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    supabase: AsyncClient = Depends(get_supabase),
) -> AuthenticatedUser:
    is_bearer = credentials is not None and credentials.scheme.lower() == "bearer"
    token = credentials.credentials if credentials else ""
    if not is_bearer or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        response = await supabase.auth.get_user(token)
    except (AuthApiError, AuthError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    user = response.user if response is not None else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthenticatedUser(
        id=user.id,
        email=user.email,
        role=parse_app_role(user.app_metadata),
        app_metadata=user.app_metadata or {},
    )
