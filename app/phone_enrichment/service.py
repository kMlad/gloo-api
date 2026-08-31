import asyncio
import hashlib
import hmac
import json
from typing import Any
from urllib.parse import urlparse

from postgrest.exceptions import APIError

from app.phone_enrichment.parser import extract_phone_from_replies, normalize_phone
from app.phone_enrichment.providers.airscale import AirScaleClient
from app.phone_enrichment.providers.base import ProviderResult
from app.phone_enrichment.providers.fullenrich import FullEnrichClient
from app.phone_enrichment.providers.leadmagic import LeadMagicClient
from app.phone_enrichment.providers.linkedin import person_linkedin_url
from app.phone_enrichment.providers.prospeo import ProspeoClient
from app.phone_enrichment.repository import (
    ActiveLeadEnrichmentError,
    EnrichmentRepository,
)
from app.phone_enrichment.schemas import PhoneEnrichmentRequest
from app.utils import to_iso, utc_now


class EnrichmentConflictError(Exception):
    pass


class EnrichmentValidationError(Exception):
    pass


class EnrichmentNotFoundError(Exception):
    pass


class ReconciliationTooSoonError(Exception):
    pass


class InvalidWebhookError(Exception):
    pass


class PhoneEnrichmentService:
    def __init__(
        self,
        repository: EnrichmentRepository,
        leadmagic: LeadMagicClient,
        prospeo: ProspeoClient,
        airscale: AirScaleClient,
        fullenrich: FullEnrichClient,
        *,
        public_api_base_url: str,
        fullenrich_webhook_token: str,
        concurrency: int,
        reconcile_seconds: int,
    ) -> None:
        self._repository = repository
        self._leadmagic = leadmagic
        self._prospeo = prospeo
        self._airscale = airscale
        self._fullenrich = fullenrich
        self._webhook_token = fullenrich_webhook_token
        self._concurrency = concurrency
        self._reconcile_seconds = reconcile_seconds
        self._webhook_url = (
            public_api_base_url.rstrip("/")
            + "/api/v1/phone-enrichments/webhooks/fullenrich/"
            + fullenrich_webhook_token
        )

    async def start(
        self,
        request: PhoneEnrichmentRequest,
        idempotency_key: str,
        *,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        fingerprint = self._fingerprint(request)
        existing = await self._repository.get_run_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise EnrichmentConflictError(
                    "Idempotency-Key was already used for a different request"
                )
            detail = await self._repository.get_run_detail(str(existing["id"]))
            if detail is None:
                raise EnrichmentNotFoundError("Enrichment run not found")
            return detail

        requested_ids = [str(value) for value in request.lead_ids or []]
        source_import_run_id = (
            str(request.source_import_run_id)
            if request.source_import_run_id is not None
            else None
        )
        if source_import_run_id is not None:
            try:
                leads = await self._repository.get_import_run_leads(
                    source_import_run_id
                )
            except ValueError as exc:
                raise EnrichmentValidationError(str(exc)) from exc
            selection_mode = "import_run"
        elif requested_ids:
            leads = await self._repository.get_selected_leads(requested_ids)
            found = {str(lead["id"]) for lead in leads}
            missing = [lead_id for lead_id in requested_ids if lead_id not in found]
            if missing:
                raise EnrichmentValidationError(
                    "Leads were not found: " + ", ".join(missing)
                )
            selection_mode = "selected"
        else:
            leads = await self._repository.list_eligible_leads(request.limit)
            selection_mode = "eligible"

        try:
            run = await self._repository.create_run(
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                selection_mode=selection_mode,
                requested_lead_ids=requested_ids,
                requested_limit=(
                    max(len(leads), 1)
                    if selection_mode == "import_run"
                    else request.limit
                ),
                leads_selected=len(leads),
                source_import_run_id=source_import_run_id,
                created_by=created_by,
            )
        except APIError as exc:
            if exc.code != "23505":
                raise
            existing = await self._repository.get_run_by_idempotency_key(
                idempotency_key
            )
            if existing is None or existing["request_fingerprint"] != fingerprint:
                raise EnrichmentConflictError(
                    "Idempotency-Key was already used for a different request"
                ) from exc
            detail = await self._repository.get_run_detail(str(existing["id"]))
            if detail is None:
                raise EnrichmentNotFoundError("Enrichment run not found") from exc
            return detail

        run_id = str(run["id"])
        queued = 0
        for lead in leads:
            lead_id = str(lead["id"])
            if str(lead.get("enriched_phone_number") or "").strip():
                await self._repository.create_item(
                    run_id, lead_id, status="skipped_existing"
                )
                continue
            try:
                await self._repository.create_item(run_id, lead_id)
            except ActiveLeadEnrichmentError:
                await self._repository.create_item(
                    run_id, lead_id, status="skipped_active"
                )
                continue
            queued += 1

        if queued == 0:
            await self._finalize_run(run_id)

        detail = await self._repository.get_run_detail(run_id)
        if detail is None:
            raise EnrichmentNotFoundError("Enrichment run not found")
        return detail

    async def execute_background(self, run_id: str) -> None:
        try:
            await self.execute(run_id)
        except Exception as exc:  # noqa: BLE001 - background worker boundary
            now = to_iso(utc_now())
            for item in await self._repository.list_items(run_id):
                if item["status"] not in {"queued", "running"}:
                    continue
                await self._repository.update_item(
                    str(item["id"]),
                    {
                        "status": "failed",
                        "had_provider_error": True,
                        "error_message": str(exc),
                        "completed_at": now,
                    },
                )
            await self._repository.update_run(
                run_id,
                {
                    "status": "failed",
                    "errors": [{"scope": "run", "message": str(exc)}],
                    "completed_at": now,
                },
            )

    async def execute(self, run_id: str) -> dict[str, Any]:
        detail = await self._repository.get_run_detail(run_id)
        if detail is None:
            raise EnrichmentNotFoundError("Enrichment run not found")
        if detail["status"] != "queued":
            return detail
        if await self._repository.claim_run(run_id) is None:
            current = await self._repository.get_run_detail(run_id)
            if current is None:
                raise EnrichmentNotFoundError("Enrichment run not found")
            return current

        queued_items = [
            item for item in detail.get("items", []) if item["status"] == "queued"
        ]
        leads = await self._repository.get_selected_leads(
            [str(item["lead_id"]) for item in queued_items]
        )
        leads_by_id = {str(lead["id"]): lead for lead in leads}
        active: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in queued_items:
            lead = leads_by_id.get(str(item["lead_id"]))
            if lead is None:
                await self._repository.update_item(
                    str(item["id"]),
                    {
                        "status": "failed",
                        "had_provider_error": True,
                        "error_message": "Lead disappeared before enrichment started",
                        "completed_at": to_iso(utc_now()),
                    },
                )
                continue
            updated_item = await self._repository.update_item(
                str(item["id"]), {"status": "running"}
            )
            active.append((updated_item, lead))

        semaphore = asyncio.Semaphore(self._concurrency)

        async def guarded_process(
            item: dict[str, Any], lead: dict[str, Any]
        ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
            async with semaphore:
                return await self._process_synchronous_providers(run_id, item, lead)

        results = await asyncio.gather(
            *(guarded_process(item, lead) for item, lead in active)
        )
        pending = [result for result in results if result is not None]
        if pending:
            await self._submit_fullenrich(run_id, pending)
        else:
            await self._finalize_run(run_id)

        detail = await self._repository.get_run_detail(run_id)
        if detail is None:
            raise EnrichmentNotFoundError("Enrichment run not found")
        return detail

    async def run(
        self, request: PhoneEnrichmentRequest, idempotency_key: str
    ) -> dict[str, Any]:
        run = await self.start(request, idempotency_key)
        if run["status"] in {"queued", "running"}:
            return await self.execute(str(run["id"]))
        return run

    async def _process_synchronous_providers(
        self, run_id: str, item: dict[str, Any], lead: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
        item_id = str(item["id"])
        lead_id = str(lead["id"])
        try:
            signature_attempt = await self._repository.create_attempt(
                run_id=run_id,
                item_id=item_id,
                lead_id=lead_id,
                provider="smartlead_signature",
                sequence=1,
                status="in_progress",
                request_payload={
                    "reply_ids": [
                        str(reply["id"])
                        for reply in lead.get("inbound_replies", [])
                        if reply.get("id") is not None
                    ]
                },
            )
            phone, raw_candidate = extract_phone_from_replies(
                lead.get("inbound_replies", [])
            )
            if phone is not None:
                await self._repository.update_attempt(
                    str(signature_attempt["id"]),
                    {
                        "status": "found",
                        "response_payload": {"raw_candidate": raw_candidate},
                        "phone_candidate": phone,
                        "completed_at": to_iso(utc_now()),
                    },
                )
                await self._complete_with_phone(
                    item_id, lead_id, phone, "smartlead_signature"
                )
                return None
            await self._repository.update_attempt(
                str(signature_attempt["id"]),
                {
                    "status": "not_found",
                    "response_payload": {"raw_candidate": None},
                    "completed_at": to_iso(utc_now()),
                },
            )

            providers = [
                ("leadmagic", 2, self._leadmagic),
                ("prospeo", 3, self._prospeo),
                ("airscale", 4, self._airscale),
            ]
            had_error = False
            for provider_name, sequence, client in providers:
                attempt = await self._repository.create_attempt(
                    run_id=run_id,
                    item_id=item_id,
                    lead_id=lead_id,
                    provider=provider_name,
                    sequence=sequence,
                    status="in_progress",
                )
                result = await client.find_phone(lead, str(attempt["id"]))
                normalized = normalize_phone(result.phone) if result.phone else None
                if result.status == "found" and normalized is None:
                    result.status = "failed"
                    result.error_code = "invalid_phone"
                    result.error_message = "Provider returned an invalid phone number"
                await self._persist_provider_result(
                    str(attempt["id"]), result, normalized
                )
                if normalized is not None and result.status == "found":
                    await self._complete_with_phone(
                        item_id, lead_id, normalized, provider_name
                    )
                    return None
                if result.status in {"failed", "rate_limited", "timed_out"}:
                    had_error = True
                    await self._repository.update_item(
                        item_id, {"had_provider_error": True}
                    )

            fullenrich_input = self._fullenrich_input(run_id, item_id, lead)
            fullenrich_attempt = await self._repository.create_attempt(
                run_id=run_id,
                item_id=item_id,
                lead_id=lead_id,
                provider="fullenrich",
                sequence=5,
                status="pending"
                if fullenrich_input is not None
                else "skipped_no_input",
                request_payload=fullenrich_input or {},
            )
            if fullenrich_input is None:
                await self._repository.update_attempt(
                    str(fullenrich_attempt["id"]),
                    {"completed_at": to_iso(utc_now())},
                )
                await self._complete_without_phone(item_id, had_error)
                return None
            return item, fullenrich_attempt, fullenrich_input
        except Exception:  # noqa: BLE001 - isolate one enrichment item
            await self._repository.update_item(
                item_id,
                {
                    "status": "failed",
                    "had_provider_error": True,
                    "error_message": "Unexpected enrichment error",
                    "completed_at": to_iso(utc_now()),
                },
            )
            return None

    async def _submit_fullenrich(
        self,
        run_id: str,
        pending: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> None:
        result = await self._fullenrich.submit(
            [entry[2] for entry in pending], self._webhook_url
        )
        now = to_iso(utc_now())
        if result.status == "waiting" and result.external_request_id:
            for item, attempt, _ in pending:
                await self._repository.update_attempt(
                    str(attempt["id"]),
                    {
                        "status": "waiting",
                        "response_payload": result.response_payload,
                        "response_headers": result.response_headers or {},
                        "http_status": result.http_status,
                        "external_request_id": result.external_request_id,
                    },
                )
                await self._repository.update_item(
                    str(item["id"]), {"status": "waiting"}
                )
            await self._repository.update_run(
                run_id,
                {
                    "status": "waiting",
                    "fullenrich_job_id": result.external_request_id,
                },
            )
            return

        for item, attempt, _ in pending:
            await self._repository.update_attempt(
                str(attempt["id"]),
                {
                    "status": result.status,
                    "response_payload": result.response_payload,
                    "response_headers": result.response_headers or {},
                    "http_status": result.http_status,
                    "error_code": result.error_code,
                    "error_message": result.error_message,
                    "completed_at": now,
                },
            )
            await self._repository.update_item(
                str(item["id"]),
                {
                    "status": "failed",
                    "had_provider_error": True,
                    "error_message": result.error_message
                    or "FullEnrich submission failed",
                    "completed_at": now,
                },
            )
        await self._finalize_run(run_id)

    async def process_fullenrich_webhook(
        self, token: str, payload: dict[str, Any]
    ) -> None:
        if not hmac.compare_digest(token, self._webhook_token):
            raise InvalidWebhookError("Invalid FullEnrich webhook token")
        await self._apply_fullenrich_payload(payload)

    async def _apply_fullenrich_payload(self, payload: dict[str, Any]) -> None:
        external_id = payload.get("id") or payload.get("enrichment_id")
        if external_id is None:
            raise InvalidWebhookError("FullEnrich payload is missing its enrichment ID")
        attempts = await self._repository.get_attempts_by_external_id(str(external_id))
        if not attempts:
            raise InvalidWebhookError("FullEnrich enrichment ID is not pending")

        attempts_by_item = {str(item["item_id"]): item for item in attempts}
        run_ids = {str(item["run_id"]) for item in attempts}
        records = payload.get("data")
        records = records if isinstance(records, list) else []
        processed_items: set[str] = set()

        for record in records:
            if not isinstance(record, dict):
                continue
            custom = record.get("custom")
            if not isinstance(custom, dict):
                continue
            item_id = str(custom.get("item_id") or "")
            attempt = attempts_by_item.get(item_id)
            if attempt is None:
                continue
            if str(custom.get("run_id") or "") != str(attempt["run_id"]) or str(
                custom.get("lead_id") or ""
            ) != str(attempt["lead_id"]):
                raise InvalidWebhookError("FullEnrich custom identifiers do not match")
            processed_items.add(item_id)
            if attempt["status"] in {"found", "not_found", "failed"}:
                continue

            phone = self._fullenrich_phone(record)
            if phone is not None:
                await self._repository.update_attempt(
                    str(attempt["id"]),
                    {
                        "status": "found",
                        "response_payload": record,
                        "phone_candidate": phone,
                        "completed_at": to_iso(utc_now()),
                    },
                )
                await self._complete_with_phone(
                    item_id, str(attempt["lead_id"]), phone, "fullenrich"
                )
            else:
                await self._repository.update_attempt(
                    str(attempt["id"]),
                    {
                        "status": "not_found",
                        "response_payload": record,
                        "completed_at": to_iso(utc_now()),
                    },
                )
                item = await self._repository.get_item(item_id)
                await self._complete_without_phone(
                    item_id, bool(item and item.get("had_provider_error"))
                )

        provider_status = str(payload.get("status") or "").upper()
        terminal_failure = provider_status in {
            "CANCELED",
            "CREDITS_INSUFFICIENT",
            "RATE_LIMIT",
            "UNKNOWN",
        }
        if terminal_failure:
            for attempt in attempts:
                if attempt["status"] not in {"pending", "waiting", "in_progress"}:
                    continue
                await self._repository.update_attempt(
                    str(attempt["id"]),
                    {
                        "status": "failed",
                        "response_payload": payload,
                        "error_code": provider_status.casefold(),
                        "error_message": "FullEnrich enrichment did not complete",
                        "completed_at": to_iso(utc_now()),
                    },
                )
                await self._repository.update_item(
                    str(attempt["item_id"]),
                    {
                        "status": "failed",
                        "had_provider_error": True,
                        "error_message": "FullEnrich enrichment did not complete",
                        "completed_at": to_iso(utc_now()),
                    },
                )
        elif provider_status == "FINISHED":
            for attempt in attempts:
                item_id = str(attempt["item_id"])
                if (
                    attempt["status"] not in {"pending", "waiting", "in_progress"}
                    or item_id in processed_items
                ):
                    continue
                await self._repository.update_attempt(
                    str(attempt["id"]),
                    {
                        "status": "not_found",
                        "response_payload": payload,
                        "completed_at": to_iso(utc_now()),
                    },
                )
                item = await self._repository.get_item(item_id)
                await self._complete_without_phone(
                    item_id, bool(item and item.get("had_provider_error"))
                )

        for run_id in run_ids:
            await self._finalize_run(run_id)

    async def reconcile(self, run_id: str) -> dict[str, Any]:
        run = await self._repository.get_run(run_id)
        if run is None:
            raise EnrichmentNotFoundError("Enrichment run not found")
        if run["status"] != "waiting" or not run.get("fullenrich_job_id"):
            raise EnrichmentValidationError("Enrichment run is not awaiting FullEnrich")
        acquired = await self._repository.acquire_reconciliation(
            run_id, self._reconcile_seconds
        )
        if not acquired:
            raise ReconciliationTooSoonError(
                "FullEnrich reconciliation is limited to once every five minutes"
            )

        job_id = str(run["fullenrich_job_id"])
        result = await self._fullenrich.get_result(job_id)
        attempts = await self._repository.get_attempts_by_external_id(job_id)
        for attempt in attempts:
            if attempt["status"] not in {"pending", "waiting", "in_progress"}:
                continue
            await self._repository.update_attempt(
                str(attempt["id"]),
                {
                    "response_payload": result.response_payload,
                    "response_headers": result.response_headers or {},
                    "http_status": result.http_status,
                    "error_code": result.error_code,
                    "error_message": result.error_message,
                },
            )
        if result.status in {"failed", "rate_limited", "timed_out"}:
            failure_payload = {
                "id": job_id,
                "status": "RATE_LIMIT"
                if result.status == "rate_limited"
                else "UNKNOWN",
                "data": [],
            }
            await self._apply_fullenrich_payload(failure_payload)
        elif isinstance(result.response_payload, dict):
            await self._apply_fullenrich_payload(result.response_payload)

        detail = await self._repository.get_run_detail(run_id)
        if detail is None:
            raise EnrichmentNotFoundError("Enrichment run not found")
        return detail

    async def get_run_detail(self, run_id: str) -> dict[str, Any]:
        detail = await self._repository.get_run_detail(run_id)
        if detail is None:
            raise EnrichmentNotFoundError("Enrichment run not found")
        return detail

    async def _persist_provider_result(
        self, attempt_id: str, result: ProviderResult, phone: str | None
    ) -> None:
        await self._repository.update_attempt(
            attempt_id,
            {
                "status": result.status,
                "request_payload": result.request_payload,
                "response_payload": result.response_payload,
                "response_headers": result.response_headers or {},
                "http_status": result.http_status,
                "external_request_id": result.external_request_id,
                "phone_candidate": phone,
                "error_code": result.error_code,
                "error_message": result.error_message,
                "completed_at": to_iso(utc_now()),
            },
        )

    async def _complete_with_phone(
        self, item_id: str, lead_id: str, phone: str, source: str
    ) -> None:
        updated = await self._repository.update_empty_lead_phone(lead_id, phone, source)
        await self._repository.update_item(
            item_id,
            {
                "status": "enriched" if updated else "skipped_existing",
                "final_phone_number": phone if updated else None,
                "final_source": source if updated else None,
                "completed_at": to_iso(utc_now()),
            },
        )

    async def _complete_without_phone(self, item_id: str, had_error: bool) -> None:
        await self._repository.update_item(
            item_id,
            {
                "status": "failed" if had_error else "not_found",
                "error_message": (
                    "One or more providers failed; no phone result is conclusive"
                    if had_error
                    else None
                ),
                "completed_at": to_iso(utc_now()),
            },
        )

    async def _finalize_run(self, run_id: str) -> None:
        items = await self._repository.list_items(run_id)
        counts = {
            "enriched": sum(item["status"] == "enriched" for item in items),
            "not_found": sum(item["status"] == "not_found" for item in items),
            "skipped": sum(
                item["status"] in {"skipped_existing", "skipped_active"}
                for item in items
            ),
            "failed": sum(item["status"] == "failed" for item in items),
        }
        active = any(
            item["status"] in {"queued", "running", "waiting"} for item in items
        )
        if active:
            status = (
                "waiting"
                if any(item["status"] == "waiting" for item in items)
                else (
                    "queued"
                    if any(item["status"] == "queued" for item in items)
                    else "running"
                )
            )
        elif counts["failed"] and (
            counts["enriched"] or counts["not_found"] or counts["skipped"]
        ):
            status = "partial"
        elif counts["failed"]:
            status = "failed"
        else:
            status = "succeeded"
        errors = [
            {
                "item_id": str(item["id"]),
                "lead_id": str(item["lead_id"]),
                "message": item.get("error_message") or "Phone enrichment failed",
            }
            for item in items
            if item["status"] == "failed"
        ]
        values: dict[str, Any] = {
            "status": status,
            "leads_enriched": counts["enriched"],
            "leads_not_found": counts["not_found"],
            "leads_skipped": counts["skipped"],
            "leads_failed": counts["failed"],
            "errors": errors,
        }
        if not active:
            values["completed_at"] = to_iso(utc_now())
        await self._repository.update_run(run_id, values)

    @staticmethod
    def _fullenrich_input(
        run_id: str, item_id: str, lead: dict[str, Any]
    ) -> dict[str, Any] | None:
        linkedin = person_linkedin_url(lead.get("linkedin_profile"))
        first_name = lead.get("first_name")
        last_name = lead.get("last_name")
        company_name = lead.get("company_name")
        domain = PhoneEnrichmentService._company_domain(lead)
        if not linkedin and not (first_name and last_name and (domain or company_name)):
            return None
        return {
            key: value
            for key, value in {
                "first_name": first_name,
                "last_name": last_name,
                "domain": domain,
                "company_name": company_name,
                "linkedin_url": linkedin,
                "enrich_fields": ["contact.phones"],
                "custom": {
                    "run_id": run_id,
                    "item_id": item_id,
                    "lead_id": str(lead["id"]),
                },
            }.items()
            if value not in (None, "")
        }

    @staticmethod
    def _company_domain(lead: dict[str, Any]) -> str | None:
        value = lead.get("website") or lead.get("company_url")
        if value:
            parsed = urlparse(
                str(value) if "://" in str(value) else "https://" + str(value)
            )
            if parsed.hostname:
                return parsed.hostname.removeprefix("www.").casefold()
        email = str(lead.get("email") or "")
        if "@" in email:
            return email.rsplit("@", 1)[1].casefold()
        return None

    @staticmethod
    def _fullenrich_phone(record: dict[str, Any]) -> str | None:
        contact_info = record.get("contact_info")
        if not isinstance(contact_info, dict):
            return None
        probable = contact_info.get("most_probable_phone")
        candidates: list[Any] = []
        if isinstance(probable, dict):
            candidates.append(probable.get("number"))
        phones = contact_info.get("phones")
        if isinstance(phones, list):
            candidates.extend(
                phone.get("number") for phone in phones if isinstance(phone, dict)
            )
        for candidate in candidates:
            normalized = normalize_phone(candidate)
            if normalized is not None:
                return normalized
        return None

    @staticmethod
    def _fingerprint(request: PhoneEnrichmentRequest) -> str:
        payload = {
            "lead_ids": sorted(str(value) for value in request.lead_ids or []),
            "source_import_run_id": (
                str(request.source_import_run_id)
                if request.source_import_run_id is not None
                else None
            ),
            "limit": request.limit,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()
