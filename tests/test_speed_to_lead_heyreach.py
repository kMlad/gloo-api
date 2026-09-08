from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_speed_to_lead_service import (
    SDR_ID,
    FakeLeadRepository,
    FakePhoneEnrichment,
    FakeSmartLead,
    FakeSpeedToLeadRepository,
    SlackStub,
    _notifier,
)

from app.heyreach.client import HeyReachError
from app.speed_to_lead.service import (
    SpeedToLeadNotFoundError,
    SpeedToLeadService,
    SpeedToLeadValidationError,
)

HEYREACH_CAMPAIGN_ID = 77
LINKEDIN = "https://www.linkedin.com/in/patlee"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FakeHeyReachRepository:
    def __init__(self, *, assigned_sdr_id: str | None = None) -> None:
        self.assigned_sdr_id = assigned_sdr_id
        self.upserts: list[dict] = []
        self.replies: list[dict] = []
        self.lead_id = str(uuid4())
        self.conversation_id = str(uuid4())

    async def upsert_lead_conversation(self, *, conversation, **lead_values):
        self.upserts.append({"conversation": conversation, **lead_values})
        return {
            "lead": {
                "id": self.lead_id,
                "email": lead_values.get("email"),
                "assigned_sdr_id": self.assigned_sdr_id,
                **lead_values["typed_properties"],
            },
            "conversation": {"id": self.conversation_id, **conversation},
        }

    async def upsert_reply(self, values):
        self.replies.append(values)
        return values

    async def get_conversation(self, **kwargs):
        return None

    async def update_conversation(self, conversation_id, values):
        return None


class FakeHeyReach:
    def __init__(
        self,
        *,
        conversation: dict | None = None,
        messages: list[dict] | None = None,
        conversations_error: bool = False,
    ) -> None:
        self.conversation = (
            conversation
            if conversation is not None
            else {"id": "conv-1", "linkedInAccountId": 7}
        )
        self.messages = (
            messages
            if messages is not None
            else [
                {
                    "id": "out-1",
                    "body": "Hello from us",
                    "isFromMe": True,
                    "createdAt": "2026-09-07T10:00:00Z",
                },
                {
                    "id": "in-1",
                    "body": "Sounds   great, let's   talk.",
                    "isFromMe": False,
                    "createdAt": "2026-09-07T10:30:00Z",
                    "sender": "Pat Lee",
                },
            ]
        )
        self.conversations_error = conversations_error
        self.conversation_calls: list[dict] = []
        self.chatroom_calls: list[tuple[int, str]] = []
        self.saved: list[dict] = []
        self.deleted: list[str] = []
        self.delete_error: HeyReachError | None = None

    async def get_conversations_page(self, **kwargs):
        self.conversation_calls.append(kwargs)
        if self.conversations_error:
            raise HeyReachError("inbox down", status_code=500)
        if not self.conversation:
            return {"items": []}
        return {"items": [deepcopy(self.conversation)]}

    async def get_chatroom(self, *, account_id, conversation_id):
        self.chatroom_calls.append((account_id, conversation_id))
        return {
            "id": conversation_id,
            "linkedInAccountId": account_id,
            "messages": deepcopy(self.messages),
        }

    async def list_linkedin_accounts(self):
        return [{"id": 7, "firstName": "Alex", "lastName": "Sender"}]

    async def get_linkedin_account(self, account_id):
        return {"id": account_id, "firstName": "Alex", "lastName": "Sender"}

    async def save_webhook(self, **values):
        self.saved.append(values)
        return {"id": "wh-9", "webhookUrl": values["webhook_url"]}

    async def delete_webhook(self, webhook_id):
        self.deleted.append(webhook_id)
        if self.delete_error is not None:
            raise self.delete_error


def _heyreach_campaign(*, enabled: bool = True, sdr_id: str | None = SDR_ID) -> dict:
    return {
        "heyreach_campaign_id": HEYREACH_CAMPAIGN_ID,
        "name": "LinkedIn outreach",
        "enabled": True,
        "status": "ACTIVE",
        "speed_to_lead_enabled": enabled,
        "speed_to_lead_sdr_id": sdr_id if enabled else None,
        "heyreach_webhook_id": "wh-9" if enabled else None,
        "created_at": _now(),
        "updated_at": _now(),
    }


