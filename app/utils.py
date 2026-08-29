from collections.abc import Iterable, Iterator
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any


def chunks[T](values: list[T], size: int) -> Iterator[list[T]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def normalize_email(value: str | None) -> str:
    return (value or "").strip().casefold()


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any, *, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value)
    elif default is not None:
        return default
    else:
        raise ValueError("A timestamp is required")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def is_non_empty(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def merge_non_empty(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge incoming values while ignoring empty replacements."""
    result = deepcopy(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_non_empty(result[key], value)
        elif is_non_empty(value) or key not in result:
            result[key] = deepcopy(value)
    return result


def first_present(source: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = source.get(name)
        if is_non_empty(value):
            return value
    return None
