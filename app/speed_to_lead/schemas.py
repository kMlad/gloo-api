from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, model_validator

from app.models import LeadListItem, LeadPlatform, PhoneEnrichmentStatus

SpeedToLeadOutcome = Literal[
    "processed",
    "duplicate",
    "campaign_not_enabled",
    "not_positive",
    "ignored_event",
    "invalid_payload",
]
NotificationStatus = Literal["pending", "sent", "failed", "skipped"]


class SpeedToLeadCampaignUpdate(BaseModel):
    enabled: bool
    sdr_id: UUID | None = None

    @model_validator(mode="after")
    def validate_sdr(self) -> "SpeedToLeadCampaignUpdate":
        if self.enabled and self.sdr_id is None:
            raise ValueError("sdr_id is required to enable speed to lead")
        return self


class SpeedToLeadEvent(BaseModel):
    id: UUID
    platform: LeadPlatform
    lead_id: UUID
    smartlead_campaign_id: int | None = None
    heyreach_campaign_id: int | None = None
    conversation_id: UUID
    category_id: int | None = None
    category_name: str | None = None
    reply_excerpt: str | None = None
    replied_at: datetime
    dedupe_key: str
    enrichment_run_id: UUID | None = None
    notification_status: NotificationStatus = "pending"
    notification_error: str | None = None
    slack_message_ts: str | None = None
    created_at: datetime
    updated_at: datetime


class SpeedToLeadEventItem(SpeedToLeadEvent):
    campaign_name: str
    lead: LeadListItem
    enrichment: PhoneEnrichmentStatus | None = None


class SpeedToLeadListResponse(BaseModel):
    items: list[SpeedToLeadEventItem]
    total: int
    limit: int
    offset: int
