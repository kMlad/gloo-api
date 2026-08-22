from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.phone_enrichment.providers.base import BaseProviderClient
from app.tables.email_enrichment.inputs import normalize_email
from app.tables.email_enrichment.protocol import EmailInputs, FindEmailResult
from app.tables.email_enrichment.providers.base import to_find_result, unique_emails

_TERMINAL_FAILURE = {"CANCELED", "CREDITS_INSUFFICIENT", "RATE_LIMIT", "UNKNOWN"}


class FullEnrichEmailClient(BaseProviderClient):
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        api_key: str,
        *,
        poll_timeout_seconds: float = 90.0,
        poll_interval_seconds: float = 2.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(http_client, **kwargs)
        self._api_key = api_key
        self._poll_timeout_seconds = poll_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds

    async def find_email(self, inputs: EmailInputs) -> FindEmailResult:
        contact = {
            key: value
            for key, value in {
                "first_name": inputs.first_name,
                "last_name": inputs.last_name,
                "domain": inputs.domain,
                "company_name": inputs.company_name,
                "linkedin_url": inputs.linkedin_url,
                "enrich_fields": ["contact.work_emails"],
            }.items()
            if value not in (None, "")
        }
        payload = {"name": "Gloo email enrichment", "data": [contact]}
        submitted = await self._request_json(
            "POST",
            "/contact/enrich/bulk",
            request_payload=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
            params={"silentFail": "true"},
        )
        if submitted.status in {"failed", "rate_limited", "timed_out"}:
            return to_find_result(submitted)
        response = submitted.response_payload
        enrichment_id = None
        if isinstance(response, dict):
            enrichment_id = response.get("enrichment_id")
        if not enrichment_id:
            submitted.status = "failed"
            submitted.error_code = "missing_enrichment_id"
            submitted.error_message = "FullEnrich did not return an enrichment ID"
            return to_find_result(submitted)
        elapsed = 0.0
        latest = submitted
        while elapsed <= self._poll_timeout_seconds:
            latest = await self._request_json(
                "GET",
                f"/contact/enrich/bulk/{enrichment_id}",
                request_payload={"enrichment_id": str(enrichment_id)},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            latest.external_request_id = str(enrichment_id)
            if latest.status in {"failed", "rate_limited", "timed_out"}:
                return to_find_result(latest)
            body = latest.response_payload
            job_status = body.get("status") if isinstance(body, dict) else None
            if job_status == "FINISHED":
                return to_find_result(latest, emails=_fullenrich_emails(body))
            if job_status in _TERMINAL_FAILURE:
                latest.status = "failed"
                latest.error_code = str(job_status).lower()
                latest.error_message = f"FullEnrich job ended with {job_status}"
                return to_find_result(latest)
            await self._sleep(self._poll_interval_seconds)
            elapsed += self._poll_interval_seconds
        latest.status = "timed_out"
        latest.error_code = "poll_timeout"
        latest.error_message = "FullEnrich enrichment timed out"
        latest.external_request_id = str(enrichment_id)
        return to_find_result(latest)


def _fullenrich_emails(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    records = payload.get("data")
    if not isinstance(records, list) or not records:
        return []
    record = records[0]
    if not isinstance(record, dict):
        return []
    contact_info = record.get("contact_info")
    if not isinstance(contact_info, dict):
        return []
    candidates: list[Any] = []
    probable = contact_info.get("most_probable_work_email")
    if isinstance(probable, dict):
        candidates.append(probable.get("email"))
    elif isinstance(probable, str):
        candidates.append(probable)
    work_emails = contact_info.get("work_emails")
    if isinstance(work_emails, list):
        for item in work_emails:
            if isinstance(item, dict):
                candidates.append(item.get("email"))
            elif isinstance(item, str):
                candidates.append(item)
    return unique_emails(
        [email for value in candidates if (email := normalize_email(value))]
    )
