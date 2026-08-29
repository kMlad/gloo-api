from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from app.dependencies import get_table_service
from app.tables.service import (
    InvalidFullEnrichEmailWebhookError,
    TableService,
)

router = APIRouter(
    prefix="/api/v1/email-enrichments/webhooks",
    tags=["email-enrichment-webhooks"],
)

ServiceDependency = Annotated[TableService, Depends(get_table_service)]


@router.post("/fullenrich", status_code=status.HTTP_204_NO_CONTENT)
async def fullenrich_email_webhook(
    request: Request,
    service: ServiceDependency,
    signature: Annotated[str, Header(alias="X-Signature-SHA1")] = "",
) -> Response:
    raw_body = await request.body()
    try:
        await service.process_fullenrich_email_webhook(raw_body, signature)
    except InvalidFullEnrichEmailWebhookError as error:
        raise HTTPException(status_code=401, detail="Invalid webhook") from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
