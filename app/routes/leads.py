from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import (
    AuthenticatedUser,
    parse_app_role,
    require_admin_or_sales_lead,
    require_lead_user,
)
from app.dependencies import get_repository, get_smartlead_client
from app.env import Env, get_env
from app.models import (
    AssignmentStatus,
    LeadAssignmentRecord,
    LeadAssignmentRequest,
    LeadAssignmentResponse,
    LeadAssignmentTarget,
    LeadDetailResponse,
    LeadListResponse,
    LeadStatus,
    LeadUpdate,
    ReplyType,
)
from app.repositories import Repository
from app.services import LeadService
from app.smartlead.client import SmartLeadClient
from app.supabase_client import get_supabase
from supabase import AsyncClient, AuthApiError

router = APIRouter(
    prefix="/api/v1/leads",
    tags=["leads"],
    dependencies=[Depends(require_lead_user)],
)

RepositoryDependency = Annotated[Repository, Depends(get_repository)]
SmartLeadDependency = Annotated[SmartLeadClient, Depends(get_smartlead_client)]
EnvDependency = Annotated[Env, Depends(get_env)]
LeadUserDependency = Annotated[AuthenticatedUser, Depends(require_lead_user)]
ManagerDependency = Annotated[
    AuthenticatedUser, Depends(require_admin_or_sales_lead)
]
SupabaseDependency = Annotated[AsyncClient, Depends(get_supabase)]


async def _validate_sdr(supabase: AsyncClient, sdr_id: UUID) -> None:
    try:
        response = await supabase.auth.admin.get_user_by_id(str(sdr_id))
    except AuthApiError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Target user is not an active SDR",
        ) from error
    user = response.user
    if (
        parse_app_role(user.app_metadata) != "sdr"
        or user.deleted_at is not None
        or user.banned_until is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Target user is not an active SDR",
        )


@router.get("", response_model=LeadListResponse)
async def list_leads(
    repository: RepositoryDependency,
    actor: LeadUserDependency,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    reply_type: ReplyType | None = Query(default=None),
    reply_types: list[ReplyType] | None = Query(default=None),
    status: LeadStatus | None = Query(default=None),
    campaign_id: int | None = Query(default=None, gt=0),
    import_run_id: UUID | None = Query(default=None),
    assignment_status: AssignmentStatus | None = Query(default=None),
    assigned_sdr_id: UUID | None = Query(default=None),
) -> dict:
    if assignment_status == "unassigned" and assigned_sdr_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="assigned_sdr_id cannot be combined with unassigned leads",
        )
    selected_reply_types = list(reply_types or [])
    if reply_type is not None and reply_type not in selected_reply_types:
        selected_reply_types.append(reply_type)
    items, total = await repository.list_leads(
        limit=limit,
        offset=offset,
        reply_types=selected_reply_types or None,
        status=status,
        campaign_id=campaign_id,
        import_run_id=str(import_run_id) if import_run_id is not None else None,
        assignment_status=assignment_status,
        assigned_sdr_id=(
            str(assigned_sdr_id) if assigned_sdr_id is not None else None
        ),
        visible_to_sdr_id=actor.id if actor.role == "sdr" else None,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/assignments", response_model=LeadAssignmentResponse)
async def assign_leads(
    payload: LeadAssignmentRequest,
    repository: RepositoryDependency,
    actor: ManagerDependency,
    supabase: SupabaseDependency,
) -> dict:
    await _validate_sdr(supabase, payload.sdr_id)
    requested_ids = [str(lead_id) for lead_id in payload.lead_ids]
    assigned_ids = await repository.assign_leads(
        requested_ids,
        sdr_id=str(payload.sdr_id),
        assigned_by=actor.id,
    )
    assigned = set(assigned_ids)
    skipped_ids = [lead_id for lead_id in requested_ids if lead_id not in assigned]
    return {
        "sdr_id": str(payload.sdr_id),
        "assigned_lead_ids": assigned_ids,
        "skipped_lead_ids": skipped_ids,
        "assigned_count": len(assigned_ids),
        "skipped_count": len(skipped_ids),
    }


@router.get("/{lead_id}", response_model=LeadDetailResponse)
async def get_lead(
    lead_id: UUID,
    repository: RepositoryDependency,
    smartlead: SmartLeadDependency,
    env: EnvDependency,
    actor: LeadUserDependency,
) -> dict:
    result = await LeadService(
        repository,
        smartlead,
        chat_refresh_ttl_seconds=env.smartlead_chat_refresh_ttl_seconds,
    ).get_detail(
        str(lead_id), assigned_sdr_id=actor.id if actor.role == "sdr" else None
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return result


@router.patch("/{lead_id}", response_model=dict[str, object])
async def update_lead(
    lead_id: UUID,
    payload: LeadUpdate,
    repository: RepositoryDependency,
    actor: LeadUserDependency,
) -> dict:
    lead = await repository.update_lead(
        str(lead_id),
        payload.model_dump(exclude_unset=True),
        assigned_sdr_id=actor.id if actor.role == "sdr" else None,
    )
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


@router.put("/{lead_id}/assignment", response_model=LeadAssignmentRecord)
async def replace_lead_assignment(
    lead_id: UUID,
    payload: LeadAssignmentTarget,
    repository: RepositoryDependency,
    actor: ManagerDependency,
    supabase: SupabaseDependency,
) -> dict:
    await _validate_sdr(supabase, payload.sdr_id)
    assignment = await repository.set_lead_assignment(
        str(lead_id), sdr_id=str(payload.sdr_id), assigned_by=actor.id
    )
    if assignment is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {
        "lead_id": assignment["id"],
        "sdr_id": assignment["assigned_sdr_id"],
        "assigned_by": assignment["assigned_by"],
        "assigned_at": assignment["assigned_at"],
    }


@router.delete("/{lead_id}/assignment", response_model=LeadAssignmentRecord)
async def remove_lead_assignment(
    lead_id: UUID,
    repository: RepositoryDependency,
    actor: ManagerDependency,
) -> dict:
    assignment = await repository.set_lead_assignment(
        str(lead_id), sdr_id=None, assigned_by=actor.id
    )
    if assignment is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {
        "lead_id": assignment["id"],
        "sdr_id": None,
        "assigned_by": None,
        "assigned_at": None,
    }
