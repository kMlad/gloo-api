from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.heyreach.service import HeyReachImportService
from app.models import HeyReachImportRequest
from app.phone_enrichment.providers.linkedin import canonical_linkedin_profile
from app.repositories import ConcurrentImportError
from app.services import ImportLimitExceeded, ImportValidationError


class FakeRepository:
    def __init__(self, campaigns: list[dict] | None = None) -> None:
        self.campaigns = campaigns or [
            {
                "heyreach_campaign_id": 10,
                "name": "Campaign",
                "enabled": True,
            }
        ]
        self.runs: dict[str, dict] = {}
        self.leads: dict[str, dict] = {}
        self.conversations: dict[tuple[int, str], dict] = {}
        self.replies: dict[str, dict] = {}
        self.run_items: dict[tuple[str, str], dict] = {}

    async def list_campaigns(self, *, enabled_only: bool = False):
        if enabled_only:
            return [item for item in self.campaigns if item["enabled"]]
        return self.campaigns

    async def get_campaigns_by_ids(self, campaign_ids):
        return [
            item
            for item in self.campaigns
            if item["heyreach_campaign_id"] in campaign_ids
        ]

    async def sync_campaign_catalog(self, campaigns):
        by_id = {item["heyreach_campaign_id"]: item for item in self.campaigns}
        for campaign in campaigns:
            campaign_id = int(campaign["id"])
            existing = by_id.get(campaign_id)
            name = str(campaign.get("name") or f"HeyReach campaign {campaign_id}")
            if existing is None:
                row = {
                    "heyreach_campaign_id": campaign_id,
                    "name": name,
                    "enabled": True,
                }
                self.campaigns.append(row)
                by_id[campaign_id] = row
            else:
                existing["name"] = name
        return campaigns

    async def create_import_run(self, **values):
        campaign_ids = set(values["campaign_ids"])
        for run in self.runs.values():
            if run["status"] not in {"queued", "running"}:
                continue
            if campaign_ids & set(run["campaign_ids"]):
                raise ConcurrentImportError
        run_id = str(uuid4())
        run = {
            "id": run_id,
            "status": "queued",
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

    async def claim_import_run(self, run_id):
        if self.runs[run_id]["status"] != "queued":
            return None
        self.runs[run_id]["status"] = "running"
        return deepcopy(self.runs[run_id])

    async def get_import_run(self, run_id):
        run = self.runs.get(run_id)
        return deepcopy(run) if run is not None else None

    async def get_import_run_by_idempotency_key(self, key):
        return next(
            (
                deepcopy(run)
                for run in self.runs.values()
                if run.get("idempotency_key") == key
            ),
            None,
        )

    async def upsert_import_run_item(self, **values):
        key = (values["run_id"], values["conversation_id"])
        item = {"id": str(uuid4()), **values}
        if key in self.run_items:
            item["id"] = self.run_items[key]["id"]
        self.run_items[key] = item
        return deepcopy(item)

    async def upsert_lead_conversation(self, *, conversation, **lead_values):
        lead_id = str(uuid4())
        linkedin = lead_values["typed_properties"]["linkedin_profile_normalized"]
        existing = self.leads.get(linkedin)
        if existing is None:
            existing = {
                "id": lead_id,
                "email": lead_values.get("email"),
                **lead_values["typed_properties"],
            }
            self.leads[linkedin] = existing
        conversation_row = {
            "id": str(uuid4()),
            "lead_id": existing["id"],
            **conversation,
        }
        self.conversations[
            (conversation["heyreach_campaign_id"], conversation["heyreach_conversation_id"])
        ] = conversation_row
        return {"lead": deepcopy(existing), "conversation": deepcopy(conversation_row)}

    async def upsert_reply(self, values):
        self.replies[values["dedupe_key"]] = values
        return values


class FakeHeyReach:
    async def list_linkedin_accounts(self):
        return [{"id": 7, "firstName": "Alex", "lastName": "Sender"}]

    async def get_linkedin_account(self, account_id):
        return {"id": account_id, "firstName": "Alex", "lastName": "Sender"}

    async def get_campaign_leads_page(self, *, campaign_id, offset, **kwargs):
        if offset:
            return {"items": []}
        return {
            "items": [
                {
                    "id": 99,
                    "leadMessageStatus": "MessageReply",
                    "lastActionTime": "2026-09-01T10:00:00Z",
                    "linkedInSenderId": 7,
                    "linkedInSenderFullName": "Alex Sender",
                    "linkedInUserProfile": {
                        "firstName": "Pat",
                        "lastName": "Lee",
                        "profileUrl": "https://www.linkedin.com/in/PatLee",
                        "companyName": "Acme",
                        "emailAddress": "pat@example.com",
                        "customUserFields": [{"name": "title", "value": "CEO"}],
                    },
                },
                {
                    "id": 100,
                    "leadMessageStatus": "MessageSent",
                    "linkedInUserProfile": {
                        "profileUrl": "https://www.linkedin.com/in/skipped",
                    },
                },
            ]
        }

    async def get_conversations_page(self, **kwargs):
        return {
            "items": [
                {
                    "id": "conv-1",
                    "linkedInAccountId": 7,
                }
            ]
        }

    async def get_chatroom(self, *, account_id, conversation_id):
        return {
            "id": conversation_id,
            "linkedInAccountId": account_id,
            "linkedInAccount": {
                "id": account_id,
                "firstName": "Alex",
                "lastName": "Sender",
            },
            "messages": [
                {
                    "id": "out-1",
                    "body": "Hello from us",
                    "isFromMe": True,
                    "createdAt": "2026-08-31T10:00:00Z",
                },
                {
                    "id": "in-1",
                    "body": "Interested. Call +1 415 555 2671",
                    "isFromMe": False,
                    "createdAt": "2026-09-01T10:00:00Z",
                    "sender": "Pat Lee",
                },
            ],
        }


@pytest.mark.asyncio
async def test_imports_replied_linkedin_leads_and_messages() -> None:
    repository = FakeRepository()
    service = HeyReachImportService(repository, FakeHeyReach(), max_conversations=10)
    run = await service.start(HeyReachImportRequest(campaign_ids=[10]))
    result = await service.execute(str(run["id"]))

    assert result["status"] == "succeeded"
    assert result["leads_processed"] == 1
    assert result["conversations_processed"] == 1
    assert result["replies_processed"] == 2
    lead = next(iter(repository.leads.values()))
    assert lead["linkedin_profile"] == "https://www.linkedin.com/in/patlee"
    assert lead["first_name"] == "Pat"
    assert any(
        reply["direction"] == "inbound" and "415" in reply["body"]
        for reply in repository.replies.values()
    )
    conversation = next(iter(repository.conversations.values()))
    assert conversation["linkedin_sender_name"] == "Alex Sender"
    outbound = next(
        reply
        for reply in repository.replies.values()
        if reply["direction"] == "outbound"
    )
    assert outbound["sent_from"] == "Alex Sender"
    inbound = next(
        reply
        for reply in repository.replies.values()
        if reply["direction"] == "inbound"
    )
    assert inbound["sent_from"] == "Pat Lee"


@pytest.mark.asyncio
async def test_rejects_imports_over_the_conversation_limit() -> None:
    repository = FakeRepository()
    service = HeyReachImportService(repository, FakeHeyReach(), max_conversations=0)
    run = await service.start(HeyReachImportRequest(campaign_ids=[10]))
    with pytest.raises(ImportLimitExceeded):
        await service.execute(str(run["id"]))
    finished = await repository.get_import_run(str(run["id"]))
    assert finished["status"] == "rejected"


@pytest.mark.asyncio
async def test_idempotent_start_returns_the_original_run() -> None:
    repository = FakeRepository()
    service = HeyReachImportService(repository, FakeHeyReach(), max_conversations=10)
    first = await service.start(
        HeyReachImportRequest(campaign_ids=[10]),
        idempotency_key="heyreach-import-01",
    )
    second = await service.start(
        HeyReachImportRequest(campaign_ids=[10]),
        idempotency_key="heyreach-import-01",
    )
    assert first["id"] == second["id"]


@pytest.mark.asyncio
async def test_idempotency_key_cannot_change_the_request() -> None:
    repository = FakeRepository(
        [
            {"heyreach_campaign_id": 10, "name": "A", "enabled": True},
            {"heyreach_campaign_id": 11, "name": "B", "enabled": True},
        ]
    )
    service = HeyReachImportService(repository, FakeHeyReach(), max_conversations=10)
    await service.start(
        HeyReachImportRequest(campaign_ids=[10]),
        idempotency_key="heyreach-import-02",
    )
    with pytest.raises(ImportValidationError, match="already used"):
        await service.start(
            HeyReachImportRequest(campaign_ids=[11]),
            idempotency_key="heyreach-import-02",
        )


@pytest.mark.asyncio
async def test_overlapping_campaign_imports_are_rejected() -> None:
    repository = FakeRepository(
        [
            {"heyreach_campaign_id": 10, "name": "A", "enabled": True},
            {"heyreach_campaign_id": 11, "name": "B", "enabled": True},
        ]
    )
    service = HeyReachImportService(repository, FakeHeyReach(), max_conversations=10)
    await service.start(HeyReachImportRequest(campaign_ids=[10]))
    with pytest.raises(ConcurrentImportError):
        await service.start(HeyReachImportRequest(campaign_ids=[10, 11]))


@pytest.mark.asyncio
async def test_missing_inbox_uses_a_fallback_conversation_id() -> None:
    class EmptyInbox(FakeHeyReach):
        async def get_conversations_page(self, **kwargs):
            return {"items": []}

    repository = FakeRepository()
    service = HeyReachImportService(repository, EmptyInbox(), max_conversations=10)
    run = await service.start(HeyReachImportRequest(campaign_ids=[10]))
    result = await service.execute(str(run["id"]))

    assert result["status"] == "succeeded"
    conversation = next(iter(repository.conversations.values()))
    assert conversation["heyreach_conversation_id"].startswith("fallback:10:")
    assert conversation["heyreach_conversation_id"].endswith(
        "https://www.linkedin.com/in/patlee"
    )


@pytest.mark.asyncio
async def test_sender_name_falls_back_to_linkedin_account_catalog() -> None:
    class CatalogOnly(FakeHeyReach):
        async def get_campaign_leads_page(self, *, campaign_id, offset, **kwargs):
            page = await super().get_campaign_leads_page(
                campaign_id=campaign_id, offset=offset, **kwargs
            )
            for item in page["items"]:
                item.pop("linkedInSenderFullName", None)
            return page

        async def get_chatroom(self, *, account_id, conversation_id):
            chatroom = await super().get_chatroom(
                account_id=account_id, conversation_id=conversation_id
            )
            chatroom.pop("linkedInAccount", None)
            return chatroom

    repository = FakeRepository()
    service = HeyReachImportService(repository, CatalogOnly(), max_conversations=10)
    run = await service.start(HeyReachImportRequest(campaign_ids=[10]))
    await service.execute(str(run["id"]))

    conversation = next(iter(repository.conversations.values()))
    assert conversation["linkedin_sender_name"] == "Alex Sender"


def test_canonical_linkedin_profile_normalizes_person_urls() -> None:
    assert (
        canonical_linkedin_profile("https://www.linkedin.com/in/PatLee/")
        == "https://www.linkedin.com/in/patlee"
    )
    assert (
        canonical_linkedin_profile("linkedin.com/in/PatLee")
        == "https://www.linkedin.com/in/patlee"
    )
    assert canonical_linkedin_profile("https://www.linkedin.com/company/acme") is None
    assert canonical_linkedin_profile("") is None
