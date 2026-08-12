from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models import ImportRequest
from app.services import ImportLimitExceeded, ImportService, ImportValidationError
from app.utils import merge_non_empty


class FakeRepository:
    def __init__(self, campaigns: list[dict] | None = None) -> None:
        self.campaigns = campaigns or [
            {
                "smartlead_campaign_id": 10,
                "name": "Campaign",
                "enabled": True,
            }
        ]
        self.runs: dict[str, dict] = {}
        self.leads: dict[str, dict] = {}
        self.conversations: dict[tuple[int, str], dict] = {}
        self.replies: dict[str, dict] = {}

    async def list_campaigns(self, *, enabled_only: bool = False):
        if enabled_only:
            return [item for item in self.campaigns if item["enabled"]]
        return self.campaigns

    async def get_campaigns_by_ids(self, campaign_ids):
        return [
            item
            for item in self.campaigns
            if item["smartlead_campaign_id"] in campaign_ids
        ]

    async def create_import_run(self, **values):
        run_id = str(uuid4())
        run = {
            "id": run_id,
            "status": "running",
            **values,
            "qualifying_conversation_count": 0,
            "leads_processed": 0,
            "conversations_processed": 0,
            "replies_processed": 0,
            "errors": [],
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": None,
        }
        self.runs[run_id] = run
        return deepcopy(run)

    async def update_import_run(self, run_id, values):
        self.runs[run_id].update(values)
        return deepcopy(self.runs[run_id])

    async def upsert_lead(
        self,
        *,
        email,
        email_normalized,
        observed_at,
        typed_properties,
        properties,
        custom_properties,
    ):
        existing = self.leads.get(email_normalized)
        if existing is None:
            existing = {
                "id": str(uuid4()),
                "email": email,
                "email_normalized": email_normalized,
                **typed_properties,
                "properties": properties,
                "custom_properties": custom_properties,
                "source_observed_at": observed_at,
            }
            self.leads[email_normalized] = existing
        else:
            existing.update(
                {
                    key: value
                    for key, value in typed_properties.items()
                    if value not in (None, "")
                }
            )
            existing["properties"] = merge_non_empty(existing["properties"], properties)
            existing["custom_properties"] = merge_non_empty(
                existing["custom_properties"], custom_properties
            )
        return deepcopy(existing)

    async def upsert_conversation(self, values):
        key = (
            values["smartlead_campaign_id"],
            values["smartlead_campaign_lead_map_id"],
        )
        conversation = {"id": str(uuid4()), **values}
        if key in self.conversations:
            conversation["id"] = self.conversations[key]["id"]
        self.conversations[key] = conversation
        return deepcopy(conversation)

    async def upsert_reply(self, values):
        reply = {"id": str(uuid4()), **values}
        if values["dedupe_key"] in self.replies:
            reply["id"] = self.replies[values["dedupe_key"]]["id"]
        self.replies[values["dedupe_key"]] = reply
        return deepcopy(reply)


class FakeSmartLead:
    def __init__(self, *, total_count: int = 1) -> None:
        self.total_count = total_count

    async def get_categories(self):
        return [
            {"id": 1, "name": "Interested", "sentiment_type": "positive"},
            {"id": 2, "name": "Not Interested", "sentiment_type": "negative"},
        ]

    async def get_inbox_page(self, *, fetch_message_history, offset, **kwargs):
        if not fetch_message_history:
            return {"messages": [], "total_count": self.total_count}
        if offset:
            return {"messages": [], "total_count": 1}
        return {
            "total_count": 1,
            "messages": [
                {
                    "campaign_lead_map_id": "map-1",
                    "campaign": {"id": 10, "name": "Campaign"},
                    "lead": {
                        "email": "Lead@Example.com ",
                        "first_name": "Inbox name",
                        "company": "Acme",
                    },
                    "category": {"id": 1, "name": "Interested"},
                    "message_history": [
                        {
                            "id": "outbound-1",
                            "direction": "outbound",
                            "body": "Cold email",
                            "sent_at": "2026-07-31T10:00:00Z",
                        },
                        {
                            "id": "reply-1",
                            "direction": "inbound",
                            "subject": "Re: Hello",
                            "body": "Sounds good.\nPat Lee\n+1 555 0100",
                            "received_at": "2026-08-01T10:00:00Z",
                            "sent_from": "lead@example.com",
                            "sent_to": "sender@example.com",
                        },
                    ],
                }
            ],
        }

    async def get_campaign_leads_page(self, *, offset, **kwargs):
        if offset:
            return {"data": []}
        return {
            "data": [
                {
                    "campaign_lead_map_id": "map-1",
                    "status": "INPROGRESS",
                    "lead": {
                        "id": 99,
                        "email": "lead@example.com",
                        "first_name": "Pat",
                        "last_name": "Lee",
                        "phone_number": "+44 20 1234 5678",
                        "company_name": "Acme Ltd",
                        "location": "London",
                        "website": "https://acme.example",
                        "company_url": "https://acme.example",
                        "linkedin_profile": "https://linkedin.com/in/patlee",
                        "future_smartlead_property": {"nested": "preserved"},
                        "custom_fields": {
                            "job_title": "CEO",
                            "qualification": {"score": 9},
                        },
                    },
                }
            ]
        }


