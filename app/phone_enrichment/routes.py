from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from app.auth import require_internal_token
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
    dependencies=[Depends(require_internal_token)],
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
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
    payload: PhoneEnrichmentRequest | None = None,
) -> dict[str, Any]:
    try:
        return await service.run(payload or PhoneEnrichmentRequest(), idempotency_key)
    except EnrichmentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EnrichmentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@internal_router.get("/{run_id}", response_model=PhoneEnrichmentRunResponse)
async def get_phone_enrichment(
    run_id: UUID, service: ServiceDependency
) -> dict[str, Any]:
    try:
        return await service.get_run_detail(str(run_id))
    except EnrichmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@internal_router.post(
    "/{run_id}/reconcile", response_model=PhoneEnrichmentRunResponse
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
