from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import require_internal_token
from app.dependencies import get_repository, get_smartlead_client
from app.env import Env, get_env
from app.models import (
    CampaignCreate,
    CampaignResponse,
    CampaignUpdate,
    ImportRequest,
    ImportRunResponse,
)
from app.repositories import ConcurrentImportError, Repository
from app.services import (
    CampaignService,
    ImportLimitExceeded,
    ImportService,
    ImportValidationError,
)
from app.smartlead.client import SmartLeadClient, SmartLeadError

router = APIRouter(
    prefix="/api/v1/smartlead",
    tags=["smartlead"],
    dependencies=[Depends(require_internal_token)],
)

RepositoryDependency = Annotated[Repository, Depends(get_repository)]
SmartLeadDependency = Annotated[SmartLeadClient, Depends(get_smartlead_client)]


@router.get("/campaigns", response_model=list[CampaignResponse])
async def list_campaigns(repository: RepositoryDependency) -> list[dict]:
    return await CampaignService(repository).list()


@router.post("/campaigns", response_model=CampaignResponse)
async def add_campaign(
    payload: CampaignCreate,
    repository: RepositoryDependency,
    smartlead: SmartLeadDependency,
) -> dict:
    try:
        return await CampaignService(repository, smartlead).add(
            payload.smartlead_campaign_id, payload.enabled, payload.reply_types
        )
    except SmartLeadError as exc:
        upstream_status = (
            status.HTTP_422_UNPROCESSABLE_CONTENT
            if exc.status_code in {400, 404, 422}
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code=upstream_status, detail=str(exc)) from exc


@router.patch(
    "/campaigns/{smartlead_campaign_id}", response_model=CampaignResponse
)
async def update_campaign(
    smartlead_campaign_id: int,
    payload: CampaignUpdate,
    repository: RepositoryDependency,
) -> dict:
    campaign = await CampaignService(repository).update(
        smartlead_campaign_id,
        enabled=payload.enabled,
        reply_types=payload.reply_types,
    )
    if campaign is None:
        raise HTTPException(status_code=404, detail="SmartLead campaign is not configured")
    return campaign


@router.post("/imports", response_model=ImportRunResponse)
async def create_import(
    repository: RepositoryDependency,
    smartlead: SmartLeadDependency,
    env: Annotated[Env, Depends(get_env)],
    payload: ImportRequest | None = None,
) -> dict:
    service = ImportService(
        repository,
        smartlead,
        max_conversations=env.smartlead_import_limit,
    )
    try:
        return await service.run(payload or ImportRequest())
    except ConcurrentImportError as exc:
        raise HTTPException(status_code=409, detail="A SmartLead import is already running") from exc
    except ImportLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": str(exc),
                "run": exc.run,
            },
        ) from exc
    except ImportValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@router.get("/imports/{run_id}", response_model=ImportRunResponse)
async def get_import(run_id: UUID, repository: RepositoryDependency) -> dict:
    result = await repository.get_import_run(str(run_id))
    if result is None:
        raise HTTPException(status_code=404, detail="Import run not found")
    return result