class MultiCampaignSmartLead(FakeSmartLead):
    async def get_inbox_page(
        self, *, fetch_message_history, offset, campaign_ids, **kwargs
    ):
        if not fetch_message_history:
            return {"messages": [], "total_count": len(campaign_ids)}
        if offset:
            return {"messages": [], "total_count": len(campaign_ids)}

        messages = []
        for campaign_id in campaign_ids:
            messages.append(
                {
                    "campaign_lead_map_id": f"map-{campaign_id}",
                    "campaign": {"id": campaign_id},
                    "lead": {"email": "same@example.com"},
                    "category": {"id": 1, "name": "Interested"},
                    "last_message": {
                        "id": f"reply-{campaign_id}",
                        "body": "Positive reply",
                        "received_at": f"2026-08-{campaign_id - 8:02d}T10:00:00Z",
                    },
                }
            )
        return {"messages": messages, "total_count": len(messages)}

    async def get_campaign_leads_page(self, *, campaign_id, offset, **kwargs):
        if offset:
            return {"data": []}
        return {
            "data": [
                {
                    "campaign_lead_map_id": f"map-{campaign_id}",
                    "lead": {
                        "id": campaign_id,
                        "email": "same@example.com",
                        "first_name": "Same",
                        "company_name": f"Company {campaign_id}",
                        "custom_fields": {f"campaign_{campaign_id}": True},
                    },
                }
            ]
        }


class CurrentSmartLeadShape(FakeSmartLead):
    async def get_inbox_page(self, *, fetch_message_history, offset, **kwargs):
        if offset:
            return {"messages": []}
        item = {
            "lead_category_id": 1,
            "lead_first_name": "Pat",
            "lead_last_name": "Lee",
            "lead_email": "lead@example.com",
            "email_lead_id": "99",
            "email_lead_map_id": "map-1",
            "email_campaign_id": 10,
            "revenue": "1M-5M",
        }
        if fetch_message_history:
            item["email_history"] = [
                {
                    "message_id": "outbound-1",
                    "type": "SENT",
                    "time": "2026-07-31T10:00:00Z",
                    "email_body": "Cold email",
                },
                {
                    "message_id": "reply-1",
                    "type": "REPLY",
                    "time": "2026-08-01T10:00:00Z",
                    "email_body": "Sounds good",
                    "subject": "Re: Hello",
                    "from": "lead@example.com",
                    "to": "sender@example.com",
                },
            ]
        return {"messages": [item]}


@pytest.mark.asyncio
async def test_import_preserves_all_properties_and_only_inbound_replies() -> None:
    repository = FakeRepository()
    service = ImportService(repository, FakeSmartLead(), max_conversations=1000)

    result = await service.run(ImportRequest())

    assert result["status"] == "succeeded"
    assert result["leads_processed"] == 1
    assert result["replies_processed"] == 1
    lead = repository.leads["lead@example.com"]
    assert lead["first_name"] == "Pat"
    assert lead["company_name"] == "Acme Ltd"
    assert lead["smartlead_phone_number"] == "+44 20 1234 5678"
    assert lead["properties"]["future_smartlead_property"]["nested"] == "preserved"
    assert lead["custom_properties"]["qualification"]["score"] == 9
    assert len(repository.replies) == 1
    assert next(iter(repository.replies.values()))["smartlead_message_id"] == "reply-1"
    snapshot = repository.conversations[(10, "map-1")]["lead_properties"]
    assert snapshot["_campaign_record"]["status"] == "INPROGRESS"

    await service.run(ImportRequest())
    assert len(repository.leads) == 1
    assert len(repository.conversations) == 1
    assert len(repository.replies) == 1


@pytest.mark.asyncio
async def test_import_accepts_current_flat_master_inbox_shape() -> None:
    repository = FakeRepository()
    service = ImportService(
        repository, CurrentSmartLeadShape(), max_conversations=1000
    )

    result = await service.run(ImportRequest())

    assert result["status"] == "succeeded"
    assert result["qualifying_conversation_count"] == 1
    assert result["leads_processed"] == 1
    assert result["replies_processed"] == 1
    assert (10, "map-1") in repository.conversations
    reply = next(iter(repository.replies.values()))
    assert reply["smartlead_message_id"] == "reply-1"
    assert reply["sent_from"] == "lead@example.com"
    assert reply["sent_to"] == "sender@example.com"


@pytest.mark.asyncio
async def test_import_rejects_runs_over_the_preflight_limit() -> None:
    repository = FakeRepository()
    service = ImportService(
        repository, FakeSmartLead(total_count=1001), max_conversations=1000
    )

    with pytest.raises(ImportLimitExceeded) as raised:
        await service.run(ImportRequest())

    assert raised.value.run["status"] == "rejected"
    assert raised.value.run["qualifying_conversation_count"] == 1001


@pytest.mark.asyncio
async def test_same_email_across_campaigns_creates_one_lead_and_two_snapshots() -> None:
    repository = FakeRepository(
        campaigns=[
            {"smartlead_campaign_id": 10, "name": "Ten", "enabled": True},
            {"smartlead_campaign_id": 11, "name": "Eleven", "enabled": True},
        ]
    )
    service = ImportService(
        repository, MultiCampaignSmartLead(), max_conversations=1000
    )

    result = await service.run(ImportRequest())

    assert result["status"] == "succeeded"
    assert result["leads_processed"] == 1
    assert result["conversations_processed"] == 2
    assert len(repository.leads) == 1
    assert len(repository.conversations) == 2
    lead = repository.leads["same@example.com"]
    assert lead["company_name"] == "Company 11"
    assert lead["custom_properties"] == {"campaign_10": True, "campaign_11": True}


@pytest.mark.asyncio
async def test_disabled_requested_campaign_is_rejected_before_a_run_starts() -> None:
    repository = FakeRepository(
        campaigns=[
            {"smartlead_campaign_id": 10, "name": "Disabled", "enabled": False}
        ]
    )
    service = ImportService(repository, FakeSmartLead(), max_conversations=1000)

    with pytest.raises(ImportValidationError, match="disabled"):
        await service.run(ImportRequest(campaign_ids=[10]))
    assert repository.runs == {}
