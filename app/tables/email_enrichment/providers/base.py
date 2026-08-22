from typing import Any

from app.phone_enrichment.providers.base import ProviderResult
from app.tables.email_enrichment.protocol import FindEmailResult


def to_find_result(
    result: ProviderResult, *, emails: list[str] | None = None
) -> FindEmailResult:
    found = [email for email in emails or [] if email]
    status = result.status
    if found and status in {"not_found", "waiting"}:
        status = "found"
    return FindEmailResult(
        status=status,
        request_payload=result.request_payload,
        emails=found,
        response_payload=result.response_payload,
        response_headers=result.response_headers,
        http_status=result.http_status,
        error_code=result.error_code,
        error_message=result.error_message,
        external_request_id=result.external_request_id,
    )


def unique_emails(*groups: Any) -> list[str]:
    seen: set[str] = set[str]()
    ordered: list[str] = []
    for group in groups:
        values = group if isinstance(group, list) else [group]
        for value in values:
            if not isinstance(value, str):
                continue
            email = value.strip()
            key = email.casefold()
            if not email or key in seen:
                continue
            seen.add(key)
            ordered.append(email)
    return ordered
