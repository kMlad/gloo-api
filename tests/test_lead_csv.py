from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.auth import AuthenticatedUser
from app.lead_csv import (
    LeadCsvMapping,
    LeadCsvMappingError,
    import_leads_csv,
    preview_leads_csv,
    suggest_mapping,
    validate_mapping_headers,
)
from app.tables.csv_import import CsvImportError


def _actor(role: str, user_id: str = "sdr-1") -> AuthenticatedUser:
    return AuthenticatedUser(
        id=user_id,
        email=f"{role}@example.com",
        role=role,  # type: ignore[arg-type]
        app_metadata={"role": role},
    )


class LeadCsvRepositoryStub:
    def __init__(
        self,
        *,
        emails: set[str] | None = None,
        phones: set[str] | None = None,
    ) -> None:
        self.emails = set(emails or [])
        self.phones = set(phones or [])
        self.inserted: list[dict] = []

    async def existing_lead_emails(self, emails: list[str]) -> set[str]:
        return {email for email in emails if email in self.emails}

    async def existing_lead_phones(self, phones: list[str]) -> set[str]:
        return {phone for phone in phones if phone in self.phones}

    async def insert_leads(self, rows: list[dict]) -> list[dict]:
        stored = []
        for row in rows:
            item = {**row, "id": str(uuid4())}
            stored.append(item)
        self.inserted.extend(stored)
        return stored


def test_suggest_mapping_matches_header_aliases() -> None:
    suggested = suggest_mapping(
        ["Email Address", "Mobile", "First Name", "Company", "Title"]
    )
    assert suggested["email"] == "Email Address"
    assert suggested["phone"] == "Mobile"
    assert suggested["first_name"] == "First Name"
    assert suggested["company_name"] == "Company"
    assert suggested["last_name"] is None
    assert "company_url" not in suggested


def test_suggest_mapping_treats_company_url_as_website() -> None:
    suggested = suggest_mapping(["Email", "Company Website"])
    assert suggested["website"] == "Company Website"
    assert "company_url" not in suggested


def test_lead_csv_mapping_requires_email_or_phone() -> None:
    with pytest.raises(ValidationError, match="email or phone"):
        LeadCsvMapping(first_name="First")


def test_lead_csv_mapping_rejects_duplicate_headers() -> None:
    with pytest.raises(ValidationError, match="more than once"):
        LeadCsvMapping(email="Email", phone="Email")
    with pytest.raises(ValidationError, match="more than once"):
        LeadCsvMapping(email="Email", custom_properties=["Email"])


def test_validate_mapping_headers_rejects_unknown_columns() -> None:
    mapping = LeadCsvMapping(email="Email")
    with pytest.raises(LeadCsvMappingError, match="Unknown CSV column"):
        validate_mapping_headers(mapping, ["Phone"])


def test_preview_leads_csv_returns_headers_sample_and_suggestions() -> None:
    preview = preview_leads_csv(
        b"Email,Phone,Name\npat@example.com,555,Pat\nlee@example.com,444,Lee\n"
        b"sam@example.com,333,Sam\nava@example.com,222,Ava\n"
    )
    assert preview["headers"] == ["Email", "Phone", "Name"]
    assert preview["row_count"] == 4
    assert len(preview["preview_rows"]) == 3
    assert preview["suggested_mapping"]["email"] == "Email"
    assert preview["suggested_mapping"]["phone"] == "Phone"


def test_preview_leads_csv_rejects_invalid_files() -> None:
    with pytest.raises(CsvImportError, match="empty"):
        preview_leads_csv(b"")