def _payload(**overrides) -> dict:
    payload = {
        "event_type": "LEAD_TAG_UPDATED",
        "timestamp": "2026-09-07T10:45:00Z",
        "correlation_id": "corr-1",
        "campaign": {"id": HEYREACH_CAMPAIGN_ID, "name": "LinkedIn outreach"},
        "sender": {
            "id": 7,
            "first_name": "Alex",
            "last_name": "Sender",
            "profile_url": "https://www.linkedin.com/in/alex-sender",
        },
        "lead": {
            "profile_url": "https://www.linkedin.com/in/PatLee/",
            "email_address": "Pat@Example.com",
            "first_name": "Pat",
            "last_name": "Lee",
            "company_name": "Acme",
            "position": "CTO",
            "tags": ["Interested"],
        },
    }
    payload.update(overrides)
    return payload


def _service(
    repository,
    *,
    heyreach=None,
    heyreach_repository=None,
    enrichment=None,
    notifier=None,
    configured: bool = True,
):
    heyreach = heyreach or FakeHeyReach()
    heyreach_repository = heyreach_repository or FakeHeyReachRepository()
    enrichment = enrichment or FakePhoneEnrichment()
    service = SpeedToLeadService(
        repository,
        FakeLeadRepository(),
        FakeSmartLead(),
        enrichment,
        webhook_url="https://api.example.com/api/v1/smartlead/webhooks/token",
        notifier=notifier,
        heyreach=heyreach if configured else None,
        heyreach_repository=heyreach_repository if configured else None,
        heyreach_webhook_url=(
            "https://api.example.com/api/v1/heyreach/webhooks/token"
            if configured
            else None
        ),
    )
    return service, heyreach, heyreach_repository, enrichment


# ------------------------------------------------------------------ webhook


@pytest.mark.asyncio
async def test_interested_tag_creates_event_assigns_and_enriches() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    service, heyreach, heyreach_repository, enrichment = _service(repository)

    result = await service.handle_heyreach_tag_update(_payload())

    assert result.outcome == "processed"
    event = result.event
    assert event["platform"] == "heyreach"
    assert event["heyreach_campaign_id"] == HEYREACH_CAMPAIGN_ID
    assert event["smartlead_campaign_id"] is None
    assert event["category_id"] is None
    assert event["category_name"] == "Interested"
    assert event["conversation_id"] == heyreach_repository.conversation_id
    assert event["reply_excerpt"] == "Sounds great, let's talk."
    assert event["replied_at"] == "2026-09-07T10:30:00Z"
    assert event["notification_status"] == "skipped"

    # Lead + conversation persisted through the HeyReach RPC path.
    upsert = heyreach_repository.upserts[0]
    assert upsert["email"] == "Pat@Example.com"
    assert upsert["email_normalized"] == "pat@example.com"
    assert upsert["typed_properties"]["linkedin_profile"] == LINKEDIN
    assert upsert["typed_properties"]["first_name"] == "Pat"
    assert upsert["typed_properties"]["company_name"] == "Acme"
    conversation = upsert["conversation"]
    assert conversation["heyreach_campaign_id"] == HEYREACH_CAMPAIGN_ID
    assert conversation["heyreach_conversation_id"] == "conv-1"
    assert conversation["auto_tag"] == "Interested"
    assert conversation["reply_type"] == "positive"
    assert conversation["linkedin_account_id"] == 7
    assert conversation["linkedin_sender_name"] == "Alex Sender"
    assert conversation["lead_properties"]["_campaign_record"]["_webhook_event"] == {
        "event_type": "LEAD_TAG_UPDATED",
        "timestamp": "2026-09-07T10:45:00Z",
        "correlation_id": "corr-1",
    }
    assert [reply["direction"] for reply in heyreach_repository.replies] == [
        "outbound",
        "inbound",
    ]
    assert heyreach.conversation_calls[0]["campaign_ids"] == [HEYREACH_CAMPAIGN_ID]
    assert heyreach.conversation_calls[0]["lead_profile_url"] == LINKEDIN
    assert heyreach.chatroom_calls == [(7, "conv-1")]

    # Assignment + enrichment behave like SmartLead.
    assert repository.assignments == [(heyreach_repository.lead_id, SDR_ID)]
    assert enrichment.started == [
        ([heyreach_repository.lead_id], f"speed-to-lead:{event['id']}")
    ]
    assert enrichment.executed == [event["enrichment_run_id"]]


