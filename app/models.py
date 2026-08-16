from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

PhoneSource = Literal[
    "smartlead_signature",
    "leadmagic",
    "prospeo",
    "airscale",
    "fullenrich",
]
ReplyType = Literal["positive", "ooo"]


def _validate_reply_types(reply_types: list[ReplyType]) -> None:
    if not reply_types:
        raise ValueError("reply_types must not be empty")
    if len(set(reply_types)) != len(reply_types):
        raise ValueError("reply_types must not contain duplicates")


class CampaignCreate(BaseModel):
    smartlead_campaign_id: int = Field(gt=0)
    enabled: bool = True
    reply_types: list[ReplyType] = Field(default_factory=lambda: ["positive"])

    @model_validator(mode="after")
    def validate_reply_types(self) -> "CampaignCreate":
        _validate_reply_types(self.reply_types)
        return self


class CampaignUpdate(BaseModel):
    enabled: bool | None = None
    reply_types: list[ReplyType] | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "CampaignUpdate":
        if self.enabled is None and self.reply_types is None:
            raise ValueError("at least one campaign field must be provided")
        if self.reply_types is not None:
            _validate_reply_types(self.reply_types)
        return self


class CampaignResponse(BaseModel):
    smartlead_campaign_id: int
    name: str
    enabled: bool
    reply_types: list[ReplyType]
    created_at: datetime
    updated_at: datetime


class ImportRequest(BaseModel):
    campaign_ids: list[int] | None = None
    reply_time_from: datetime | None = None
    reply_time_to: datetime | None = None

    @model_validator(mode="after")
    def validate_filters(self) -> "ImportRequest":
        if self.campaign_ids is not None:
            if not self.campaign_ids:
                raise ValueError("campaign_ids must not be empty")
            if len(set(self.campaign_ids)) != len(self.campaign_ids):
                raise ValueError("campaign_ids must not contain duplicates")
            if any(campaign_id <= 0 for campaign_id in self.campaign_ids):
                raise ValueError("campaign_ids must contain positive integers")

        for value in (self.reply_time_from, self.reply_time_to):
            if value is not None and value.tzinfo is None:
                raise ValueError("reply time filters must include a timezone")

        if (
            self.reply_time_from is not None
            and self.reply_time_to is not None
            and self.reply_time_from >= self.reply_time_to
        ):
            raise ValueError("reply_time_from must be before reply_time_to")
        return self


class ImportRunResponse(BaseModel):
    id: UUID
    status: Literal["running", "succeeded", "partial", "failed", "rejected"]
    campaign_ids: list[int]
    reply_time_from: datetime | None
    reply_time_to: datetime | None
    max_conversations: int
    qualifying_conversation_count: int
    leads_processed: int
    conversations_processed: int
    replies_processed: int
    errors: list[dict[str, Any]]
    started_at: datetime
    completed_at: datetime | None


class LeadListItem(BaseModel):
    id: UUID
    email: str
    first_name: str | None
    last_name: str | None
    smartlead_phone_number: str | None
    company_name: str | None
    location: str | None
    website: str | None
    company_url: str | None
    linkedin_profile: str | None
    enriched_phone_number: str | None
    phone_source: PhoneSource | None
    positive_conversation_count: int
    ooo_conversation_count: int
    latest_reply_at: datetime | None


class LeadListResponse(BaseModel):
    items: list[LeadListItem]
    total: int
    limit: int
    offset: int


class LeadDetailResponse(BaseModel):
    lead: dict[str, Any]
    conversations: list[dict[str, Any]]
