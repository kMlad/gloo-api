from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.phone_enrichment.providers.base import ProviderResult
from app.phone_enrichment.schemas import PhoneEnrichmentRequest
from app.phone_enrichment.service import (
    InvalidWebhookError,
    PhoneEnrichmentService,
    ReconciliationTooSoonError,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def test_fullenrich_omits_company_linkedin_and_uses_company_identity() -> None:
    payload = PhoneEnrichmentService._fullenrich_input(
        "run-1",
        "item-1",
        {
            "id": "lead-1",
            "first_name": "Pat",
            "last_name": "Lee",
            "company_name": "Acme",
            "website": "https://www.acme.example/about",
            "linkedin_profile": "https://www.linkedin.com/company/acme/",
        },
    )

    assert payload is not None
    assert "linkedin_url" not in payload
    assert payload["domain"] == "acme.example"
    assert payload["company_name"] == "Acme"


class FakeEnrichmentRepository:
    def __init__(self, lead: dict) -> None:
        self.lead = lead
        self.runs: dict[str, dict] = {}
        self.items: dict[str, dict] = {}
        self.attempts: dict[str, dict] = {}
        self.reconciliation_available = False

    async def get_run_by_idempotency_key(self, key):
        return next(
            (run for run in self.runs.values() if run["idempotency_key"] == key),
            None,
        )

    async def get_selected_leads(self, lead_ids):
        return [deepcopy(self.lead)] if str(self.lead["id"]) in lead_ids else []

    async def list_eligible_leads(self, limit):
        return [deepcopy(self.lead)] if limit else []

    async def create_run(self, **values):
        run_id = str(uuid4())
        run = {
            "id": run_id,
            **values,
            "status": "running",
            "leads_enriched": 0,
            "leads_not_found": 0,
            "leads_skipped": 0,
            "leads_failed": 0,
            "fullenrich_job_id": None,
            "errors": [],
            "last_reconciled_at": None,
            "started_at": _now(),
            "completed_at": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.runs[run_id] = run
        return deepcopy(run)

    async def get_run(self, run_id):
        run = self.runs.get(run_id)
        return deepcopy(run) if run else None

    async def update_run(self, run_id, values):
        self.runs[run_id].update(values)
        self.runs[run_id]["updated_at"] = _now()
        return deepcopy(self.runs[run_id])

    async def create_item(self, run_id, lead_id, *, status="running"):
        item_id = str(uuid4())
        item = {
            "id": item_id,
            "run_id": run_id,
            "lead_id": lead_id,
            "status": status,
            "final_phone_number": None,
            "final_source": None,
            "had_provider_error": False,
            "error_message": None,
            "started_at": _now(),
            "completed_at": _now() if status.startswith("skipped") else None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.items[item_id] = item
        return deepcopy(item)

    async def update_item(self, item_id, values):
        self.items[item_id].update(values)
        self.items[item_id]["updated_at"] = _now()
        return deepcopy(self.items[item_id])

    async def get_item(self, item_id):
        item = self.items.get(item_id)
        return deepcopy(item) if item else None

    async def create_attempt(self, **values):
        attempt_id = str(uuid4())
        attempt = {
            "id": attempt_id,
            **values,
            "request_payload": values.get("request_payload") or {},
            "response_payload": None,
            "response_headers": {},
            "http_status": None,
            "external_request_id": None,
            "phone_candidate": None,
            "error_code": None,
            "error_message": None,
            "started_at": _now(),
            "completed_at": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.attempts[attempt_id] = attempt
        return deepcopy(attempt)

    async def update_attempt(self, attempt_id, values):
        self.attempts[attempt_id].update(values)
        self.attempts[attempt_id]["updated_at"] = _now()
        return deepcopy(self.attempts[attempt_id])

    async def update_empty_lead_phone(self, lead_id, phone, source):
        if self.lead.get("enriched_phone_number"):
            return False
        self.lead["enriched_phone_number"] = phone
        self.lead["phone_source"] = source
        return True

    async def list_items(self, run_id):
        return [deepcopy(item) for item in self.items.values() if item["run_id"] == run_id]

    async def get_run_detail(self, run_id):
        run = self.runs.get(run_id)
        if not run:
            return None
        detail = deepcopy(run)
        detail["items"] = []
        for item in self.items.values():
            if item["run_id"] != run_id:
                continue
            item_copy = deepcopy(item)
            item_copy["attempts"] = [
                deepcopy(attempt)
                for attempt in self.attempts.values()
                if attempt["item_id"] == item["id"]
            ]
            detail["items"].append(item_copy)
        return detail

    async def get_attempts_by_external_id(self, external_id):
        return [
            deepcopy(attempt)
            for attempt in self.attempts.values()
            if attempt.get("external_request_id") == external_id
        ]

    async def acquire_reconciliation(self, run_id, minimum_seconds):
        return self.reconciliation_available


class ProviderStub:
    def __init__(self, name, calls, result):
        self.name = name
        self.calls = calls
        self.result = result

    async def find_phone(self, lead, request_id):
        self.calls.append(self.name)
        return deepcopy(self.result)


class FullEnrichStub:
    def __init__(self, calls, result=None):
        self.calls = calls
        self.result = result or ProviderResult(
            status="failed",
            request_payload={},
            error_message="should not be called",
        )

    async def submit(self, contacts, webhook_url):
        self.calls.append("fullenrich")
        return deepcopy(self.result)

    async def get_result(self, enrichment_id):
        raise AssertionError("not expected")


def _service(repository, calls, leadmagic, prospeo, airscale, fullenrich=None):
    return PhoneEnrichmentService(
        repository,
        ProviderStub("leadmagic", calls, leadmagic),
        ProviderStub("prospeo", calls, prospeo),
        ProviderStub("airscale", calls, airscale),
        fullenrich or FullEnrichStub(calls),
        public_api_base_url="https://api.example.com",
        fullenrich_webhook_token="webhook-token-with-at-least-32-characters",
        concurrency=2,
        reconcile_seconds=300,
    )


@pytest.mark.asyncio
async def test_waterfall_stops_at_first_valid_provider_and_replay_is_idempotent() -> None:
    lead_id = str(uuid4())
    repository = FakeEnrichmentRepository(
        {
            "id": lead_id,
            "email": "pat@example.com",
            "first_name": "Pat",
            "last_name": "Lee",
            "company_name": "Acme",
            "website": "https://acme.example",
            "linkedin_profile": "https://linkedin.com/in/pat",
            "enriched_phone_number": None,
            "phone_source": None,
            "inbound_replies": [
                {"id": "reply-1", "received_at": _now(), "body": "Interested"}
            ],
        }
    )
    calls: list[str] = []
    service = _service(
        repository,
        calls,
        ProviderResult(status="not_found", request_payload={"email": "pat@example.com"}),
        ProviderResult(
            status="found", request_payload={}, phone="+1 (415) 555-2671"
        ),
        ProviderResult(status="found", request_payload={}, phone="+442079460958"),
    )

    request = PhoneEnrichmentRequest(lead_ids=[lead_id])
    result = await service.run(request, "idempotency-key-1")
    replay = await service.run(request, "idempotency-key-1")

    assert result["status"] == "succeeded"
    assert replay["id"] == result["id"]
    assert repository.lead["enriched_phone_number"] == "+14155552671"
    assert repository.lead["phone_source"] == "prospeo"
    assert calls == ["leadmagic", "prospeo"]
    assert [
        attempt["provider"] for attempt in result["items"][0]["attempts"]
    ] == ["smartlead_signature", "leadmagic", "prospeo"]


@pytest.mark.asyncio
async def test_signature_phone_avoids_all_credit_providers() -> None:
    lead_id = str(uuid4())
    repository = FakeEnrichmentRepository(
        {
            "id": lead_id,
            "email": "pat@example.com",
            "enriched_phone_number": None,
            "phone_source": None,
            "inbound_replies": [
                {
                    "id": "reply-1",
                    "received_at": _now(),
                    "body": "Thanks,\nPat\n+44 20 7946 0958",
                }
            ],
        }
    )
    calls: list[str] = []
    no_result = ProviderResult(status="not_found", request_payload={})
    service = _service(repository, calls, no_result, no_result, no_result)

    result = await service.run(
        PhoneEnrichmentRequest(lead_ids=[lead_id]), "idempotency-key-2"
    )

    assert result["status"] == "succeeded"
    assert repository.lead["phone_source"] == "smartlead_signature"
    assert calls == []


@pytest.mark.asyncio
async def test_existing_enriched_phone_is_never_overwritten() -> None:
    lead_id = str(uuid4())
    repository = FakeEnrichmentRepository(
        {
            "id": lead_id,
            "email": "pat@example.com",
            "enriched_phone_number": "+442079460958",
            "phone_source": "leadmagic",
            "inbound_replies": [
                {
                    "id": "reply-1",
                    "received_at": _now(),
                    "body": "New number +1 415 555 2671",
                }
            ],
        }
    )
    calls: list[str] = []
    no_result = ProviderResult(status="not_found", request_payload={})
    service = _service(repository, calls, no_result, no_result, no_result)

    result = await service.run(
        PhoneEnrichmentRequest(lead_ids=[lead_id]), "idempotency-key-existing"
    )

    assert result["items"][0]["status"] == "skipped_existing"
    assert repository.lead["enriched_phone_number"] == "+442079460958"
    assert calls == []


@pytest.mark.asyncio
async def test_fullenrich_callback_completes_waiting_run_and_is_replay_safe() -> None:
    lead_id = str(uuid4())
    repository = FakeEnrichmentRepository(
        {
            "id": lead_id,
            "email": "pat@acme.example",
            "first_name": "Pat",
            "last_name": "Lee",
            "company_name": "Acme",
            "website": "https://acme.example",
            "linkedin_profile": "https://linkedin.com/in/pat",
            "enriched_phone_number": None,
            "phone_source": None,
            "inbound_replies": [],
        }
    )
    calls: list[str] = []
    no_result = ProviderResult(status="not_found", request_payload={})
    fullenrich = FullEnrichStub(
        calls,
        ProviderResult(
            status="waiting",
            request_payload={},
            response_payload={"enrichment_id": "job-1"},
            http_status=200,
            external_request_id="job-1",
        ),
    )
    service = _service(
        repository, calls, no_result, no_result, no_result, fullenrich
    )

    waiting = await service.run(
        PhoneEnrichmentRequest(lead_ids=[lead_id]), "idempotency-key-3"
    )
    with pytest.raises(ReconciliationTooSoonError):
        await service.reconcile(waiting["id"])
    item = waiting["items"][0]
    payload = {
        "id": "job-1",
        "status": "FINISHED",
        "data": [
            {
                "custom": {
                    "run_id": waiting["id"],
                    "item_id": item["id"],
                    "lead_id": lead_id,
                },
                "contact_info": {
                    "most_probable_phone": {"number": "+1 415 555 2671"}
                },
            }
        ],
    }
    token = "webhook-token-with-at-least-32-characters"
    with pytest.raises(InvalidWebhookError):
        await service.process_fullenrich_webhook("incorrect-token", payload)
    await service.process_fullenrich_webhook(token, payload)
    await service.process_fullenrich_webhook(token, payload)
    completed = await service.get_run_detail(waiting["id"])

    assert waiting["status"] == "waiting"
    assert completed["status"] == "succeeded"
    assert repository.lead["phone_source"] == "fullenrich"
    assert calls == ["leadmagic", "prospeo", "airscale", "fullenrich"]
