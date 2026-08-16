from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import require_internal_token
from app.dependencies import get_repository
from app.models import LeadDetailResponse, LeadListResponse, ReplyType
from app.repositories import Repository

router = APIRouter(
    prefix="/api/v1/leads",
    tags=["leads"],
    dependencies=[Depends(require_internal_token)],
)

RepositoryDependency = Annotated[Repository, Depends(get_repository)]


@router.get("", response_model=LeadListResponse)
async def list_leads(
    repository: RepositoryDependency,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    reply_type: ReplyType | None = Query(default=None),
) -> dict:
    items, total = await repository.list_leads(
        limit=limit, offset=offset, reply_type=reply_type
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{lead_id}", response_model=LeadDetailResponse)
async def get_lead(lead_id: UUID, repository: RepositoryDependency) -> dict:
    result = await repository.get_lead_detail(str(lead_id))
    if result is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return result
