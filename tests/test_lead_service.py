from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest

from app.services import LeadService
from app.smartlead.client import SmartLeadError
from app.utils import to_iso, utc_now


class FakeLeadRepository:
    def __init__(self, *, lead: dict | None, conversations: list[dict]) -> None:
        self.lead = deepcopy(lead)
        self.conversations = deepcopy(conversations)
        self.replies: list[dict] = []
        self.mark_calls = 0

    async def get_lead_detail(self, lead_id: str) -> dict | None:
        if self.lead is None:
            return None
        replies_by_conversation: dict[str, list[dict]] = {}
        for reply in self.replies:
            replies_by_conversation.setdefault(reply["conversation_id"], []).append(
                reply
            )
        conversations = []
        for conversation in self.conversations:
            item = deepcopy(conversation)
            item["replies"] = deepcopy(
                replies_by_conversation.get(str(conversation["id"]), [])
            )
            conversations.append(item)
        return {"lead": deepcopy(self.lead), "conversations": conversations}

    async def upsert_reply(self, values: dict) -> dict:
        reply = {"id": str(uuid4()), **values}
        self.replies.append(reply)
        return deepcopy(reply)

    async def mark_chat_refreshed(self, lead_id: str) -> None:
        self.mark_calls += 1
        if self.lead is not None:
            self.lead["chat_refreshed_at"] = to_iso(utc_now())
            self.lead["id"] = lead_id


class FakeSmartLead:
    def __init__(
        self,
        messages_by_lead: dict[tuple[int, str], list[dict]] | None = None,
        *,
        error: SmartLeadError | None = None,
    ) -> None:
        self.messages_by_lead = messages_by_lead or {}
        self.error = error
        self.calls: list[tuple[int, str]] = []

    async def get_lead_message_history(self, *, campaign_id: int, lead_id: str):
        self.calls.append((campaign_id, lead_id))
        if self.error is not None:
            raise self.error
        return deepcopy(self.messages_by_lead.get((campaign_id, lead_id), []))


def _conversation(**overrides) -> dict:
    conversation = {
        "id": "conversation-1",
        "smartlead_campaign_id": 10,
        "smartlead_lead_id": "99",
    }
    conversation.update(overrides)
    return conversation


def _thread() -> list[dict]:
    return [
        {
            "id": "outbound-1",
            "direction": "outbound",
            "subject": "Hello",
            "body": "Cold email",
            "sent_at": "2026-07-31T10:00:00Z",
            "sent_from": "sender@example.com",
            "sent_to": "lead@example.com",
        },
        {
            "id": "reply-1",
            "type": "REPLY",
            "subject": "Re: Hello",
            "email_body": "Sounds good",
            "time": "2026-08-01T10:00:00Z",
            "from": "lead@example.com",
            "to": "sender@example.com",
        },
    ]


@pytest.mark.asyncio
async def test_get_detail_returns_none_when_lead_is_missing() -> None:
    service = LeadService(
        FakeLeadRepository(lead=None, conversations=[]),
        FakeSmartLead(),
        chat_refresh_ttl_seconds=3600,
    )

    assert await service.get_detail("missing") is None


@pytest.mark.asyncio
async def test_stale_cache_refreshes_inbound_and_outbound_messages() -> None:
    repository = FakeLeadRepository(
        lead={"id": "lead-1", "email": "lead@example.com", "chat_refreshed_at": None},
        conversations=[_conversation()],
    )
    smartlead = FakeSmartLead({(10, "99"): _thread()})
    service = LeadService(repository, smartlead, chat_refresh_ttl_seconds=3600)

    detail = await service.get_detail("lead-1")

    assert smartlead.calls == [(10, "99")]
    assert repository.mark_calls == 1
    replies = detail["conversations"][0]["replies"]
    assert [item["direction"] for item in replies] == ["outbound", "inbound"]
    assert [item["smartlead_message_id"] for item in replies] == [
        "outbound-1",
        "reply-1",
    ]
    assert replies[0]["body"] == "Cold email"
    assert replies[1]["body"] == "Sounds good"
    assert detail["lead"]["chat_refreshed_at"] is not None


@pytest.mark.asyncio
async def test_fresh_cache_skips_smartlead() -> None:
    repository = FakeLeadRepository(
        lead={
            "id": "lead-1",
            "chat_refreshed_at": to_iso(utc_now() - timedelta(minutes=10)),
        },
        conversations=[_conversation()],
    )
    smartlead = FakeSmartLead({(10, "99"): _thread()})
    service = LeadService(repository, smartlead, chat_refresh_ttl_seconds=3600)

    detail = await service.get_detail("lead-1")

    assert smartlead.calls == []
    assert repository.mark_calls == 0
    assert detail["conversations"][0]["replies"] == []


@pytest.mark.asyncio
async def test_zero_ttl_always_refreshes() -> None:
    repository = FakeLeadRepository(
        lead={"id": "lead-1", "chat_refreshed_at": to_iso(utc_now())},
        conversations=[_conversation()],
    )
    smartlead = FakeSmartLead({(10, "99"): _thread()})
    service = LeadService(repository, smartlead, chat_refresh_ttl_seconds=0)

    await service.get_detail("lead-1")

    assert smartlead.calls == [(10, "99")]
    assert repository.mark_calls == 1


@pytest.mark.asyncio
async def test_smartlead_failure_returns_cache_without_stamping() -> None:
    repository = FakeLeadRepository(
        lead={"id": "lead-1", "chat_refreshed_at": None},
        conversations=[_conversation()],
    )
    smartlead = FakeSmartLead(error=SmartLeadError("unavailable", status_code=502))
    service = LeadService(repository, smartlead, chat_refresh_ttl_seconds=3600)

    detail = await service.get_detail("lead-1")

    assert smartlead.calls == [(10, "99")]
    assert repository.mark_calls == 0
    assert detail["lead"]["chat_refreshed_at"] is None
    assert detail["conversations"][0]["replies"] == []


@pytest.mark.asyncio
async def test_conversations_without_smartlead_lead_id_are_skipped() -> None:
    repository = FakeLeadRepository(
        lead={"id": "lead-1", "chat_refreshed_at": None},
        conversations=[
            _conversation(id="skip", smartlead_lead_id=None),
            _conversation(id="keep", smartlead_campaign_id=11, smartlead_lead_id="88"),
        ],
    )
    smartlead = FakeSmartLead(
        {
            (11, "88"): [
                {
                    "id": "outbound-2",
                    "direction": "outbound",
                    "body": "Follow up",
                    "sent_at": "2026-08-02T10:00:00Z",
                }
            ]
        }
    )
    service = LeadService(repository, smartlead, chat_refresh_ttl_seconds=3600)

    detail = await service.get_detail("lead-1")

    assert smartlead.calls == [(11, "88")]
    assert repository.mark_calls == 1
    replies_by_id = {
        conversation["id"]: conversation["replies"]
        for conversation in detail["conversations"]
    }
    assert replies_by_id["skip"] == []
    assert replies_by_id["keep"][0]["direction"] == "outbound"
    assert replies_by_id["keep"][0]["body"] == "Follow up"
