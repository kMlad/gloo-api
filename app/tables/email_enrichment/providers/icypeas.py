from typing import Any

import httpx

from app.phone_enrichment.providers.base import BaseProviderClient
from app.tables.email_enrichment.inputs import normalize_email
from app.tables.email_enrichment.protocol import EmailInputs, FindEmailResult
from app.tables.email_enrichment.providers.base import to_find_result, unique_emails

_CERTAINTY_RANK = {
    "ultra_sure": 5,
    "very_sure": 4,
    "sure": 3,
    "likely": 2,
    "low": 1,
}


class IcypeasEmailClient(BaseProviderClient):
    def __init__(
        self, http_client: httpx.AsyncClient, api_key: str, **kwargs: Any
    ) -> None:
        super().__init__(http_client, **kwargs)
        self._api_key = api_key

    async def find_email(self, inputs: EmailInputs) -> FindEmailResult:
        payload = {
            "firstname": inputs.first_name,
            "lastname": inputs.last_name,
            "domainOrCompany": inputs.domain or inputs.company_name,
        }
        result = await self._request_json(
            "POST",
            "/api/sync/email-search",
            request_payload=payload,
            headers={"Authorization": self._api_key},
        )
        emails = _icypeas_emails(result.response_payload)
        return to_find_result(result, emails=emails)


def _icypeas_emails(payload: Any) -> list[str]:
    records: list[tuple[int, str]] = []
    for item in _email_records(payload):
        email = normalize_email(item.get("email"))
        if email is None:
            continue
        certainty = str(item.get("certainty") or "").casefold()
        records.append((_CERTAINTY_RANK.get(certainty, 0), email))
    records.sort(key=lambda item: item[0], reverse=True)
    return unique_emails([email for _rank, email in records])


def _email_records(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    emails = payload.get("emails")
    if isinstance(emails, list):
        return [item for item in emails if isinstance(item, dict)]
    item = payload.get("item")
    if isinstance(item, dict):
        results = item.get("results")
        if isinstance(results, dict):
            nested = results.get("emails")
            if isinstance(nested, list):
                return [entry for entry in nested if isinstance(entry, dict)]
    return []
