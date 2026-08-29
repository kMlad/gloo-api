from typing import Any

import httpx

from app.phone_enrichment.providers.base import BaseProviderClient
from app.tables.email_enrichment.inputs import normalize_email
from app.tables.email_enrichment.protocol import EmailInputs, FindEmailResult
from app.tables.email_enrichment.providers.base import to_find_result


class LeadMagicEmailClient(BaseProviderClient):
    def __init__(
        self, http_client: httpx.AsyncClient, api_key: str, **kwargs: Any
    ) -> None:
        super().__init__(http_client, **kwargs)
        self._api_key = api_key

    async def find_email(self, inputs: EmailInputs) -> FindEmailResult:
        payload = {
            "first_name": inputs.first_name,
            "last_name": inputs.last_name,
            "domain": inputs.domain,
        }
        result = await self._request_json(
            "POST",
            "/v1/people/email-finder",
            request_payload=payload,
            headers={"X-API-Key": self._api_key},
        )
        email = None
        response = result.response_payload
        if isinstance(response, dict):
            email = normalize_email(response.get("email"))
        return to_find_result(result, emails=[email] if email else [])
