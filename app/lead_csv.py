from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.auth import AuthenticatedUser
from app.repositories import Repository
from app.tables.csv_import import parse_csv
from app.utils import normalize_email, to_iso, utc_now

PREVIEW_ROW_LIMIT = 3

MAPPABLE_FIELDS = (
    "email",
    "phone",
    "first_name",
    "last_name",
    "company_name",
    "location",
    "website",
    "linkedin_profile",
)

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "email": ("email", "e_mail", "email_address", "emailaddress"),
    "phone": (
        "phone",
        "phone_number",
        "phonenumber",
        "mobile",
        "mobile_number",
        "smartlead_phone_number",
    ),
    "first_name": ("first_name", "firstname", "first"),
    "last_name": ("last_name", "lastname", "last"),
    "company_name": ("company_name", "company", "companyname"),
    "location": ("location", "city", "city_name"),
    "website": (
        "website",
        "web",
        "url",
        "company_url",
        "companyurl",
        "company_website",
    ),
    "linkedin_profile": (
        "linkedin",
        "linkedin_profile",
        "linkedin_url",
        "linkedinurl",
    ),
}

TYPED_COLUMNS = {
    "first_name": "first_name",
    "last_name": "last_name",
    "phone": "smartlead_phone_number",
    "company_name": "company_name",
    "location": "location",
    "website": "website",
    "linkedin_profile": "linkedin_profile",
}


class LeadCsvMappingError(Exception):
    pass


