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
from app.dependencies import get_repository, get_smartlead_client
from app.env import Env, get_env
from app.models import (
    CampaignCreate,
    CampaignResponse,
    CampaignUpdate,
    ImportRequest,
    ImportRunListResponse,
    ImportRunResponse,
    LeadListResponse,
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
)

RepositoryDependency = Annotated[Repository, Depends(get_repository)]
SmartLeadDependency = Annotated[SmartLeadClient, Depends(get_smartlead_client)]


@router.get(
    "/campaigns",
    response_model=list[CampaignResponse],
    dependencies=[Depends(require_internal_or_admin_or_sales_lead)],
)
async def list_campaigns(
    repository: RepositoryDependency,
    smartlead: SmartLeadDependency,
) -> list[dict]:
    try:
        return await CampaignService(repository, smartlead).list()
    except SmartLeadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/campaigns",
    response_model=CampaignResponse,
    dependencies=[Depends(require_internal_token)],
)
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
    "/campaigns/{smartlead_campaign_id}",
    response_model=CampaignResponse,
    dependencies=[Depends(require_internal_token)],
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
        raise HTTPException(
            status_code=404, detail="SmartLead campaign is not configured"
        )
    return campaign


@router.post(
    "/imports",
    response_model=ImportRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_import(
    repository: RepositoryDependency,
    smartlead: SmartLeadDependency,
    env: Annotated[Env, Depends(get_env)],
    actor: Annotated[
        AuthenticatedUser | None, Depends(require_internal_or_admin_or_sales_lead)
    ],
    background_tasks: BackgroundTasks,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
    ],
    payload: ImportRequest | None = None,
) -> dict:
    service = ImportService(
        repository,
        smartlead,
        max_conversations=env.smartlead_import_limit,
    )
    try:
        run = await service.start(
            payload or ImportRequest(),
            idempotency_key=idempotency_key,
            requested_by=actor.id if actor is not None else None,
        )
        if run["status"] == "queued":
            background_tasks.add_task(service.execute_background, str(run["id"]))
        return run
    except ConcurrentImportError as exc:
        raise HTTPException(
            status_code=409, detail="A SmartLead import is already running"
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
    response_model=ImportRunResponse,
    dependencies=[Depends(require_internal_or_admin_or_sales_lead)],
)
async def get_import(run_id: UUID, repository: RepositoryDependency) -> dict:
    result = await repository.get_import_run(str(run_id))
    if result is None:
        raise HTTPException(status_code=404, detail="Import run not found")
    return result


@router.get(
    "/imports",
    response_model=ImportRunListResponse,
    dependencies=[Depends(require_internal_or_admin_or_sales_lead)],
)
async def list_imports(
    repository: RepositoryDependency,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    items, total = await repository.list_import_runs(limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get(
    "/imports/{run_id}/leads",
    response_model=LeadListResponse,
    dependencies=[Depends(require_internal_or_admin_or_sales_lead)],
)
async def list_import_leads(
    run_id: UUID,
    repository: RepositoryDependency,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    if await repository.get_import_run(str(run_id)) is None:
        raise HTTPException(status_code=404, detail="Import run not found")
    items, total = await repository.list_leads(
        limit=limit,
        offset=offset,
        import_run_id=str(run_id),
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}
