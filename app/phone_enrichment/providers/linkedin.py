from typing import Any
from urllib.parse import urlparse


def person_linkedin_url(value: Any) -> str | None:
    """Return a LinkedIn person profile URL, excluding company and other pages."""
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlparse(text if "://" in text else "https://" + text)
    hostname = (parsed.hostname or "").casefold()
    path_parts = [part for part in parsed.path.split("/") if part]
    if not (hostname == "linkedin.com" or hostname.endswith(".linkedin.com")):
        return None
    if len(path_parts) != 2 or path_parts[0].casefold() != "in":
        return None
    return text
