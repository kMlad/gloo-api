from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import AsyncClient, AuthApiError

from app.auth import AuthenticatedUser, require_authenticated_user
from app.env import Env, get_env
from app.models import AppRole, InviteUserRequest, InviteUserResponse
from app.supabase_client import get_supabase
from app.utils import normalize_email, utc_now

router = APIRouter(prefix="/api/v1/users", tags=["users"])

INVITE_PERMISSIONS: dict[AppRole, frozenset[AppRole]] = {
    "admin": frozenset({"admin", "sales_lead", "sdr"}),
    "sales_lead": frozenset({"sdr"}),
    "sdr": frozenset(),
}

AuthenticatedUserDependency = Annotated[
    AuthenticatedUser, Depends(require_authenticated_user)
]
SupabaseDependency = Annotated[AsyncClient, Depends(get_supabase)]
EnvDependency = Annotated[Env, Depends(get_env)]


def can_invite(inviter_role: AppRole | None, invited_role: AppRole) -> bool:
    if inviter_role is None:
        return False
    return invited_role in INVITE_PERMISSIONS.get(inviter_role, frozenset())


def _http_status_for_auth_error(error: AuthApiError) -> int:
    if error.code in {"email_exists", "user_already_exists", "conflict"}:
        return status.HTTP_409_CONFLICT
    if error.code in {"over_request_rate_limit", "over_email_send_rate_limit"}:
        return status.HTTP_429_TOO_MANY_REQUESTS
    if error.code in {"email_address_invalid", "validation_failed", "bad_json"}:
        return status.HTTP_400_BAD_REQUEST
    if error.status in {400, 401, 403, 404, 409, 422, 429}:
        return error.status
    return status.HTTP_400_BAD_REQUEST


@router.post(
    "/invites",
    response_model=InviteUserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def invite_user(
    payload: InviteUserRequest,
    inviter: AuthenticatedUserDependency,
    supabase: SupabaseDependency,
    env: EnvDependency,
) -> dict:
    if not can_invite(inviter.role, payload.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to invite this role",
        )

    email = normalize_email(str(payload.email))
    invite_options = (
        {"redirect_to": env.invite_redirect_url} if env.invite_redirect_url else None
    )
    try:
        invited = await supabase.auth.admin.invite_user_by_email(email, invite_options)
    except AuthApiError as error:
        raise HTTPException(
            status_code=_http_status_for_auth_error(error),
            detail=error.message,
        ) from error

    user = invited.user
    app_metadata = dict(user.app_metadata or {})
    app_metadata["role"] = payload.role
    try:
        updated = await supabase.auth.admin.update_user_by_id(
            user.id,
            {"app_metadata": app_metadata},
        )
    except AuthApiError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="User was invited but role could not be assigned",
        ) from error

    assigned = updated.user
    invited_at = assigned.invited_at or assigned.created_at or utc_now()
    return {
        "id": assigned.id,
        "email": assigned.email or email,
        "role": payload.role,
        "invited_at": invited_at,
    }
