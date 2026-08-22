from typing import Any
from urllib.parse import urlparse

from app.phone_enrichment.providers.linkedin import person_linkedin_url


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def normalize_domain(value: Any) -> str | None:
    text = cell_text(value)
    if not text:
        return None
    parsed = urlparse(text if "://" in text else "https://" + text)
    hostname = parsed.hostname
    if hostname:
        return hostname.removeprefix("www.").casefold()
    candidate = text.removeprefix("www.").casefold()
    if "/" in candidate or " " in candidate or "." not in candidate:
        return None
    return candidate


def normalize_email(value: Any) -> str | None:
    text = cell_text(value)
    if not text or " " in text or text.count("@") != 1:
        return None
    local, domain = text.split("@", 1)
    if not local or not domain or "." not in domain:
        return None
    return f"{local}@{domain.casefold()}"


def email_cache_key(value: str) -> str:
    return value.casefold()


def person_linkedin(value: Any) -> str | None:
    return person_linkedin_url(value)
