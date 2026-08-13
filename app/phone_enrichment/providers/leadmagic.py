from typing import Any

import httpx

from app.phone_enrichment.providers.base import BaseProviderClient, ProviderResult
from app.phone_enrichment.providers.linkedin import person_linkedin_url


class LeadMagicClient(BaseProviderClient):
    def __init__(self, http_client: httpx.AsyncClient, api_key: str, **kwargs: Any) -> None:
        super().__init__(http_client, **kwargs)
        self._api_key = api_key

    async def find_phone(self, lead: dict[str, Any], request_id: str) -> ProviderResult:
        payload = {
            key: value
            for key, value in {
                "profile_url": person_linkedin_url(lead.get("linkedin_profile")),
                "work_email": lead.get("email"),
            }.items()
            if value
        }
        if not payload:
            return ProviderResult(status="skipped_no_input", request_payload={})
        result = await self._request_json(
            "POST",
            "/v1/people/mobile-finder",
            request_payload=payload,
            headers={"X-API-Key": self._api_key, "X-Request-ID": request_id},
        )
        if result.http_status is not None and result.http_status < 400:
            response = result.response_payload
            if isinstance(response, dict) and response.get("mobile_number"):
                result.status = "found"
                result.phone = str(response["mobile_number"])
        return result
