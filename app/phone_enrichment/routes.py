from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Response,
    status,
)

from app.auth import AuthenticatedUser, require_internal_or_admin_or_sales_lead
from app.dependencies import get_phone_enrichment_service
from app.phone_enrichment.schemas import (
    PhoneEnrichmentRequest,
    PhoneEnrichmentRunResponse,
)
from app.phone_enrichment.service import (
    EnrichmentConflictError,
    EnrichmentNotFoundError,
    EnrichmentValidationError,
    InvalidWebhookError,
    PhoneEnrichmentService,
    ReconciliationTooSoonError,
)

internal_router = APIRouter(
    prefix="/api/v1/phone-enrichments",
    tags=["phone-enrichments"],
)
webhook_router = APIRouter(
    prefix="/api/v1/phone-enrichments/webhooks",
    tags=["phone-enrichment-webhooks"],
)

ServiceDependency = Annotated[
    PhoneEnrichmentService, Depends(get_phone_enrichment_service)
]


@internal_router.post(
    "", response_model=PhoneEnrichmentRunResponse, status_code=status.HTTP_202_ACCEPTED
)
async def create_phone_enrichment(
    service: ServiceDependency,
    actor: Annotated[
        AuthenticatedUser | None, Depends(require_internal_or_admin_or_sales_lead)
    ],
    background_tasks: BackgroundTasks,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
    payload: PhoneEnrichmentRequest | None = None,
) -> dict[str, Any]:
    try:
        run = await service.start(
            payload or PhoneEnrichmentRequest(),
            idempotency_key,
            created_by=actor.id if actor is not None else None,
        )
        if run["status"] == "queued":
            background_tasks.add_task(service.execute_background, str(run["id"]))
        return run
    except EnrichmentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EnrichmentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@internal_router.get(
    "/{run_id}",
    response_model=PhoneEnrichmentRunResponse,
    dependencies=[Depends(require_internal_or_admin_or_sales_lead)],
)
async def get_phone_enrichment(
    run_id: UUID, service: ServiceDependency
) -> dict[str, Any]:
    try:
        return await service.get_run_detail(str(run_id))
    except EnrichmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@internal_router.post(
    "/{run_id}/reconcile",
    response_model=PhoneEnrichmentRunResponse,
    dependencies=[Depends(require_internal_or_admin_or_sales_lead)],
)
async def reconcile_phone_enrichment(
    run_id: UUID, service: ServiceDependency
) -> dict[str, Any]:
    try:
        return await service.reconcile(str(run_id))
    except EnrichmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except EnrichmentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ReconciliationTooSoonError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "300"},
        ) from exc


@webhook_router.post("/fullenrich/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def fullenrich_webhook(
    token: str,
    payload: dict[str, Any],
    service: ServiceDependency,
) -> Response:
    try:
        await service.process_fullenrich_webhook(token, payload)
    except InvalidWebhookError as exc:
        raise HTTPException(status_code=401, detail="Invalid webhook") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
