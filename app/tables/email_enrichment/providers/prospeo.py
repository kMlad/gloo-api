from typing import Any

import httpx

from app.phone_enrichment.providers.base import BaseProviderClient
from app.tables.email_enrichment.inputs import normalize_email
from app.tables.email_enrichment.protocol import EmailInputs, FindEmailResult
from app.tables.email_enrichment.providers.base import to_find_result


class ProspeoEmailClient(BaseProviderClient):
    def __init__(
        self, http_client: httpx.AsyncClient, api_key: str, **kwargs: Any
    ) -> None:
        super().__init__(http_client, **kwargs)
        self._api_key = api_key

    async def find_email(self, inputs: EmailInputs) -> FindEmailResult:
        data = {
            key: value
            for key, value in {
                "first_name": inputs.first_name,
                "last_name": inputs.last_name,
                "linkedin_url": inputs.linkedin_url,
                "company_name": inputs.company_name,
                "company_website": inputs.domain,
            }.items()
            if value
        }
        payload = {"enrich_mobile": False, "data": data}
        result = await self._request_json(
            "POST",
            "/enrich-person",
            request_payload=payload,
            headers={"X-KEY": self._api_key},
        )
        if result.error_code == "NO_MATCH":
            result.status = "not_found"
            result.error_code = None
            result.error_message = None
            return to_find_result(result)
        email = None
        response = result.response_payload
        if isinstance(response, dict):
            person = response.get("person")
            if isinstance(person, dict):
                email = normalize_email(person.get("email"))
        return to_find_result(result, emails=[email] if email else [])