@pytest.mark.asyncio
async def test_flattened_payload_and_tag_field_are_accepted() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    service, _, heyreach_repository, _ = _service(repository)
    payload = {
        "event_type": "LEAD_TAG_UPDATED",
        "campaign_id": HEYREACH_CAMPAIGN_ID,
        "campaign_name": "LinkedIn outreach",
        "lead_profile_url": LINKEDIN,
        "lead_first_name": "Pat",
        "lead_last_name": "Lee",
        "tag": "Interested",
    }

    result = await service.handle_heyreach_tag_update(payload)

    assert result.outcome == "processed"
    assert heyreach_repository.upserts[0]["typed_properties"]["first_name"] == "Pat"
    assert heyreach_repository.upserts[0]["email"] is None


@pytest.mark.asyncio
async def test_non_positive_tag_is_skipped() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    service, _, heyreach_repository, enrichment = _service(repository)

    result = await service.handle_heyreach_tag_update(
        _payload(lead={**_payload()["lead"], "tags": ["Not interested"]})
    )

    assert result.outcome == "not_positive"
    assert result.reason == "Not interested"
    assert heyreach_repository.upserts == []
    assert repository.events == {}
    assert enrichment.started == []


@pytest.mark.asyncio
async def test_tag_missing_from_payload_falls_back_to_inbox_record() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    heyreach = FakeHeyReach(
        conversation={"id": "conv-1", "linkedInAccountId": 7, "autoTag": "Interested"}
    )
    service, _, _, _ = _service(repository, heyreach=heyreach)
    lead = {key: value for key, value in _payload()["lead"].items() if key != "tags"}

    result = await service.handle_heyreach_tag_update(_payload(lead=lead))

    assert result.outcome == "processed"
    assert result.event["category_name"] == "Interested"


@pytest.mark.asyncio
async def test_untagged_lead_without_inbox_tag_is_not_positive() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    service, heyreach, _, _ = _service(repository)
    lead = {key: value for key, value in _payload()["lead"].items() if key != "tags"}

    result = await service.handle_heyreach_tag_update(_payload(lead=lead))

    assert result.outcome == "not_positive"
    assert result.reason is None
    assert heyreach.chatroom_calls == [(7, "conv-1")]
    assert repository.events == {}


@pytest.mark.asyncio
async def test_disabled_or_unknown_campaign_is_ignored() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign(enabled=False))
    service, heyreach, _, _ = _service(repository)

    assert (
        await service.handle_heyreach_tag_update(_payload())
    ).outcome == "campaign_not_enabled"
    assert (
        await service.handle_heyreach_tag_update(_payload(campaign={"id": 1}))
    ).outcome == "campaign_not_enabled"
    assert heyreach.conversation_calls == []


@pytest.mark.asyncio
async def test_other_event_types_and_bad_payloads_are_rejected() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    service, _, _, _ = _service(repository)

    other = await service.handle_heyreach_tag_update(
        _payload(event_type="MESSAGE_REPLY_RECEIVED")
    )
    assert other.outcome == "ignored_event"
    assert other.reason == "MESSAGE_REPLY_RECEIVED"

    missing_campaign = await service.handle_heyreach_tag_update(
        _payload(campaign={"name": "x"})
    )
    assert missing_campaign.outcome == "invalid_payload"
    assert missing_campaign.reason == "missing campaign id"

    lead = {**_payload()["lead"], "profile_url": "not a url"}
    missing_profile = await service.handle_heyreach_tag_update(_payload(lead=lead))
    assert missing_profile.outcome == "invalid_payload"
    assert missing_profile.reason == "missing lead profile url"


@pytest.mark.asyncio
async def test_event_type_match_is_case_insensitive_and_optional() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    service, _, _, _ = _service(repository)

    lower = await service.handle_heyreach_tag_update(
        _payload(event_type="lead_tag_updated")
    )
    assert lower.outcome == "processed"

    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    service, _, _, _ = _service(repository)
    payload = _payload()
    del payload["event_type"]
    assert (await service.handle_heyreach_tag_update(payload)).outcome == "processed"


@pytest.mark.asyncio
async def test_unconfigured_heyreach_rejects_webhooks_and_opt_in() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    service, _, _, _ = _service(repository, configured=False)

    result = await service.handle_heyreach_tag_update(_payload())
    assert result.outcome == "invalid_payload"
    assert result.reason == "heyreach is not configured"

    with pytest.raises(SpeedToLeadValidationError):
        await service.configure_heyreach_campaign(
            HEYREACH_CAMPAIGN_ID, enabled=True, sdr_id=SDR_ID
        )


