import re

PLACEHOLDER_RE = re.compile(r"\{\{([^}]+)\}\}")
OUTPUT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
NOT_FOUND_VALUES = {"", "not found", "n/a", "none", "null"}

EXPAND_INSTRUCTIONS = """\
You turn a short research goal into a reusable row-level research brief.

The goal may include {{Column Name}} placeholders. Keep those placeholders
verbatim — do not replace them with example values.

Return:
- enhanced_prompt: a research brief covering the task, what to look up, what
  not to invent, a "Not found" floor when evidence is missing, a habit of
  citing sources, and Google-style discovery queries such as
  "Name Company LinkedIn" when a profile URL is needed.
- outputs: 1-10 primitive fields to write back. Keys are lowercase snake_case.
  Types are only "text" or "boolean". No nested objects or arrays.

Do not invent columns the goal does not imply.
"""

RESEARCH_INSTRUCTIONS = """\
You are a careful web researcher. Use web search. Do not invent facts.
If you cannot verify a field, return null for that field (or an empty value)
rather than guessing. Prefer primary sources. Include source URLs.
Set confidence to low when evidence is thin, second-hand, or only a SERP
headline match. Never ask follow-up questions — answer from search.
"""


def validate_output_key(value: str) -> str:
    key = value.strip()
    if not OUTPUT_KEY_RE.fullmatch(key):
        raise ValueError("output keys must be lowercase snake_case")
    return key


def placeholder_names(prompt: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for match in PLACEHOLDER_RE.finditer(prompt):
        name = match.group(1).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def interpolate_prompt(
    prompt: str,
    values_by_name: dict[str, str],
    *,
    invalid_names: set[str] | None = None,
) -> str:
    from app.tables.claygent.protocol import (
        InvalidPlaceholderError,
        UnknownPlaceholderError,
    )

    blocked = invalid_names or set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if name in blocked:
            raise InvalidPlaceholderError(
                name, f"Column {name} cannot be used as a claygent input"
            )
        if name not in values_by_name:
            raise UnknownPlaceholderError(name)
        return values_by_name[name]

    return PLACEHOLDER_RE.sub(replace, prompt)


def display_name_for_key(key: str) -> str:
    return key.replace("_", " ").capitalize()


def unique_child_name(parent_name: str, key: str, taken: set[str]) -> str:
    base = display_name_for_key(key)
    if base not in taken:
        return base
    prefixed = f"{parent_name} {base[0].lower()}{base[1:]}" if base else parent_name
    if prefixed not in taken:
        return prefixed
    index = 2
    while True:
        candidate = f"{prefixed} {index}"
        if candidate not in taken:
            return candidate
        index += 1


def stringify_cell(value: object) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def is_not_found(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().casefold() in NOT_FOUND_VALUES:
        return True
    return False


def output_json_schema(outputs: list[object]) -> dict[str, object]:
    properties: dict[str, object] = {}
    required: list[str] = []
    for field in outputs:
        key = getattr(field, "key")
        field_type = getattr(field, "type")
        if field_type == "boolean":
            properties[key] = {"type": ["boolean", "null"]}
        else:
            properties[key] = {"type": ["string", "null"]}
        required.append(key)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def envelope_json_schema(outputs: list[object]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "output": output_json_schema(outputs),
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
            "confidence_reason": {"type": "string"},
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "title": {"type": "string"},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["output", "confidence", "confidence_reason", "sources"],
        "additionalProperties": False,
    }


def expand_json_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "enhanced_prompt": {"type": "string"},
            "outputs": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "type": {"type": "string", "enum": ["text", "boolean"]},
                    },
                    "required": ["key", "type"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["enhanced_prompt", "outputs"],
        "additionalProperties": False,
    }
