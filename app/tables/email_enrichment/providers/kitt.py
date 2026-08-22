from typing import Any

import httpx

from app.phone_enrichment.providers.base import BaseProviderClient
from app.tables.email_enrichment.inputs import normalize_email
from app.tables.email_enrichment.protocol import EmailInputs, FindEmailResult
from app.tables.email_enrichment.providers.base import to_find_result, unique_emails


class KittEmailClient(BaseProviderClient):
    def __init__(self, http_client: httpx.AsyncClient, api_key: str, **kwargs: Any) -> None:
        super().__init__(http_client, **kwargs)
        self._api_key = api_key

    async def find_email(self, inputs: EmailInputs) -> FindEmailResult:
        payload = {
            key: value
            for key, value in {
                "fullName": inputs.full_name,
                "domain": inputs.domain,
                "linkedinStandardProfileURL": inputs.linkedin_url,
                "realtime": True,
            }.items()
            if value not in (None, "")
        }
        result = await self._request_json(
            "POST",
            "/job/find_email",
            request_payload=payload,
            headers={"x-api-key": self._api_key},
        )
        return to_find_result(result, emails=_kitt_emails(result.response_payload))


def _kitt_emails(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    candidates: list[Any] = [
        payload.get("email"),
        payload.get("workEmail"),
        payload.get("work_email"),
    ]
    for key in ("data", "result", "job"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.extend(
                [
                    nested.get("email"),
                    nested.get("workEmail"),
                    nested.get("work_email"),
                ]
            )
        elif isinstance(nested, str):
            candidates.append(nested)
    emails = payload.get("emails")
    if isinstance(emails, list):
        for item in emails:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                candidates.append(item.get("email"))
    return unique_emails(
        [email for value in candidates if (email := normalize_email(value))]
    )
