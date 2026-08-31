from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

PhoneProvider = Literal[
    "smartlead_signature",
    "leadmagic",
    "prospeo",
    "airscale",
    "fullenrich",
]
RunStatus = Literal["queued", "running", "waiting", "succeeded", "partial", "failed"]
ItemStatus = Literal[
    "queued",
    "running",
    "waiting",
    "enriched",
    "not_found",
    "skipped_existing",
    "skipped_active",
    "failed",
]
AttemptStatus = Literal[
    "pending",
    "in_progress",
    "waiting",
    "found",
    "not_found",
    "skipped_no_input",
    "rate_limited",
    "timed_out",
    "failed",
]


class PhoneEnrichmentRequest(BaseModel):
    lead_ids: list[UUID] | None = None
    source_import_run_id: UUID | None = None
    limit: int = Field(default=25, ge=1, le=100)

    @model_validator(mode="after")
    def validate_lead_ids(self) -> "PhoneEnrichmentRequest":
        if self.lead_ids is not None and self.source_import_run_id is not None:
            raise ValueError(
                "lead_ids and source_import_run_id are mutually exclusive"
            )
        if self.lead_ids is not None:
            if not self.lead_ids:
                raise ValueError("lead_ids must not be empty")
            if len(self.lead_ids) > 100:
                raise ValueError("lead_ids may contain at most 100 values")
            if len(set(self.lead_ids)) != len(self.lead_ids):
                raise ValueError("lead_ids must not contain duplicates")
        return self


class PhoneEnrichmentAttemptResponse(BaseModel):
    id: UUID
    run_id: UUID
    item_id: UUID
    lead_id: UUID
    provider: PhoneProvider
    sequence: int
    status: AttemptStatus
    request_payload: dict[str, Any]
    response_payload: Any | None
    response_headers: dict[str, Any]
    http_status: int | None
    external_request_id: str | None
    phone_candidate: str | None
    error_code: str | None
    error_message: str | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PhoneEnrichmentItemResponse(BaseModel):
    id: UUID
    run_id: UUID
    lead_id: UUID
    status: ItemStatus
    final_phone_number: str | None
    final_source: PhoneProvider | None
    had_provider_error: bool
    error_message: str | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    attempts: list[PhoneEnrichmentAttemptResponse] = Field(default_factory=list)


class PhoneEnrichmentRunResponse(BaseModel):
    id: UUID
    idempotency_key: str
    request_fingerprint: str
    selection_mode: Literal["selected", "eligible", "import_run"]
    requested_lead_ids: list[UUID]
    source_import_run_id: UUID | None = None
    created_by: UUID | None = None
    requested_limit: int
    status: RunStatus
    leads_selected: int
    leads_enriched: int
    leads_not_found: int
    leads_skipped: int
    leads_failed: int
    fullenrich_job_id: str | None
    errors: list[dict[str, Any]]
    last_reconciled_at: datetime | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    items: list[PhoneEnrichmentItemResponse] = Field(default_factory=list)