@pytest.mark.asyncio
async def test_duplicate_delivery_is_idempotent() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    service, _, heyreach_repository, enrichment = _service(repository)

    first = await service.handle_heyreach_tag_update(_payload())
    second = await service.handle_heyreach_tag_update(_payload())

    assert first.outcome == "processed"
    assert second.outcome == "duplicate"
    assert len(repository.events) == 1
    assert len(heyreach_repository.upserts) == 1
    assert len(enrichment.started) == 1


@pytest.mark.asyncio
async def test_new_reply_after_retag_creates_a_second_event() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    heyreach = FakeHeyReach()
    service, _, _, _ = _service(repository, heyreach=heyreach)

    assert (await service.handle_heyreach_tag_update(_payload())).outcome == "processed"
    heyreach.messages.append(
        {
            "id": "in-2",
            "body": "Following up, still keen.",
            "isFromMe": False,
            "createdAt": "2026-09-08T09:00:00Z",
        }
    )
    second = await service.handle_heyreach_tag_update(_payload())

    assert second.outcome == "processed"
    assert second.event["reply_excerpt"] == "Following up, still keen."
    assert len(repository.events) == 2


@pytest.mark.asyncio
async def test_missing_inbox_record_uses_payload_timestamp_and_fallback_id() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    heyreach = FakeHeyReach(conversation={}, messages=[])
    service, _, heyreach_repository, _ = _service(repository, heyreach=heyreach)

    result = await service.handle_heyreach_tag_update(
        _payload(message="Yes please, call me tomorrow")
    )

    assert result.outcome == "processed"
    assert result.event["replied_at"] == "2026-09-07T10:45:00Z"
    assert result.event["reply_excerpt"] == "Yes please, call me tomorrow"
    assert heyreach.chatroom_calls == []
    conversation = heyreach_repository.upserts[0]["conversation"]
    assert conversation["heyreach_conversation_id"] == (
        f"fallback:{HEYREACH_CAMPAIGN_ID}:{LINKEDIN}"
    )
    assert heyreach_repository.replies == []


@pytest.mark.asyncio
async def test_inbox_failure_does_not_block_the_event() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    heyreach = FakeHeyReach(conversations_error=True)
    service, _, _, _ = _service(repository, heyreach=heyreach)

    result = await service.handle_heyreach_tag_update(_payload())

    assert result.outcome == "processed"
    assert result.event["reply_excerpt"] is None


@pytest.mark.asyncio
async def test_already_assigned_lead_keeps_its_sdr() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    other_sdr = str(uuid4())
    heyreach_repository = FakeHeyReachRepository(assigned_sdr_id=other_sdr)
    service, _, _, _ = _service(repository, heyreach_repository=heyreach_repository)

    result = await service.handle_heyreach_tag_update(_payload())

    assert result.outcome == "processed"
    assert repository.assignments == []


@pytest.mark.asyncio
async def test_campaign_without_sdr_does_not_assign() -> None:
    campaign = _heyreach_campaign()
    campaign["speed_to_lead_sdr_id"] = None
    repository = FakeSpeedToLeadRepository(None, campaign)
    service, _, _, _ = _service(repository)

    result = await service.handle_heyreach_tag_update(_payload())

    assert result.outcome == "processed"
    assert repository.assignments == []


@pytest.mark.asyncio
async def test_long_excerpts_are_truncated() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    heyreach = FakeHeyReach(
        messages=[
            {
                "id": "in-1",
                "body": "word " * 100,
                "isFromMe": False,
                "createdAt": "2026-09-07T10:30:00Z",
            }
        ]
    )
    service, _, _, _ = _service(repository, heyreach=heyreach)

    result = await service.handle_heyreach_tag_update(_payload())

    excerpt = result.event["reply_excerpt"]
    assert len(excerpt) == 280
    assert excerpt.endswith("…")


@pytest.mark.asyncio
async def test_background_boundary_swallows_errors() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    service, _, _, _ = _service(repository)

    async def boom(campaign_id):
        raise RuntimeError("db down")

    repository.get_heyreach_campaign = boom  # type: ignore[method-assign]
    await service.process_heyreach_webhook(_payload())


