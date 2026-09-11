from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi import (
    status as http_status,
)
from pydantic import ValidationError

from app.auth import (
    AuthenticatedUser,
    require_admin_or_sales_lead,
    require_lead_user,
    validate_active_sdr,
)
from app.dependencies import (
    get_optional_heyreach_client,
    get_repository,
    get_smartlead_client,
)
from app.env import Env, get_env
from app.heyreach.client import HeyReachClient
from app.lead_csv import (
    LeadCsvMapping,
    LeadCsvMappingError,
    import_leads_csv,
    preview_leads_csv,
)
from app.models import (
    AssignmentStatus,
    LeadAssignmentRecord,
    LeadAssignmentRequest,
    LeadAssignmentResponse,
    LeadAssignmentTarget,
    LeadCsvImportResponse,
    LeadCsvPreviewResponse,
    LeadDetailResponse,
    LeadListResponse,
    LeadLocationListResponse,
    LeadPlatform,
    LeadStatus,
    LeadUpdate,
    ReplyType,
)
from app.repositories import MAX_LOCATION_FILTERS, Repository, normalize_locations
from app.services import LeadService
from app.smartlead.client import SmartLeadClient
from app.supabase_client import get_supabase
from app.tables.csv_import import CsvImportError
from supabase import AsyncClient

router = APIRouter(
    prefix="/api/v1/leads",
    tags=["leads"],
    dependencies=[Depends(require_lead_user)],
)

RepositoryDependency = Annotated[Repository, Depends(get_repository)]
SmartLeadDependency = Annotated[SmartLeadClient, Depends(get_smartlead_client)]
HeyReachDependency = Annotated[
    HeyReachClient | None, Depends(get_optional_heyreach_client)
]
EnvDependency = Annotated[Env, Depends(get_env)]
LeadUserDependency = Annotated[AuthenticatedUser, Depends(require_lead_user)]
ManagerDependency = Annotated[
    AuthenticatedUser, Depends(require_admin_or_sales_lead)
]
SupabaseDependency = Annotated[AsyncClient, Depends(get_supabase)]


@router.get("", response_model=LeadListResponse)
async def list_leads(
    repository: RepositoryDependency,
    actor: LeadUserDependency,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    reply_type: ReplyType | None = Query(default=None),
    reply_types: list[ReplyType] | None = Query(default=None),
    status: LeadStatus | None = Query(default=None),
    platform: LeadPlatform | None = Query(default=None),
    campaign_id: int | None = Query(default=None, gt=0),
    heyreach_campaign_id: int | None = Query(default=None, gt=0),
    import_run_id: UUID | None = Query(default=None),
    assignment_status: AssignmentStatus | None = Query(default=None),
    assigned_sdr_id: UUID | None = Query(default=None),
    location: str | None = Query(default=None, max_length=200),
    locations: list[str] | None = Query(default=None, max_length=200),
) -> dict:
    if assignment_status == "unassigned" and assigned_sdr_id is not None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="assigned_sdr_id cannot be combined with unassigned leads",
        )
    if campaign_id is not None and heyreach_campaign_id is not None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="campaign_id and heyreach_campaign_id are mutually exclusive",
        )
    if platform == "smartlead" and heyreach_campaign_id is not None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="platform=smartlead cannot be combined with heyreach_campaign_id",
        )
    if platform == "heyreach" and campaign_id is not None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="platform=heyreach cannot be combined with campaign_id",
        )
    selected_reply_types = list(reply_types or [])
    if reply_type is not None and reply_type not in selected_reply_types:
        selected_reply_types.append(reply_type)
    selected_locations = normalize_locations(
        [*(locations or []), *([location] if location is not None else [])]
    )
    if (
        selected_locations is not None
        and len(selected_locations) > MAX_LOCATION_FILTERS
    ):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"locations may contain at most {MAX_LOCATION_FILTERS} values",
        )
    items, total = await repository.list_leads(
        limit=limit,
        offset=offset,
        reply_types=selected_reply_types or None,
        status=status,
        platform=platform,
        campaign_id=campaign_id,
        heyreach_campaign_id=heyreach_campaign_id,
        import_run_id=str(import_run_id) if import_run_id is not None else None,
        assignment_status=assignment_status,
        assigned_sdr_id=(
            str(assigned_sdr_id) if assigned_sdr_id is not None else None
        ),
        visible_to_sdr_id=actor.id if actor.role == "sdr" else None,
        locations=selected_locations,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/locations", response_model=LeadLocationListResponse)
async def list_lead_locations(
    repository: RepositoryDependency,
    actor: LeadUserDependency,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    items, total = await repository.list_lead_locations(
        query=q,
        limit=limit,
        offset=offset,
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
    await validate_active_sdr(supabase, str(payload.sdr_id))
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


def _csv_http_error(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=str(error),
    )


@router.post("/imports/preview", response_model=LeadCsvPreviewResponse)
async def preview_lead_csv(file: UploadFile = File(...)) -> dict:
    content = await file.read()
    try:
        return preview_leads_csv(content)
    except CsvImportError as error:
        raise _csv_http_error(error) from error


@router.post("/imports", response_model=LeadCsvImportResponse)
async def import_lead_csv(
    repository: RepositoryDependency,
    actor: LeadUserDependency,
    file: UploadFile = File(...),
    mapping: str = Form(...),
) -> dict:
    try:
        parsed_mapping = LeadCsvMapping.model_validate_json(mapping)
    except ValidationError as error:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error.errors(),
        ) from error
    content = await file.read()
    try:
        return await import_leads_csv(
            content=content,
            mapping=parsed_mapping,
            actor=actor,
            repository=repository,
        )
    except (CsvImportError, LeadCsvMappingError) as error:
        raise _csv_http_error(error) from error


@router.get("/{lead_id}", response_model=LeadDetailResponse)
async def get_lead(
    lead_id: UUID,
    repository: RepositoryDependency,
    smartlead: SmartLeadDependency,
    heyreach: HeyReachDependency,
    env: EnvDependency,
    actor: LeadUserDependency,
) -> dict:
    result = await LeadService(
        repository,
        smartlead,
        heyreach=heyreach,
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
    await validate_active_sdr(supabase, str(payload.sdr_id))
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