@pytest.mark.asyncio
async def test_import_skips_invalid_and_duplicate_emails() -> None:
    repository = LeadCsvRepositoryStub(emails={"pat@example.com"})
    result = await import_leads_csv(
        content=(
            b"Email,Company\n"
            b"pat@example.com,Acme\n"
            b"not-an-email,Globex\n"
            b"lee@example.com,Initech\n"
            b"lee@example.com,Duplicate\n"
            b",NoEmail\n"
        ),
        mapping=LeadCsvMapping(email="Email", company_name="Company"),
        actor=_actor("admin"),
        repository=repository,  # type: ignore[arg-type]
    )

    assert result["created_count"] == 1
    assert result["skipped_duplicate_count"] == 2
    assert result["skipped_invalid_count"] == 2
    assert repository.inserted[0]["email_normalized"] == "lee@example.com"
    assert repository.inserted[0]["company_name"] == "Initech"
    assert "assigned_sdr_id" not in repository.inserted[0]


@pytest.mark.asyncio
async def test_import_phone_only_rows_dedupe_by_digits() -> None:
    repository = LeadCsvRepositoryStub(phones={"14155552671"})
    result = await import_leads_csv(
        content=(
            b"Phone,First\n"
            b"+1 (415) 555-2671,Existing\n"
            b"415-555-0000,New\n"
            b"4155550000,Duplicate\n"
            b",Missing\n"
        ),
        mapping=LeadCsvMapping(phone="Phone", first_name="First"),
        actor=_actor("sales_lead"),
        repository=repository,  # type: ignore[arg-type]
    )

    assert result["created_count"] == 1
    assert result["skipped_duplicate_count"] == 2
    assert result["skipped_invalid_count"] == 1
    assert repository.inserted[0]["email"] is None
    assert repository.inserted[0]["smartlead_phone_number"] == "415-555-0000"
    assert "assigned_sdr_id" not in repository.inserted[0]


@pytest.mark.asyncio
async def test_import_new_email_is_not_skipped_for_matching_phone() -> None:
    repository = LeadCsvRepositoryStub(phones={"14155552671"})
    result = await import_leads_csv(
        content=b"Email,Phone\npat@example.com,+1 415 555 2671\n",
        mapping=LeadCsvMapping(email="Email", phone="Phone"),
        actor=_actor("admin"),
        repository=repository,  # type: ignore[arg-type]
    )

    assert result["created_count"] == 1
    assert result["skipped_duplicate_count"] == 0


@pytest.mark.asyncio
async def test_sdr_import_assigns_created_leads_to_self() -> None:
    repository = LeadCsvRepositoryStub()
    result = await import_leads_csv(
        content=b"Email\npat@example.com\n",
        mapping=LeadCsvMapping(email="Email"),
        actor=_actor("sdr", user_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        repository=repository,  # type: ignore[arg-type]
    )

    assert result["created_count"] == 1
    inserted = repository.inserted[0]
    assert inserted["assigned_sdr_id"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert inserted["assigned_by"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert inserted["assigned_at"]


@pytest.mark.asyncio
async def test_import_stores_custom_properties_and_original_row() -> None:
    repository = LeadCsvRepositoryStub()
    await import_leads_csv(
        content=b"Email,Title,Skip\npat@example.com,SDR,ignored\n",
        mapping=LeadCsvMapping(email="Email", custom_properties=["Title"]),
        actor=_actor("admin"),
        repository=repository,  # type: ignore[arg-type]
    )

    inserted = repository.inserted[0]
    assert inserted["custom_properties"] == {"Title": "SDR"}
    assert inserted["properties"]["Email"] == "pat@example.com"
    assert inserted["properties"]["Title"] == "SDR"
    assert inserted["properties"]["Skip"] == "ignored"


@pytest.mark.asyncio
async def test_import_maps_company_url_column_to_website() -> None:
    repository = LeadCsvRepositoryStub()
    await import_leads_csv(
        content=b"Email,Company URL\npat@example.com,https://acme.example\n",
        mapping=LeadCsvMapping(email="Email", website="Company URL"),
        actor=_actor("admin"),
        repository=repository,  # type: ignore[arg-type]
    )

    inserted = repository.inserted[0]
    assert inserted["website"] == "https://acme.example"
    assert "company_url" not in inserted
