import hashlib
import hmac
from typing import Any

import httpx

from app.phone_enrichment.providers.base import BaseProviderClient, ProviderResult
from app.tables.email_enrichment.inputs import normalize_email
from app.tables.email_enrichment.protocol import EmailInputs
from app.tables.email_enrichment.providers.base import unique_emails


class FullEnrichEmailClient(BaseProviderClient):
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        api_key: str,
        *,
        webhook_url: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(http_client, **kwargs)
        self._api_key = api_key
        self._webhook_url = webhook_url

    @staticmethod
    def contact(
        inputs: EmailInputs, *, run_id: str, item_id: str, row_id: str
    ) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "first_name": inputs.first_name,
                "last_name": inputs.last_name,
                "domain": inputs.domain,
                "company_name": inputs.company_name,
                "linkedin_url": inputs.linkedin_url,
                "enrich_fields": ["contact.work_emails"],
                "custom": {
                    "run_id": run_id,
                    "item_id": item_id,
                    "row_id": row_id,
                },
            }.items()
            if value not in (None, "")
        }

    async def submit(
        self, contacts: list[dict[str, Any]], *, run_id: str
    ) -> ProviderResult:
        payload = {
            "name": f"Gloo email enrichment {run_id}",
            "webhook_url": self._webhook_url,
            "webhook_events": {"contact_finished": self._webhook_url},
            "data": contacts,
        }
        result = await self._request_json(
            "POST",
            "/contact/enrich/bulk",
            request_payload=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
            params={"silentFail": "true"},
        )
        result.request_payload = {
            **payload,
            "webhook_url": "[configured]",
            "webhook_events": {"contact_finished": "[configured]"},
        }
        response = result.response_payload
        enrichment_id = None
        if isinstance(response, dict):
            enrichment_id = response.get("enrichment_id")
        if (
            result.http_status is not None
            and result.http_status < 400
            and enrichment_id
        ):
            result.status = "waiting"
            result.external_request_id = str(enrichment_id)
        elif result.http_status is not None and result.http_status < 400:
            result.status = "failed"
            result.error_code = "missing_enrichment_id"
            result.error_message = "FullEnrich did not return an enrichment ID"
        return result

    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        expected = hmac.new(
            self._api_key.encode("utf-8"), raw_body, hashlib.sha1
        ).hexdigest()
        return bool(signature) and hmac.compare_digest(expected, signature)

    @staticmethod
    def emails_from_record(record: Any) -> list[str]:
        return _fullenrich_emails({"data": [record]})


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
