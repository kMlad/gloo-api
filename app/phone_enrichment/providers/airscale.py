from typing import Any

import httpx

from app.phone_enrichment.providers.base import BaseProviderClient, ProviderResult
from app.phone_enrichment.providers.linkedin import person_linkedin_url


class AirScaleClient(BaseProviderClient):
    def __init__(
        self, http_client: httpx.AsyncClient, api_key: str, **kwargs: Any
    ) -> None:
        super().__init__(http_client, **kwargs)
        self._api_key = api_key

    async def find_phone(self, lead: dict[str, Any], request_id: str) -> ProviderResult:
        linkedin = person_linkedin_url(lead.get("linkedin_profile"))
        if not linkedin:
            return ProviderResult(status="skipped_no_input", request_payload={})
        payload = {"linkedin_profile_url": linkedin}
        result = await self._request_json(
            "POST",
            "/v1/phone",
            request_payload=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        response = result.response_payload
        if (
            result.http_status is not None
            and result.http_status < 400
            and isinstance(response, dict)
        ):
            phones = response.get("phone_numbers")
            if isinstance(phones, str) and phones:
                result.status = "found"
                result.phone = phones
            elif isinstance(phones, list) and phones:
                result.status = "found"
                result.phone = str(phones[0])
        return result
