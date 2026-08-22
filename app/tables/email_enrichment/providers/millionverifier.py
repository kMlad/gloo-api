from typing import Any

import httpx

from app.phone_enrichment.providers.base import BaseProviderClient
from app.tables.email_enrichment.protocol import ValidationResult


class MillionVerifierClient(BaseProviderClient):
    def __init__(self, http_client: httpx.AsyncClient, api_key: str, **kwargs: Any) -> None:
        super().__init__(http_client, **kwargs)
        self._api_key = api_key

    async def verify(self, email: str) -> ValidationResult:
        result = await self._request_json(
            "GET",
            "/api/v3/",
            request_payload={"email": email},
            params={"api": self._api_key, "email": email},
        )
        response = result.response_payload
        verification = None
        if isinstance(response, dict):
            verification = str(response.get("result") or "") or None
        status = result.status
        error_code = result.error_code
        error_message = result.error_message
        if (
            result.http_status is not None
            and 200 <= result.http_status < 300
            and error_code is None
        ):
            status = "ok" if verification == "ok" else "invalid"
        elif (
            result.http_status is not None
            and 300 <= result.http_status < 400
        ):
            status = "failed"
            error_code = "unexpected_redirect"
            error_message = "Provider returned an unexpected redirect"
        return ValidationResult(
            status=status,
            request_payload={"email": email},
            result=verification,
            response_payload=response,
            response_headers=result.response_headers,
            http_status=result.http_status,
            error_code=error_code,
            error_message=error_message,
        )
