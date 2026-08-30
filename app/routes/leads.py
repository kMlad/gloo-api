from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import require_authenticated_user
from app.dependencies import get_repository, get_smartlead_client
from app.env import Env, get_env
from app.models import (
    LeadDetailResponse,
    LeadListResponse,
    LeadStatus,
    LeadUpdate,
    ReplyType,
)
from app.repositories import Repository
from app.services import LeadService
from app.smartlead.client import SmartLeadClient

router = APIRouter(
    prefix="/api/v1/leads",
    tags=["leads"],
    dependencies=[Depends(require_authenticated_user)],
)

RepositoryDependency = Annotated[Repository, Depends(get_repository)]
SmartLeadDependency = Annotated[SmartLeadClient, Depends(get_smartlead_client)]
EnvDependency = Annotated[Env, Depends(get_env)]


@router.get("", response_model=LeadListResponse)
async def list_leads(
    repository: RepositoryDependency,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    reply_type: ReplyType | None = Query(default=None),
    status: LeadStatus | None = Query(default=None),
) -> dict:
    items, total = await repository.list_leads(
        limit=limit, offset=offset, reply_type=reply_type, status=status
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{lead_id}", response_model=LeadDetailResponse)
async def get_lead(
    lead_id: UUID,
    repository: RepositoryDependency,
    smartlead: SmartLeadDependency,
    env: EnvDependency,
) -> dict:
    result = await LeadService(
        repository,
        smartlead,
        chat_refresh_ttl_seconds=env.smartlead_chat_refresh_ttl_seconds,
    ).get_detail(str(lead_id))
    if result is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return result


@router.patch("/{lead_id}", response_model=dict[str, object])
async def update_lead(
    lead_id: UUID,
    payload: LeadUpdate,
    repository: RepositoryDependency,
) -> dict:
    lead = await repository.update_lead(
        str(lead_id), payload.model_dump(exclude_unset=True)
    )
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead
