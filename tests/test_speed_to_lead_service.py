from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.notifications.slack import SlackError
from app.phone_enrichment.service import (
    EnrichmentValidationError,
    PhoneEnrichedEvent,
)
from app.smartlead.client import SmartLeadError
from app.speed_to_lead.notifications import SpeedToLeadNotifier
from app.speed_to_lead.service import (
    SpeedToLeadNotFoundError,
    SpeedToLeadService,
    SpeedToLeadValidationError,
)

CAMPAIGN_ID = 10
SDR_ID = str(uuid4())


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FakeSpeedToLeadRepository:
    def __init__(self, campaign: dict | None) -> None:
        self.campaign = campaign
        self.events: dict[str, dict] = {}
        self.assignments: list[tuple[str, str]] = []
        self.assigned_lead_ids: set[str] = set()
        self.campaign_updates: list[dict] = []
        self.existing_conversation: dict | None = None
        self.leads_by_id: dict[str, dict] = {}

    async def get_smartlead_campaign(self, campaign_id):
        if self.campaign and self.campaign["smartlead_campaign_id"] == campaign_id:
            return deepcopy(self.campaign)
        return None

    async def update_smartlead_campaign(
        self, campaign_id, *, enabled, sdr_id, webhook_id
    ):
        if self.campaign is None:
            return None
        self.campaign_updates.append(
            {"enabled": enabled, "sdr_id": sdr_id, "webhook_id": webhook_id}
        )
        self.campaign.update(
            {
                "speed_to_lead_enabled": enabled,
                "speed_to_lead_sdr_id": sdr_id,
                "smartlead_webhook_id": webhook_id,
                "updated_at": _now(),
            }
        )
        return deepcopy(self.campaign)

    async def find_smartlead_conversation(self, *, campaign_id, email_normalized):
        return deepcopy(self.existing_conversation)

    async def get_event_by_dedupe_key(self, dedupe_key):
        return next(
            (
                deepcopy(event)
                for event in self.events.values()
                if event["dedupe_key"] == dedupe_key
            ),
            None,
        )

    async def insert_event(self, values):
        if any(e["dedupe_key"] == values["dedupe_key"] for e in self.events.values()):
            return None
        event = {
            "id": str(uuid4()),
            **values,
            "enrichment_run_id": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.events[event["id"]] = event
        return deepcopy(event)

    async def update_event(self, event_id, values):
        self.events[event_id].update(values)
        return deepcopy(self.events[event_id])

    async def get_event_by_enrichment_run(self, enrichment_run_id):
        return next(
            (
                deepcopy(event)
                for event in self.events.values()
                if event.get("enrichment_run_id") == enrichment_run_id
            ),
            None,
        )

    async def get_user_email(self, user_id):
        return "sdr@gloo.example" if user_id == SDR_ID else None

    async def list_events(
        self, *, limit, offset, include_handled=False, visible_to_sdr_id=None
    ):
        self.list_calls = getattr(self, "list_calls", [])
        self.list_calls.append(
            {
                "limit": limit,
                "offset": offset,
                "include_handled": include_handled,
                "visible_to_sdr_id": visible_to_sdr_id,
            }
        )
        rows = sorted(
            self.events.values(), key=lambda e: e["replied_at"], reverse=True
        )
        return (
            [
                {**deepcopy(event), "lead": deepcopy(self.leads_by_id[event["lead_id"]])}
                for event in rows[offset : offset + limit]
            ],
            len(rows),
        )

    async def get_enrichment_runs(self, run_ids):
        return {run_id: {"id": run_id, "status": "running"} for run_id in run_ids}

    async def assign_lead_if_unassigned(self, lead_id, *, sdr_id):
        if lead_id in self.assigned_lead_ids:
            return False
        self.assigned_lead_ids.add(lead_id)
        self.assignments.append((lead_id, sdr_id))
        return True


class FakeLeadRepository:
    def __init__(self, *, assigned_sdr_id: str | None = None) -> None:
        self.assigned_sdr_id = assigned_sdr_id
        self.upserts: list[dict] = []
        self.replies: list[dict] = []
        self.lead_id = str(uuid4())
        self.conversation_id = str(uuid4())

    async def upsert_lead_conversation(self, **values):
        self.upserts.append(values)
        return {
            "lead": {
                "id": self.lead_id,
                "email": values["email"],
                "assigned_sdr_id": self.assigned_sdr_id,
                **values["typed_properties"],
            },
            "conversation": {
                "id": self.conversation_id,
                **values["conversation"],
            },
        }

    async def upsert_reply(self, values):
        self.replies.append(values)
        return values

    async def decorate_leads(self, leads):
        for lead in leads:
            lead["positive_conversation_count"] = 1
            lead["speed_to_lead_at"] = "decorated"
        return leads

    async def get_campaigns_by_ids(self, campaign_ids):
        return [
            {"smartlead_campaign_id": campaign_id, "name": "Campaign"}
            for campaign_id in campaign_ids
            if campaign_id == CAMPAIGN_ID
        ]

    async def get_heyreach_campaigns_by_ids(self, campaign_ids):
        return []


class FakeSmartLead:
    def __init__(self, categories=None, *, fail_categories: bool = False) -> None:
        self.categories = categories or [
            {"id": 1, "name": "Interested", "sentiment_type": "positive"},
            {"id": 2, "name": "Not Interested", "sentiment_type": "negative"},
        ]
        self.category_calls = 0
        self.fail_categories = fail_categories
        self.saved: list[dict] = []
        self.deleted: list[tuple[int, str]] = []
        self.delete_error: SmartLeadError | None = None

    async def get_categories(self):
        self.category_calls += 1
        if self.fail_categories:
            raise SmartLeadError("down")
        return deepcopy(self.categories)

    async def save_webhook(self, campaign_id, **values):
        self.saved.append({"campaign_id": campaign_id, **values})
        return {"id": "555", "webhook_url": values["webhook_url"]}

    async def delete_webhook(self, campaign_id, webhook_id):
        self.deleted.append((campaign_id, webhook_id))
        if self.delete_error is not None:
            raise self.delete_error


class FakePhoneEnrichment:
    def __init__(self, *, status: str = "queued", error: Exception | None = None):
        self.status = status
        self.error = error
        self.started: list[tuple[list[str], str]] = []
        self.executed: list[str] = []

    async def start(self, request, idempotency_key, *, created_by=None):
        if self.error is not None:
            raise self.error
        self.started.append(([str(v) for v in request.lead_ids], idempotency_key))
        return {"id": str(uuid4()), "status": self.status}

    async def execute_background(self, run_id):
        self.executed.append(run_id)


def _campaign(*, enabled: bool = True, sdr_id: str | None = SDR_ID) -> dict:
    return {
        "smartlead_campaign_id": CAMPAIGN_ID,
        "name": "Campaign",
        "enabled": True,
        "reply_types": ["positive"],
        "speed_to_lead_enabled": enabled,
        "speed_to_lead_sdr_id": sdr_id if enabled else None,
        "smartlead_webhook_id": "555" if enabled else None,
        "created_at": _now(),
        "updated_at": _now(),
    }


def _payload(**overrides) -> dict:
    payload = {
        "event_type": "LEAD_CATEGORY_UPDATED",
        "lead_id": 42,
        "lead_email": "Pat@Example.com",
        "lead_name": "Pat Lee",
        "lead_data": {
            "email": "Pat@Example.com",
            "first_name": "Pat",
            "last_name": "Lee",
            "phone_number": "+1 555 0100",
            "company_name": "Acme",
            "custom_fields": {"tier": "gold"},
            "category": {"name": "Interested", "sentiment_type": "positive"},
        },
        "category": "Interested",
        "lead_category_id": 1,
        "campaign_name": "Campaign",
        "campaign_id": CAMPAIGN_ID,
        "from": "pat@example.com",
        "to": "sdr@gloo.example",
        "history": [
            {
                "type": "SENT",
                "time": "2026-09-07T09:00:00Z",
                "email_body": "Hi Pat",
                "subject": "Intro",
            },
            {
                "type": "REPLY",
                "time": "2026-09-07T09:30:00Z",
                "email_body": "Yes, let's talk.   Call me.",
                "subject": "Re: Intro",
            },
        ],
        "lastReply": {
            "type": "REPLY",
            "time": "2026-09-07T09:30:00Z",
            "email_body": "Yes, let's talk.   Call me.",
        },
    }
    payload.update(overrides)
    return payload


class SlackStub:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.messages: list[dict] = []

    async def post_message(self, channel, text, *, blocks=None, thread_ts=None):
        if self.error is not None:
            raise self.error
        self.messages.append(
            {"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts}
        )
        return f"{len(self.messages)}.0"


def _notifier(slack: SlackStub | None) -> SpeedToLeadNotifier:
    return SpeedToLeadNotifier(
        slack, channel_id="C1", app_base_url="https://app.example.com"
    )


def _service(
    repository,
    leads=None,
    smartlead=None,
    enrichment=None,
    notifier=None,
) -> tuple[SpeedToLeadService, FakeLeadRepository, FakeSmartLead, FakePhoneEnrichment]:
    leads = leads or FakeLeadRepository()
    smartlead = smartlead or FakeSmartLead()
    enrichment = enrichment or FakePhoneEnrichment()
    service = SpeedToLeadService(
        repository,
        leads,
        smartlead,
        enrichment,
        webhook_url="https://api.example.com/api/v1/smartlead/webhooks/token",
        notifier=notifier,
    )
    return service, leads, smartlead, enrichment


@pytest.mark.asyncio
async def test_positive_category_creates_event_assigns_and_enriches() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, leads, _, enrichment = _service(repository)

    result = await service.handle_smartlead_category_update(_payload())

    assert result.outcome == "processed"
    event = result.event
    assert event is not None
    assert event["platform"] == "smartlead"
    assert event["lead_id"] == leads.lead_id
    assert event["conversation_id"] == leads.conversation_id
    assert event["smartlead_campaign_id"] == CAMPAIGN_ID
    assert event["category_id"] == 1
    assert event["category_name"] == "Interested"
    assert event["reply_excerpt"] == "Yes, let's talk. Call me."
    assert event["replied_at"] == "2026-09-07T09:30:00Z"
    assert event["enrichment_run_id"] is not None

    upsert = leads.upserts[0]
    assert upsert["email_normalized"] == "pat@example.com"
    assert upsert["typed_properties"]["first_name"] == "Pat"
    assert upsert["typed_properties"]["smartlead_phone_number"] == "+1 555 0100"
    assert upsert["custom_properties"] == {"tier": "gold"}
    conversation = upsert["conversation"]
    assert conversation["reply_type"] == "positive"
    assert conversation["smartlead_lead_id"] == "42"
    assert conversation["positive_category_id"] == 1
    assert conversation["qualified_at"] == "2026-09-07T09:30:00Z"
    assert conversation["smartlead_campaign_lead_map_id"] == (
        f"fallback:{CAMPAIGN_ID}:pat@example.com"
    )
    assert len(leads.replies) == 1
    assert leads.replies[0]["direction"] == "inbound"
    assert leads.replies[0]["body"] == "Yes, let's talk.   Call me."

    assert repository.assignments == [(leads.lead_id, SDR_ID)]
    assert enrichment.started == [
        ([leads.lead_id], f"speed-to-lead:{event['id']}")
    ]
    assert enrichment.executed == [event["enrichment_run_id"]]


@pytest.mark.asyncio
async def test_non_positive_category_is_skipped() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, leads, _, enrichment = _service(repository)

    result = await service.handle_smartlead_category_update(
        _payload(
            lead_category_id=2,
            category="Not Interested",
            lead_data={
                "email": "pat@example.com",
                "category": {"name": "Not Interested", "sentiment_type": "negative"},
            },
        )
    )

    assert result.outcome == "not_positive"
    assert leads.upserts == []
    assert repository.events == {}
    assert enrichment.started == []


@pytest.mark.asyncio
async def test_sentiment_map_wins_over_payload_sentiment() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, leads, _, _ = _service(repository)

    # Category 2 is negative in SmartLead even if the payload claims positive.
    result = await service.handle_smartlead_category_update(
        _payload(lead_category_id=2)
    )

    assert result.outcome == "not_positive"
    assert leads.upserts == []


@pytest.mark.asyncio
async def test_falls_back_to_payload_sentiment_when_categories_unavailable() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, _, _, _ = _service(
        repository, smartlead=FakeSmartLead(fail_categories=True)
    )

    result = await service.handle_smartlead_category_update(_payload())

    assert result.outcome == "processed"


@pytest.mark.asyncio
async def test_category_lookup_is_cached() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, _, smartlead, _ = _service(repository)

    await service.handle_smartlead_category_update(_payload())
    await service.handle_smartlead_category_update(
        _payload(lastReply={"type": "REPLY", "time": "2026-09-07T10:00:00Z"})
    )

    assert smartlead.category_calls == 1


@pytest.mark.asyncio
async def test_disabled_campaign_is_ignored() -> None:
    repository = FakeSpeedToLeadRepository(_campaign(enabled=False))
    service, leads, smartlead, enrichment = _service(repository)

    result = await service.handle_smartlead_category_update(_payload())

    assert result.outcome == "campaign_not_enabled"
    assert leads.upserts == []
    assert smartlead.category_calls == 0
    assert enrichment.started == []


@pytest.mark.asyncio
async def test_unknown_campaign_is_ignored() -> None:
    repository = FakeSpeedToLeadRepository(None)
    service, leads, _, _ = _service(repository)

    result = await service.handle_smartlead_category_update(_payload())

    assert result.outcome == "campaign_not_enabled"
    assert leads.upserts == []


@pytest.mark.asyncio
async def test_other_event_types_are_ignored() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, leads, _, _ = _service(repository)

    result = await service.handle_smartlead_category_update(
        _payload(event_type="EMAIL_REPLY")
    )

    assert result.outcome == "ignored_event"
    assert leads.upserts == []


@pytest.mark.asyncio
async def test_duplicate_delivery_is_idempotent() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, leads, _, enrichment = _service(repository)

    first = await service.handle_smartlead_category_update(_payload())
    second = await service.handle_smartlead_category_update(_payload())

    assert first.outcome == "processed"
    assert second.outcome == "duplicate"
    assert len(repository.events) == 1
    assert len(leads.upserts) == 1
    assert len(enrichment.started) == 1


@pytest.mark.asyncio
async def test_insert_race_is_reported_as_duplicate() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())

    async def missing(_key):
        return None

    repository.get_event_by_dedupe_key = missing  # type: ignore[method-assign]
    service, _, _, enrichment = _service(repository)

    await service.handle_smartlead_category_update(_payload())
    second = await service.handle_smartlead_category_update(_payload())

    assert second.outcome == "duplicate"
    assert len(enrichment.started) == 1


@pytest.mark.asyncio
async def test_already_assigned_lead_keeps_its_sdr() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    other_sdr = str(uuid4())
    service, _, _, _ = _service(
        repository, leads=FakeLeadRepository(assigned_sdr_id=other_sdr)
    )

    result = await service.handle_smartlead_category_update(_payload())

    assert result.outcome == "processed"
    assert repository.assignments == []


@pytest.mark.asyncio
async def test_campaign_without_sdr_does_not_assign() -> None:
    campaign = _campaign()
    campaign["speed_to_lead_sdr_id"] = None
    repository = FakeSpeedToLeadRepository(campaign)
    service, _, _, _ = _service(repository)

    result = await service.handle_smartlead_category_update(_payload())

    assert result.outcome == "processed"
    assert repository.assignments == []


@pytest.mark.asyncio
async def test_existing_conversation_map_id_is_reused() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    repository.existing_conversation = {
        "id": str(uuid4()),
        "smartlead_campaign_lead_map_id": "map-77",
    }
    service, leads, _, _ = _service(repository)

    await service.handle_smartlead_category_update(_payload())

    assert (
        leads.upserts[0]["conversation"]["smartlead_campaign_lead_map_id"] == "map-77"
    )


@pytest.mark.asyncio
async def test_enrichment_failure_keeps_event() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, _, _, enrichment = _service(
        repository,
        enrichment=FakePhoneEnrichment(error=EnrichmentValidationError("nope")),
    )

    result = await service.handle_smartlead_category_update(_payload())

    assert result.outcome == "processed"
    assert result.event is not None
    assert result.event["enrichment_run_id"] is None
    assert enrichment.executed == []


@pytest.mark.asyncio
async def test_finished_enrichment_run_is_not_executed() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, _, _, enrichment = _service(
        repository, enrichment=FakePhoneEnrichment(status="succeeded")
    )

    result = await service.handle_smartlead_category_update(_payload())

    assert result.event is not None
    assert result.event["enrichment_run_id"] is not None
    assert enrichment.executed == []


@pytest.mark.asyncio
async def test_missing_email_is_invalid() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, leads, _, _ = _service(repository)

    result = await service.handle_smartlead_category_update(
        _payload(lead_email=None, lead_data={"category": {"sentiment_type": "positive"}})
    )

    assert result.outcome == "invalid_payload"
    assert leads.upserts == []


@pytest.mark.asyncio
async def test_payload_without_history_uses_event_timestamp() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, leads, _, _ = _service(repository)

    result = await service.handle_smartlead_category_update(
        _payload(
            history=None,
            lastReply=None,
            event_timestamp="2026-09-07T11:00:00+00:00",
            reply_body="Sure",
        )
    )

    assert result.outcome == "processed"
    assert result.event is not None
    assert result.event["replied_at"] == "2026-09-07T11:00:00Z"
    assert result.event["reply_excerpt"] == "Sure"
    assert leads.replies == []
    assert leads.upserts[0]["conversation"]["qualified_at"] == "2026-09-07T11:00:00Z"


@pytest.mark.asyncio
async def test_background_boundary_swallows_errors() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())

    async def boom(_campaign_id):
        raise RuntimeError("db down")

    repository.get_smartlead_campaign = boom  # type: ignore[method-assign]
    service, _, _, _ = _service(repository)

    await service.process_smartlead_webhook(_payload())


@pytest.mark.asyncio
async def test_enable_registers_webhook_and_stores_id() -> None:
    repository = FakeSpeedToLeadRepository(_campaign(enabled=False))
    service, _, smartlead, _ = _service(repository)

    campaign = await service.configure_smartlead_campaign(
        CAMPAIGN_ID, enabled=True, sdr_id=SDR_ID
    )

    assert campaign["speed_to_lead_enabled"] is True
    assert campaign["speed_to_lead_sdr_id"] == SDR_ID
    assert campaign["smartlead_webhook_id"] == "555"
    assert smartlead.saved == [
        {
            "campaign_id": CAMPAIGN_ID,
            "name": "Gloo speed to lead",
            "webhook_url": "https://api.example.com/api/v1/smartlead/webhooks/token",
            "event_types": ["LEAD_CATEGORY_UPDATED"],
            "categories": ["Interested"],
            "webhook_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_enable_rejects_when_smartlead_has_no_positive_categories() -> None:
    repository = FakeSpeedToLeadRepository(_campaign(enabled=False))
    smartlead = FakeSmartLead(
        categories=[{"id": 2, "name": "Not Interested", "sentiment_type": "negative"}]
    )
    service, _, _, _ = _service(repository, smartlead=smartlead)

    with pytest.raises(
        SpeedToLeadValidationError,
        match="no positive categories",
    ):
        await service.configure_smartlead_campaign(
            CAMPAIGN_ID, enabled=True, sdr_id=SDR_ID
        )

    assert smartlead.saved == []
    assert repository.campaign_updates == []


@pytest.mark.asyncio
async def test_re_enable_updates_existing_webhook() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, _, smartlead, _ = _service(repository)

    await service.configure_smartlead_campaign(
        CAMPAIGN_ID, enabled=True, sdr_id=SDR_ID
    )

    assert smartlead.saved[0]["webhook_id"] == "555"


@pytest.mark.asyncio
async def test_disable_deletes_webhook_and_clears_fields() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, _, smartlead, _ = _service(repository)

    campaign = await service.configure_smartlead_campaign(
        CAMPAIGN_ID, enabled=False, sdr_id=None
    )

    assert campaign["speed_to_lead_enabled"] is False
    assert campaign["speed_to_lead_sdr_id"] is None
    assert campaign["smartlead_webhook_id"] is None
    assert smartlead.deleted == [(CAMPAIGN_ID, "555")]


@pytest.mark.asyncio
async def test_disable_tolerates_already_deleted_webhook() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    smartlead = FakeSmartLead()
    smartlead.delete_error = SmartLeadError("gone", status_code=404)
    service, _, _, _ = _service(repository, smartlead=smartlead)

    campaign = await service.configure_smartlead_campaign(
        CAMPAIGN_ID, enabled=False, sdr_id=None
    )

    assert campaign["smartlead_webhook_id"] is None


@pytest.mark.asyncio
async def test_disable_propagates_other_smartlead_errors() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    smartlead = FakeSmartLead()
    smartlead.delete_error = SmartLeadError("boom", status_code=500)
    service, _, _, _ = _service(repository, smartlead=smartlead)

    with pytest.raises(SmartLeadError):
        await service.configure_smartlead_campaign(
            CAMPAIGN_ID, enabled=False, sdr_id=None
        )
    assert repository.campaign_updates == []


@pytest.mark.asyncio
async def test_enable_requires_sdr_and_known_campaign() -> None:
    service, _, _, _ = _service(FakeSpeedToLeadRepository(_campaign(enabled=False)))
    with pytest.raises(SpeedToLeadValidationError):
        await service.configure_smartlead_campaign(
            CAMPAIGN_ID, enabled=True, sdr_id=None
        )

    service, _, _, _ = _service(FakeSpeedToLeadRepository(None))
    with pytest.raises(SpeedToLeadNotFoundError):
        await service.configure_smartlead_campaign(
            CAMPAIGN_ID, enabled=True, sdr_id=SDR_ID
        )


@pytest.mark.asyncio
async def test_notification_is_skipped_without_slack_config() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, _, _, _ = _service(repository, notifier=_notifier(None))

    result = await service.handle_smartlead_category_update(_payload())

    assert result.event is not None
    assert result.event["notification_status"] == "skipped"
    assert result.event.get("slack_message_ts") is None


@pytest.mark.asyncio
async def test_alert_is_posted_before_enrichment_and_ts_is_stored() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    order: list[str] = []

    class OrderedEnrichment(FakePhoneEnrichment):
        async def start(self, request, idempotency_key, *, created_by=None):
            order.append("enrichment")
            return await super().start(request, idempotency_key, created_by=created_by)

    class OrderedSlack(SlackStub):
        async def post_message(self, channel, text, *, blocks=None, thread_ts=None):
            order.append("slack")
            return await super().post_message(
                channel, text, blocks=blocks, thread_ts=thread_ts
            )

    slack = OrderedSlack()
    service, _, _, _ = _service(
        repository, enrichment=OrderedEnrichment(), notifier=_notifier(slack)
    )

    result = await service.handle_smartlead_category_update(_payload())

    assert order == ["slack", "enrichment"]
    assert result.event is not None
    assert result.event["notification_status"] == "sent"
    assert result.event["slack_message_ts"] == "1.0"
    assert result.event["enrichment_run_id"] is not None
    message = slack.messages[0]
    assert message["channel"] == "C1"
    assert message["text"] == "New positive reply: Pat Lee (Acme) · Campaign"
    section = message["blocks"][0]["text"]["text"]
    assert "*Assigned:* sdr@gloo.example" in section
    assert "https://app.example.com/speed-to-lead" in section
    assert message["blocks"][1]["text"]["text"] == "> Yes, let's talk. Call me."


@pytest.mark.asyncio
async def test_alert_shows_existing_assignee_not_campaign_sdr() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    slack = SlackStub()
    other_sdr = str(uuid4())
    service, _, _, _ = _service(
        repository,
        leads=FakeLeadRepository(assigned_sdr_id=other_sdr),
        notifier=_notifier(slack),
    )

    await service.handle_smartlead_category_update(_payload())

    section = slack.messages[0]["blocks"][0]["text"]["text"]
    assert "*Assigned:* Assigned SDR" in section


@pytest.mark.asyncio
async def test_slack_failure_is_recorded_and_does_not_block_enrichment() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    slack = SlackStub(error=SlackError("Slack returned an error: channel_not_found"))
    service, _, _, enrichment = _service(repository, notifier=_notifier(slack))

    result = await service.handle_smartlead_category_update(_payload())

    assert result.outcome == "processed"
    assert result.event is not None
    assert result.event["notification_status"] == "failed"
    assert "channel_not_found" in result.event["notification_error"]
    assert result.event["enrichment_run_id"] is not None
    assert len(enrichment.started) == 1


@pytest.mark.asyncio
async def test_phone_result_is_threaded_under_the_alert() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    slack = SlackStub()
    service, leads, _, _ = _service(repository, notifier=_notifier(slack))
    result = await service.handle_smartlead_category_update(_payload())
    assert result.event is not None

    await service.handle_phone_enriched(
        PhoneEnrichedEvent(
            run_id=result.event["enrichment_run_id"],
            lead_id=leads.lead_id,
            phone="+14155552671",
            source="prospeo",
        )
    )

    assert len(slack.messages) == 2
    assert slack.messages[1]["thread_ts"] == "1.0"
    assert slack.messages[1]["text"] == "Phone found: +14155552671 (via Prospeo)"


@pytest.mark.asyncio
async def test_phone_result_without_matching_alert_is_ignored() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    slack = SlackStub()
    service, leads, _, _ = _service(repository, notifier=_notifier(slack))

    await service.handle_phone_enriched(
        PhoneEnrichedEvent(
            run_id=str(uuid4()), lead_id=leads.lead_id, phone="+1", source="prospeo"
        )
    )
    assert slack.messages == []

    # Alert failed (no ts stored): the phone follow-up has nothing to thread on.
    slack.error = SlackError("down")
    result = await service.handle_smartlead_category_update(_payload())
    slack.error = None
    assert result.event is not None
    await service.handle_phone_enriched(
        PhoneEnrichedEvent(
            run_id=result.event["enrichment_run_id"],
            lead_id=leads.lead_id,
            phone="+1",
            source="prospeo",
        )
    )
    assert slack.messages == []


@pytest.mark.asyncio
async def test_phone_notification_failure_is_logged_on_event() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    slack = SlackStub()
    service, leads, _, _ = _service(repository, notifier=_notifier(slack))
    result = await service.handle_smartlead_category_update(_payload())
    assert result.event is not None
    slack.error = SlackError("thread failed")

    await service.handle_phone_enriched(
        PhoneEnrichedEvent(
            run_id=result.event["enrichment_run_id"],
            lead_id=leads.lead_id,
            phone="+1",
            source="prospeo",
        )
    )

    stored = repository.events[result.event["id"]]
    assert stored["notification_status"] == "sent"
    assert stored["notification_error"] == "thread failed"


@pytest.mark.asyncio
async def test_phone_notification_no_ops_when_disabled() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, leads, _, _ = _service(repository)

    await service.handle_phone_enriched(
        PhoneEnrichedEvent(
            run_id=str(uuid4()), lead_id=leads.lead_id, phone="+1", source="prospeo"
        )
    )


@pytest.mark.asyncio
async def test_list_events_joins_leads_campaigns_and_enrichment() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, leads, _, _ = _service(repository)
    first = await service.handle_smartlead_category_update(_payload())
    second = await service.handle_smartlead_category_update(
        _payload(
            history=None,
            lastReply={"type": "REPLY", "time": "2026-09-07T10:00:00Z"},
        )
    )
    assert first.event is not None and second.event is not None
    repository.leads_by_id[leads.lead_id] = {
        "id": leads.lead_id,
        "email": "pat@example.com",
        "status": "new",
    }

    items, total = await service.list_events(
        limit=1, offset=0, include_handled=True, visible_to_sdr_id="sdr-9"
    )

    assert total == 2
    assert len(items) == 1
    item = items[0]
    assert item["id"] == second.event["id"]
    assert item["campaign_name"] == "Campaign"
    assert item["lead"]["id"] == leads.lead_id
    assert item["lead"]["speed_to_lead_at"] == "decorated"
    assert item["enrichment"] == {
        "id": second.event["enrichment_run_id"],
        "status": "running",
    }
    assert repository.list_calls == [
        {
            "limit": 1,
            "offset": 0,
            "include_handled": True,
            "visible_to_sdr_id": "sdr-9",
        }
    ]


@pytest.mark.asyncio
async def test_list_events_names_unknown_campaigns_and_missing_runs() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    lead_id = str(uuid4())
    repository.leads_by_id[lead_id] = {"id": lead_id, "status": "new"}
    await repository.insert_event(
        {
            "platform": "heyreach",
            "lead_id": lead_id,
            "smartlead_campaign_id": None,
            "heyreach_campaign_id": 77,
            "replied_at": "2026-09-07T10:00:00Z",
            "dedupe_key": "k",
        }
    )
    service, _, _, _ = _service(repository)

    items, total = await service.list_events(limit=10, offset=0)

    assert total == 1
    assert items[0]["campaign_name"] == "HeyReach campaign 77"
    assert items[0]["enrichment"] is None


HTML_REPLY = (
    '<div dir="ltr"><div><div><div>Hey Bisera, <br><br></div>Of course, I&#39;d love '
    "to learn more!<br><br></div>Awaiting your response!<br><br></div><div>Best, <br>"
    '</div><div>Kiril</div></div><br><div class="gmail_quote gmail_quote_container">'
    '<div dir="ltr" class="gmail_attr">On Mon, Sep 7, 2026 at 9:00 PM Bisera Loteska\n'
    "&lt;bisera@example.com [bisera@example.com]&gt; wrote:<br></div>"
    "<blockquote>Hi Kiril, quick question</blockquote></div>"
)


@pytest.mark.asyncio
async def test_reply_excerpt_strips_html_and_quoted_history() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, _, _, _ = _service(repository)
    history = [
        {
            "type": "REPLY",
            "time": "2026-09-07T09:30:00Z",
            "email_body": HTML_REPLY,
            "subject": "Re: Intro",
        }
    ]

    result = await service.handle_smartlead_category_update(
        _payload(history=history, lastReply=history[0])
    )

    assert result.event is not None
    assert result.event["reply_excerpt"] == (
        "Hey Bisera, Of course, I'd love to learn more! Awaiting your response! "
        "Best, Kiril"
    )


def _finished_run(run_id: str, lead_id: str, *, item_status: str = "failed") -> dict:
    return {
        "id": run_id,
        "status": "failed" if item_status == "failed" else "succeeded",
        "items": [
            {
                "id": str(uuid4()),
                "lead_id": lead_id,
                "status": item_status,
                "attempts": [
                    {"provider": "smartlead_signature", "sequence": 1, "status": "not_found"},
                    {"provider": "leadmagic", "sequence": 2, "status": "not_found"},
                    {
                        "provider": "prospeo",
                        "sequence": 3,
                        "status": "failed",
                        "error_code": "insufficient_credits",
                        "error_message": "Provider account has insufficient credits",
                    },
                    {"provider": "airscale", "sequence": 4, "status": "skipped_no_input"},
                    {"provider": "fullenrich", "sequence": 5, "status": "skipped_no_input"},
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_finished_run_without_phone_threads_a_summary() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    slack = SlackStub()
    service, leads, _, _ = _service(repository, notifier=_notifier(slack))
    result = await service.handle_smartlead_category_update(_payload())
    assert result.event is not None

    await service.handle_enrichment_finished(
        _finished_run(result.event["enrichment_run_id"], leads.lead_id)
    )

    assert len(slack.messages) == 2
    follow_up = slack.messages[1]
    assert follow_up["thread_ts"] == "1.0"
    assert follow_up["text"].startswith("No phone found.")
    assert "Prospeo: out of credits" in follow_up["text"]
    assert "AirScale: skipped (needs a LinkedIn URL)" in follow_up["text"]


@pytest.mark.asyncio
async def test_finished_run_with_phone_does_not_duplicate_the_phone_message() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    slack = SlackStub()
    service, leads, _, _ = _service(repository, notifier=_notifier(slack))
    result = await service.handle_smartlead_category_update(_payload())
    assert result.event is not None
    run_id = result.event["enrichment_run_id"]

    await service.handle_phone_enriched(
        PhoneEnrichedEvent(
            run_id=run_id, lead_id=leads.lead_id, phone="+14155552671", source="prospeo"
        )
    )
    await service.handle_enrichment_finished(
        _finished_run(run_id, leads.lead_id, item_status="enriched")
    )

    assert len(slack.messages) == 2
    assert slack.messages[1]["text"].startswith("Phone found:")


@pytest.mark.asyncio
async def test_finished_run_summary_ignores_unknown_runs_and_missing_ts() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    slack = SlackStub()
    service, leads, _, _ = _service(repository, notifier=_notifier(slack))

    await service.handle_enrichment_finished(_finished_run(str(uuid4()), leads.lead_id))
    assert slack.messages == []

    slack.error = SlackError("down")
    result = await service.handle_smartlead_category_update(_payload())
    slack.error = None
    assert result.event is not None
    await service.handle_enrichment_finished(
        _finished_run(result.event["enrichment_run_id"], leads.lead_id)
    )
    assert slack.messages == []


@pytest.mark.asyncio
async def test_finished_run_summary_failure_is_logged_on_event() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    slack = SlackStub()
    service, leads, _, _ = _service(repository, notifier=_notifier(slack))
    result = await service.handle_smartlead_category_update(_payload())
    assert result.event is not None
    slack.error = SlackError("summary failed")

    await service.handle_enrichment_finished(
        _finished_run(result.event["enrichment_run_id"], leads.lead_id)
    )

    stored = repository.events[result.event["id"]]
    assert stored["notification_status"] == "sent"
    assert stored["notification_error"] == "summary failed"


@pytest.mark.asyncio
async def test_finished_run_summary_no_ops_when_disabled() -> None:
    repository = FakeSpeedToLeadRepository(_campaign())
    service, leads, _, _ = _service(repository)

    await service.handle_enrichment_finished(_finished_run(str(uuid4()), leads.lead_id))


@pytest.mark.asyncio
async def test_run_already_finished_at_start_is_summarised_immediately() -> None:
    class FinishedEnrichment(FakePhoneEnrichment):
        async def start(self, request, idempotency_key, *, created_by=None):
            run = await super().start(request, idempotency_key, created_by=created_by)
            return {
                **run,
                "status": "succeeded",
                "items": [{"status": "skipped_existing", "attempts": []}],
            }

    repository = FakeSpeedToLeadRepository(_campaign())
    slack = SlackStub()
    service, _, _, enrichment = _service(
        repository, enrichment=FinishedEnrichment(), notifier=_notifier(slack)
    )

    result = await service.handle_smartlead_category_update(_payload())

    assert result.event is not None
    assert enrichment.executed == []
    assert len(slack.messages) == 2
    assert slack.messages[1]["thread_ts"] == "1.0"
    assert slack.messages[1]["text"] == "Phone already on file; enrichment skipped."
