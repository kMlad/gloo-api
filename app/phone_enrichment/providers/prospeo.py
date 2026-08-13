from typing import Any

import httpx

from app.phone_enrichment.providers.base import BaseProviderClient, ProviderResult
from app.phone_enrichment.providers.linkedin import person_linkedin_url


class ProspeoClient(BaseProviderClient):
    def __init__(self, http_client: httpx.AsyncClient, api_key: str, **kwargs: Any) -> None:
        super().__init__(http_client, **kwargs)
        self._api_key = api_key

    async def find_phone(self, lead: dict[str, Any], request_id: str) -> ProviderResult:
        data = {
            key: value
            for key, value in {
                "first_name": lead.get("first_name"),
                "last_name": lead.get("last_name"),
                "linkedin_url": person_linkedin_url(lead.get("linkedin_profile")),
                "email": lead.get("email"),
                "company_name": lead.get("company_name"),
                "company_website": lead.get("website") or lead.get("company_url"),
            }.items()
            if value
        }
        if not data:
            return ProviderResult(status="skipped_no_input", request_payload={})
        payload = {"only_verified_mobile": True, "data": data}
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
            return result
        response = result.response_payload
        if result.http_status is not None and result.http_status < 400 and isinstance(
            response, dict
        ):
            person = response.get("person")
            mobile = person.get("mobile") if isinstance(person, dict) else None
            if isinstance(mobile, dict) and mobile.get("mobile"):
                result.status = "found"
                result.phone = str(mobile["mobile"])
        return result