# ---------------------------------------------------------------- slack


@pytest.mark.asyncio
async def test_alert_is_labelled_linkedin_and_links_the_profile() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    slack = SlackStub()
    service, _, _, _ = _service(repository, notifier=_notifier(slack))

    result = await service.handle_heyreach_tag_update(_payload())

    assert result.event["notification_status"] == "sent"
    assert result.event["slack_message_ts"] == "1.0"
    message = slack.messages[0]
    assert message["text"] == (
        "New positive LinkedIn reply: Pat Lee (Acme) · LinkedIn outreach"
    )
    body = message["blocks"][0]["text"]["text"]
    assert "*New positive LinkedIn reply*" in body
    assert f"*LinkedIn:* <{LINKEDIN}|{LINKEDIN}>" in body
    assert "*Assigned:* sdr@gloo.example" in body
    assert message["blocks"][1]["text"]["text"] == "> Sounds great, let's talk."


# --------------------------------------------------------------- opt-in


@pytest.mark.asyncio
async def test_enable_registers_webhook_and_stores_id() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign(enabled=False))
    service, heyreach, _, _ = _service(repository)

    updated = await service.configure_heyreach_campaign(
        HEYREACH_CAMPAIGN_ID, enabled=True, sdr_id=SDR_ID
    )

    assert heyreach.saved == [
        {
            "name": "Gloo speed to lead",
            "webhook_url": "https://api.example.com/api/v1/heyreach/webhooks/token",
            "event_type": "LEAD_TAG_UPDATED",
            "campaign_ids": [HEYREACH_CAMPAIGN_ID],
            "webhook_id": None,
        }
    ]
    assert updated["speed_to_lead_enabled"] is True
    assert updated["speed_to_lead_sdr_id"] == SDR_ID
    assert updated["heyreach_webhook_id"] == "wh-9"


@pytest.mark.asyncio
async def test_re_enable_updates_existing_webhook() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    service, heyreach, _, _ = _service(repository)

    await service.configure_heyreach_campaign(
        HEYREACH_CAMPAIGN_ID, enabled=True, sdr_id=str(uuid4())
    )

    assert heyreach.saved[0]["webhook_id"] == "wh-9"


@pytest.mark.asyncio
async def test_disable_deletes_webhook_and_clears_fields() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    service, heyreach, _, _ = _service(repository)

    updated = await service.configure_heyreach_campaign(
        HEYREACH_CAMPAIGN_ID, enabled=False, sdr_id=None
    )

    assert heyreach.deleted == ["wh-9"]
    assert updated["speed_to_lead_enabled"] is False
    assert updated["speed_to_lead_sdr_id"] is None
    assert updated["heyreach_webhook_id"] is None


@pytest.mark.asyncio
async def test_disable_tolerates_already_deleted_webhook() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    heyreach = FakeHeyReach()
    heyreach.delete_error = HeyReachError("gone", status_code=404)
    service, _, _, _ = _service(repository, heyreach=heyreach)

    updated = await service.configure_heyreach_campaign(
        HEYREACH_CAMPAIGN_ID, enabled=False, sdr_id=None
    )

    assert updated["heyreach_webhook_id"] is None


@pytest.mark.asyncio
async def test_disable_propagates_other_heyreach_errors() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign())
    heyreach = FakeHeyReach()
    heyreach.delete_error = HeyReachError("down", status_code=503)
    service, _, _, _ = _service(repository, heyreach=heyreach)

    with pytest.raises(HeyReachError):
        await service.configure_heyreach_campaign(
            HEYREACH_CAMPAIGN_ID, enabled=False, sdr_id=None
        )
    assert repository.heyreach_campaign_updates == []


@pytest.mark.asyncio
async def test_enable_requires_sdr_and_known_campaign() -> None:
    repository = FakeSpeedToLeadRepository(None, _heyreach_campaign(enabled=False))
    service, heyreach, _, _ = _service(repository)

    with pytest.raises(SpeedToLeadValidationError):
        await service.configure_heyreach_campaign(
            HEYREACH_CAMPAIGN_ID, enabled=True, sdr_id=None
        )
    with pytest.raises(SpeedToLeadNotFoundError):
        await service.configure_heyreach_campaign(999, enabled=True, sdr_id=SDR_ID)
    assert heyreach.saved == []
