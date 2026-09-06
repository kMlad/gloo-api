from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    status,
)

from app.auth import (
    AuthenticatedUser,
    require_internal_or_admin_or_sales_lead,
    require_internal_token,
)
from app.dependencies import (
    get_heyreach_client,
    get_heyreach_repository,
    get_repository,
)
from app.env import Env, get_env
from app.heyreach.client import HeyReachClient, HeyReachError
from app.heyreach.repository import HeyReachRepository
from app.heyreach.service import HeyReachCampaignService, HeyReachImportService
from app.models import (
    HeyReachCampaignCreate,
    HeyReachCampaignResponse,
    HeyReachCampaignUpdate,
    HeyReachImportRequest,
    HeyReachImportRunListResponse,
    HeyReachImportRunResponse,
    LeadListResponse,
)
from app.repositories import ConcurrentImportError, Repository
from app.services import ImportLimitExceeded, ImportValidationError

router = APIRouter(
    prefix="/api/v1/heyreach",
    tags=["heyreach"],
)

RepositoryDependency = Annotated[Repository, Depends(get_repository)]
HeyReachRepositoryDependency = Annotated[
    HeyReachRepository, Depends(get_heyreach_repository)
]
HeyReachDependency = Annotated[HeyReachClient, Depends(get_heyreach_client)]


@router.get(
    "/campaigns",
    response_model=list[HeyReachCampaignResponse],
    dependencies=[Depends(require_internal_or_admin_or_sales_lead)],
)
async def list_campaigns(
    repository: HeyReachRepositoryDependency,
    heyreach: HeyReachDependency,
) -> list[dict]:
    try:
        return await HeyReachCampaignService(repository, heyreach).list()
    except HeyReachError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/campaigns",
    response_model=HeyReachCampaignResponse,
    dependencies=[Depends(require_internal_token)],
)
async def add_campaign(
    payload: HeyReachCampaignCreate,
    repository: HeyReachRepositoryDependency,
    heyreach: HeyReachDependency,
) -> dict:
    try:
        return await HeyReachCampaignService(repository, heyreach).add(
            payload.heyreach_campaign_id, payload.enabled
        )
    except HeyReachError as exc:
        upstream_status = (
            status.HTTP_422_UNPROCESSABLE_CONTENT
            if exc.status_code in {400, 404, 422}
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code=upstream_status, detail=str(exc)) from exc


@router.patch(
    "/campaigns/{heyreach_campaign_id}",
    response_model=HeyReachCampaignResponse,
    dependencies=[Depends(require_internal_token)],
)
async def update_campaign(
    heyreach_campaign_id: int,
    payload: HeyReachCampaignUpdate,
    repository: HeyReachRepositoryDependency,
) -> dict:
    campaign = await HeyReachCampaignService(repository).update(
        heyreach_campaign_id,
        enabled=payload.enabled,
    )
    if campaign is None:
        raise HTTPException(
            status_code=404, detail="HeyReach campaign is not configured"
        )
    return campaign


@router.post(
    "/imports",
    response_model=HeyReachImportRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_import(
    repository: HeyReachRepositoryDependency,
    heyreach: HeyReachDependency,
    env: Annotated[Env, Depends(get_env)],
    actor: Annotated[
        AuthenticatedUser | None, Depends(require_internal_or_admin_or_sales_lead)
    ],
    background_tasks: BackgroundTasks,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
    ],
    payload: HeyReachImportRequest | None = None,
) -> dict:
    service = HeyReachImportService(
        repository,
        heyreach,
        max_conversations=env.heyreach_import_limit,
    )
    try:
        run = await service.start(
            payload or HeyReachImportRequest(),
            idempotency_key=idempotency_key,
            requested_by=actor.id if actor is not None else None,
        )
        if run["status"] == "queued":
            background_tasks.add_task(service.execute_background, str(run["id"]))
        return run
    except ConcurrentImportError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "A HeyReach import is already queued or running for an overlapping "
                "campaign"
            ),
        ) from exc
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


@router.get(
    "/imports/{run_id}",
    response_model=HeyReachImportRunResponse,
    dependencies=[Depends(require_internal_or_admin_or_sales_lead)],
)
async def get_import(
    run_id: UUID, repository: HeyReachRepositoryDependency
) -> dict:
    result = await repository.get_import_run(str(run_id))
    if result is None:
        raise HTTPException(status_code=404, detail="Import run not found")
    await repository.attach_last_enrichments([result])
    return result


@router.get(
    "/imports",
    response_model=HeyReachImportRunListResponse,
    dependencies=[Depends(require_internal_or_admin_or_sales_lead)],
)
async def list_imports(
    repository: HeyReachRepositoryDependency,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    items, total = await repository.list_import_runs(limit=limit, offset=offset)
    await repository.attach_last_enrichments(items)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get(
    "/imports/{run_id}/leads",
    response_model=LeadListResponse,
    dependencies=[Depends(require_internal_or_admin_or_sales_lead)],
)
async def list_import_leads(
    run_id: UUID,
    repository: HeyReachRepositoryDependency,
    leads: RepositoryDependency,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    if await repository.get_import_run(str(run_id)) is None:
        raise HTTPException(status_code=404, detail="Import run not found")
    items, total = await leads.list_leads(
        limit=limit,
        offset=offset,
        import_run_id=str(run_id),
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}
