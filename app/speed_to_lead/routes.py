import hmac
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)

from app.auth import (
    AuthenticatedUser,
    require_internal_or_admin_or_sales_lead,
    require_lead_user,
    validate_active_sdr,
)
from app.dependencies import get_speed_to_lead_service
from app.env import Env, get_env
from app.models import CampaignResponse
from app.smartlead.client import SmartLeadError
from app.speed_to_lead.schemas import (
    SpeedToLeadCampaignUpdate,
    SpeedToLeadListResponse,
)
from app.speed_to_lead.service import (
    SpeedToLeadNotFoundError,
    SpeedToLeadService,
    SpeedToLeadValidationError,
)
from app.supabase_client import get_supabase
from supabase import AsyncClient

router = APIRouter(prefix="/api/v1/smartlead", tags=["speed-to-lead"])
list_router = APIRouter(prefix="/api/v1/speed-to-lead", tags=["speed-to-lead"])

ServiceDependency = Annotated[SpeedToLeadService, Depends(get_speed_to_lead_service)]
EnvDependency = Annotated[Env, Depends(get_env)]
SupabaseDependency = Annotated[AsyncClient, Depends(get_supabase)]


@router.post("/webhooks/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def smartlead_webhook(
    token: str,
    payload: dict[str, Any],
    service: ServiceDependency,
    env: EnvDependency,
    background_tasks: BackgroundTasks,
) -> Response:
    expected = env.smartlead_webhook_token.get_secret_value()
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook")
    background_tasks.add_task(service.process_smartlead_webhook, payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/campaigns/{smartlead_campaign_id}/speed-to-lead",
    response_model=CampaignResponse,
)
async def update_speed_to_lead(
    smartlead_campaign_id: int,
    payload: SpeedToLeadCampaignUpdate,
    service: ServiceDependency,
    supabase: SupabaseDependency,
    _actor: Annotated[
        AuthenticatedUser | None, Depends(require_internal_or_admin_or_sales_lead)
    ],
) -> dict[str, Any]:
    if payload.sdr_id is not None:
        await validate_active_sdr(supabase, str(payload.sdr_id))
    try:
        return await service.configure_smartlead_campaign(
            smartlead_campaign_id,
            enabled=payload.enabled,
            sdr_id=str(payload.sdr_id) if payload.sdr_id is not None else None,
        )
    except SpeedToLeadNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SpeedToLeadValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except SmartLeadError as exc:
        upstream_status = (
            status.HTTP_422_UNPROCESSABLE_CONTENT
            if exc.status_code in {400, 404, 422}
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code=upstream_status, detail=str(exc)) from exc


@list_router.get("", response_model=SpeedToLeadListResponse)
async def list_speed_to_lead_events(
    service: ServiceDependency,
    actor: Annotated[AuthenticatedUser, Depends(require_lead_user)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    include_handled: bool = Query(default=False),
) -> dict[str, Any]:
    items, total = await service.list_events(
        limit=limit,
        offset=offset,
        include_handled=include_handled,
        visible_to_sdr_id=actor.id if actor.role == "sdr" else None,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}
