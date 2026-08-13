from typing import Any

import httpx

from app.phone_enrichment.providers.base import BaseProviderClient, ProviderResult


class FullEnrichClient(BaseProviderClient):
    def __init__(self, http_client: httpx.AsyncClient, api_key: str, **kwargs: Any) -> None:
        super().__init__(http_client, **kwargs)
        self._api_key = api_key

    async def submit(self, contacts: list[dict[str, Any]], webhook_url: str) -> ProviderResult:
        payload = {
            "name": "Gloo phone enrichment",
            "data": contacts,
            "webhook_url": webhook_url,
            "webhook_events": {"contact_finished": webhook_url},
        }
        result = await self._request_json(
            "POST",
            "/contact/enrich/bulk",
            request_payload=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
            params={"silentFail": "true"},
        )
        # Keep callback secrets out of audit records while preserving the request data.
        result.request_payload = {
            **payload,
            "webhook_url": "[configured]",
            "webhook_events": {"contact_finished": "[configured]"},
        }
        response = result.response_payload
        if result.http_status is not None and result.http_status < 400 and isinstance(
            response, dict
        ):
            job_id = response.get("enrichment_id")
            if job_id:
                result.status = "waiting"
                result.external_request_id = str(job_id)
            else:
                result.status = "failed"
                result.error_code = "missing_enrichment_id"
                result.error_message = "FullEnrich did not return an enrichment ID"
        return result

    async def get_result(self, enrichment_id: str) -> ProviderResult:
        result = await self._request_json(
            "GET",
            f"/contact/enrich/bulk/{enrichment_id}",
            request_payload={"enrichment_id": enrichment_id},
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        if result.http_status is not None and result.http_status < 400:
            result.status = "waiting"
            result.external_request_id = enrichment_id
        return result