class LeadCsvMapping(BaseModel):
    email: str | None = None
    phone: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    company_name: str | None = None
    location: str | None = None
    website: str | None = None
    linkedin_profile: str | None = None
    custom_properties: list[str] = Field(default_factory=list)

    @field_validator(*MAPPABLE_FIELDS, mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("custom_properties", mode="before")
    @classmethod
    def clean_custom_properties(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            header = item.strip()
            if not header or header in seen:
                continue
            seen.add(header)
            cleaned.append(header)
        return cleaned

    @model_validator(mode="after")
    def validate_mapping(self) -> "LeadCsvMapping":
        if self.email is None and self.phone is None:
            raise ValueError("Map an email or phone column")
        used: dict[str, str] = {}
        for field in MAPPABLE_FIELDS:
            header = getattr(self, field)
            if header is None:
                continue
            if header in used:
                raise ValueError(
                    f"CSV column '{header}' is mapped more than once"
                )
            used[header] = field
        for header in self.custom_properties:
            if header in used:
                raise ValueError(
                    f"CSV column '{header}' is mapped more than once"
                )
        return self

    def mapped_headers(self) -> list[str]:
        headers: list[str] = []
        for field in MAPPABLE_FIELDS:
            header = getattr(self, field)
            if header is not None:
                headers.append(header)
        headers.extend(self.custom_properties)
        return headers


def normalize_header(value: str) -> str:
    collapsed = []
    previous_underscore = False
    for character in value.strip().casefold():
        if character.isalnum():
            collapsed.append(character)
            previous_underscore = False
        elif not previous_underscore:
            collapsed.append("_")
            previous_underscore = True
    return "".join(collapsed).strip("_")


def normalize_phone_digits(value: str | None) -> str:
    return "".join(
        character
        for character in (value or "")
        if character.isascii() and character.isdigit()
    )


def suggest_mapping(headers: list[str]) -> dict[str, str | None]:
    unused = list(headers)
    suggested: dict[str, str | None] = {field: None for field in MAPPABLE_FIELDS}
    for field, aliases in FIELD_ALIASES.items():
        alias_set = set(aliases)
        match = next(
            (
                header
                for header in unused
                if normalize_header(header) in alias_set
            ),
            None,
        )
        if match is None:
            continue
        suggested[field] = match
        unused.remove(match)
    return suggested


def validate_mapping_headers(mapping: LeadCsvMapping, headers: list[str]) -> None:
    known = set(headers)
    unknown = [header for header in mapping.mapped_headers() if header not in known]
    if unknown:
        raise LeadCsvMappingError(
            f"Unknown CSV column: {unknown[0]}"
        )


def preview_leads_csv(content: bytes) -> dict[str, Any]:
    parsed = parse_csv(content)
    return {
        "headers": parsed.headers,
        "preview_rows": parsed.rows[:PREVIEW_ROW_LIMIT],
        "row_count": len(parsed.rows),
        "suggested_mapping": suggest_mapping(parsed.headers),
    }


@dataclass(frozen=True)
class _MappedRow:
    email: str | None
    email_normalized: str | None
    phone: str | None
    phone_normalized: str | None
    typed_properties: dict[str, str]
    properties: dict[str, str]
    custom_properties: dict[str, str]


def _cell(cells: list[str], headers: list[str], header: str | None) -> str:
    if header is None:
        return ""
    index = headers.index(header)
    return cells[index] if index < len(cells) else ""


def _normalized_email(value: str) -> str | None:
    email = normalize_email(value)
    if not email or "@" not in email:
        return None
    return email


def _map_row(
    cells: list[str], headers: list[str], mapping: LeadCsvMapping
) -> _MappedRow:
    properties = {
        header: cells[index] if index < len(cells) else ""
        for index, header in enumerate(headers)
        if (cells[index] if index < len(cells) else "") != ""
    }
    raw_email = _cell(cells, headers, mapping.email)
    raw_phone = _cell(cells, headers, mapping.phone)
    email_normalized = _normalized_email(raw_email)
    phone_normalized = normalize_phone_digits(raw_phone) or None
    typed_properties: dict[str, str] = {}
    for field, column in TYPED_COLUMNS.items():
        value = _cell(cells, headers, getattr(mapping, field))
        if field == "phone" and phone_normalized is None:
            continue
        if value:
            typed_properties[column] = value
    custom_properties = {
        header: value
        for header in mapping.custom_properties
        if (value := _cell(cells, headers, header))
    }
    return _MappedRow(
        email=raw_email.strip() or None if email_normalized else None,
        email_normalized=email_normalized,
        phone=raw_phone.strip() or None if phone_normalized else None,
        phone_normalized=phone_normalized,
        typed_properties=typed_properties,
        properties=properties,
        custom_properties=custom_properties,
    )


def _assignment_fields(actor: AuthenticatedUser, observed_at: str) -> dict[str, str]:
    if actor.role != "sdr":
        return {}
    return {
        "assigned_sdr_id": actor.id,
        "assigned_by": actor.id,
        "assigned_at": observed_at,
    }


async def import_leads_csv(
    *,
    content: bytes,
    mapping: LeadCsvMapping,
    actor: AuthenticatedUser,
    repository: Repository,
) -> dict[str, Any]:
    parsed = parse_csv(content)
    validate_mapping_headers(mapping, parsed.headers)

    mapped_rows = [
        _map_row(cells, parsed.headers, mapping) for cells in parsed.rows
    ]
    skipped_invalid_count = 0
    valid_rows: list[_MappedRow] = []
    for row in mapped_rows:
        if row.email_normalized is None and row.phone_normalized is None:
            skipped_invalid_count += 1
            continue
        valid_rows.append(row)

    existing_emails = await repository.existing_lead_emails(
        [row.email_normalized for row in valid_rows if row.email_normalized]
    )
    existing_phones = await repository.existing_lead_phones(
        [
            row.phone_normalized
            for row in valid_rows
            if row.email_normalized is None and row.phone_normalized
        ]
    )

    seen_emails = set(existing_emails)
    seen_phones = set(existing_phones)
    skipped_duplicate_count = 0
    to_insert: list[dict[str, Any]] = []
    observed_at = to_iso(utc_now())
    assignment = _assignment_fields(actor, observed_at)

    for row in valid_rows:
        if row.email_normalized is not None:
            if row.email_normalized in seen_emails:
                skipped_duplicate_count += 1
                continue
            seen_emails.add(row.email_normalized)
        elif row.phone_normalized in seen_phones:
            skipped_duplicate_count += 1
            continue
        else:
            seen_phones.add(row.phone_normalized)

        payload: dict[str, Any] = {
            "email": row.email,
            "email_normalized": row.email_normalized,
            **row.typed_properties,
            "properties": row.properties,
            "custom_properties": row.custom_properties,
            "source_observed_at": observed_at,
            "updated_at": observed_at,
            **assignment,
        }
        to_insert.append(payload)

    inserted = await repository.insert_leads(to_insert)
    created_ids = [UUID(str(item["id"])) for item in inserted]
    return {
        "created_count": len(created_ids),
        "skipped_duplicate_count": skipped_duplicate_count,
        "skipped_invalid_count": skipped_invalid_count,
        "created_lead_ids": created_ids,
    }
