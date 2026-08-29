import json
from uuid import uuid4

import pytest

from app.phone_enrichment.providers.base import ProviderResult
from app.tables.email_enrichment import EmailEnrichmentUnavailableError
from app.tables.email_enrichment.protocol import FindEmailResult, ValidationResult
from app.tables.schemas import (
    ColumnCreate,
    ColumnUpdate,
    EmailEnrichmentConfig,
    RowCreate,
    RowUpdate,
    SheriffRunCreate,
    TableCreate,
    TableFilter,
    TableFiltersUpdate,
)
from app.tables.service import TableValidationError
from tests.test_table_service import _service


class FakeFinder:
    def __init__(self, emails: list[str] | None = None, status: str = "found") -> None:
        self.calls = 0
        self.emails = emails or []
        self.status = status

    async def find_email(self, inputs) -> FindEmailResult:
        self.calls += 1
        return FindEmailResult(
            status=self.status,
            request_payload={"first_name": inputs.first_name},
            emails=list(self.emails),
        )


class FakeValidator:
    def __init__(self, result: str | dict[str, str] = "ok") -> None:
        self.calls: list[str] = []
        self.result = result

    async def verify(self, email: str) -> ValidationResult:
        self.calls.append(email)
        value = self.result[email] if isinstance(self.result, dict) else self.result
        return ValidationResult(
            status="ok" if value == "ok" else "invalid",
            request_payload={"email": email},
            result=value,
        )


class FakeFullEnrichEmail:
    def __init__(self) -> None:
        self.batches: list[list[dict]] = []

    @staticmethod
    def contact(inputs, *, run_id: str, item_id: str, row_id: str) -> dict:
        return {
            "first_name": inputs.first_name,
            "last_name": inputs.last_name,
            "domain": inputs.domain,
            "enrich_fields": ["contact.work_emails"],
            "custom": {"run_id": run_id, "item_id": item_id, "row_id": row_id},
        }

    async def submit(self, contacts: list[dict], *, run_id: str) -> ProviderResult:
        self.batches.append(contacts)
        return ProviderResult(
            status="waiting",
            request_payload={"data": contacts},
            response_payload={"enrichment_id": "job-1"},
            http_status=200,
            external_request_id="job-1",
        )

    @staticmethod
    def verify_webhook(raw_body: bytes, signature: str) -> bool:
        return signature == "valid"

    @staticmethod
    def emails_from_record(record: dict) -> list[str]:
        email = (record.get("contact_info") or {}).get("email")
        return [email] if isinstance(email, str) else []


def _input_columns(table: dict) -> dict[str, str]:
    return {column["name"]: str(column["id"]) for column in table["columns"]}


async def _table_with_inputs(service):
    table = await service.create_table(
        TableCreate(
            name="Leads",
            columns=[
                ColumnCreate(name="First name"),
                ColumnCreate(name="Last name"),
                ColumnCreate(name="LinkedIn"),
                ColumnCreate(name="Company"),
                ColumnCreate(name="Domain"),
            ],
        ),
        created_by=str(uuid4()),
    )
    ids = _input_columns(table)
    return table, ids


def _enrichment_payload(ids: dict[str, str], **overrides) -> ColumnCreate:
    values = {
        "name": "Work email",
        "type": "email_enrichment",
        "email_enrichment": EmailEnrichmentConfig(
            first_name_column_id=ids["First name"],
            last_name_column_id=ids["Last name"],
            linkedin_column_id=ids["LinkedIn"],
            company_name_column_id=ids["Company"],
            company_domain_column_id=ids["Domain"],
        ),
    }
    values.update(overrides)
    return ColumnCreate.model_validate(values)


def _column_ids(ids: dict[str, str], **overrides) -> EmailEnrichmentConfig:
    values = {
        "providers": ["icypeas"],
        "first_name_column_id": ids["First name"],
        "last_name_column_id": ids["Last name"],
        "linkedin_column_id": ids["LinkedIn"],
        "company_name_column_id": ids["Company"],
        "company_domain_column_id": ids["Domain"],
    }
    values.update(overrides)
    return EmailEnrichmentConfig.model_validate(values)


@pytest.mark.asyncio
async def test_email_enrichment_column_creates_child_and_rejects_table_create() -> None:
    service, _repository = _service(email_validator=FakeValidator())
    table, ids = await _table_with_inputs(service)
    created = await service.add_column(table["id"], _enrichment_payload(ids))
    types = [column["type"] for column in created["columns"]]
    names = [column["name"] for column in created["columns"]]
    assert "email_enrichment" in types
    assert "Email" in names
    parent = next(
        column for column in created["columns"] if column["type"] == "email_enrichment"
    )
    child = next(
        column
        for column in created["columns"]
        if str(column.get("source_column_id") or "") == str(parent["id"])
    )
    assert child["source_field"] == "email"
    assert parent["config"]["providers"] == [
        "icypeas",
        "kitt",
        "leadmagic",
        "prospeo",
        "fullenrich",
    ]
    assert parent["config"]["accept_catchall"] is False

    with pytest.raises(ValueError, match="after the table exists"):
        TableCreate.model_validate(
            {
                "name": "Nope",
                "columns": [
                    {
                        "name": "Work email",
                        "type": "email_enrichment",
                        "email_enrichment": {
                            "first_name_column_id": ids["First name"],
                            "last_name_column_id": ids["Last name"],
                            "linkedin_column_id": ids["LinkedIn"],
                            "company_name_column_id": ids["Company"],
                            "company_domain_column_id": ids["Domain"],
                        },
                    }
                ],
            }
        )


@pytest.mark.asyncio
async def test_email_enrichment_requires_text_input_columns() -> None:
    service, _repository = _service(email_validator=FakeValidator())
    table, ids = await _table_with_inputs(service)
    await service.add_column(table["id"], ColumnCreate(name="Active", type="boolean"))
    columns = await service.get_table(table["id"])
    boolean_id = next(
        column["id"] for column in columns["columns"] if column["name"] == "Active"
    )
    with pytest.raises(TableValidationError, match="must reference a text column"):
        await service.add_column(
            table["id"],
            _enrichment_payload({**ids, "First name": str(boolean_id)}),
        )


@pytest.mark.asyncio
async def test_email_enrichment_run_writes_valid_email_and_skips_blank_rows() -> None:
    finder = FakeFinder(emails=["ada@acme.com"])
    validator = FakeValidator("ok")
    service, repository = _service(
        email_finders={"icypeas": finder},
        email_validator=validator,
    )
    table, ids = await _table_with_inputs(service)
    created = await service.add_column(
        table["id"],
        _enrichment_payload(
            ids,
            email_enrichment=EmailEnrichmentConfig(
                providers=["icypeas"],
                first_name_column_id=ids["First name"],
                last_name_column_id=ids["Last name"],
                linkedin_column_id=ids["LinkedIn"],
                company_name_column_id=ids["Company"],
                company_domain_column_id=ids["Domain"],
            ),
        ),
    )
    parent = next(
        column for column in created["columns"] if column["type"] == "email_enrichment"
    )
    child = next(
        column
        for column in created["columns"]
        if str(column.get("source_column_id") or "") == str(parent["id"])
    )
    filled = await service.add_row(
        table["id"],
        RowCreate(
            values={
                ids["First name"]: "Ada",
                ids["Last name"]: "Lovelace",
                ids["LinkedIn"]: "https://www.linkedin.com/in/ada",
                ids["Company"]: "Acme",
                ids["Domain"]: "https://acme.com",
            }
        ),
    )
    blank = await service.add_row(table["id"], RowCreate())
    run = await service.start_email_enrichment_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(),
        created_by=str(uuid4()),
    )
    await service.execute_email_enrichment_run(str(run["id"]))
    finished = await service.get_email_enrichment_run(
        table["id"], str(parent["id"]), str(run["id"])
    )
    assert finished["status"] == "succeeded"
    assert finished["succeeded_count"] == 1
    assert finished["skipped_count"] == 1
    assert finished["not_found_count"] == 0
    rows = {row["id"]: row for row in (await service.list_rows(table["id"], limit=10, offset=0))["items"]}
    succeeded = rows[filled["id"]]["values"][str(parent["id"])]
    assert succeeded["status"] == "succeeded"
    assert succeeded["email"] == "ada@acme.com"
    assert succeeded["provider"] == "icypeas"
    assert succeeded["steps"] == [
        {
            "provider": "icypeas",
            "status": "found",
            "emails": [{"email": "ada@acme.com", "validation": "valid"}],
        }
    ]
    assert rows[filled["id"]]["values"][str(child["id"])] == "ada@acme.com"
    skipped = rows[blank["id"]]["values"][str(parent["id"])]
    assert skipped["status"] == "skipped"
    assert repository.email_attempts
    with pytest.raises(TableValidationError, match="computed and cannot be patched"):
        await service.update_row(
            table["id"],
            filled["id"],
            RowUpdate(values={parent["id"]: "nope"}),
        )


@pytest.mark.asyncio
async def test_email_enrichment_batches_fullenrich_and_completes_from_webhooks() -> None:
    fullenrich = FakeFullEnrichEmail()
    validator = FakeValidator("ok")
    service, _repository = _service(
        email_validator=validator,
        fullenrich_email=fullenrich,
    )
    table, ids = await _table_with_inputs(service)
    created = await service.add_column(
        table["id"],
        _enrichment_payload(
            ids,
            email_enrichment=_column_ids(ids, providers=["fullenrich"]),
        ),
    )
    parent = next(
        column for column in created["columns"] if column["type"] == "email_enrichment"
    )
    child = next(
        column
        for column in created["columns"]
        if str(column.get("source_column_id") or "") == str(parent["id"])
    )
    rows = []
    for first_name, last_name in (("Ada", "Lovelace"), ("Grace", "Hopper")):
        rows.append(
            await service.add_row(
                table["id"],
                RowCreate(
                    values={
                        ids["First name"]: first_name,
                        ids["Last name"]: last_name,
                        ids["LinkedIn"]: f"https://linkedin.com/in/{first_name.lower()}",
                        ids["Company"]: "Acme",
                        ids["Domain"]: "acme.com",
                    }
                ),
            )
        )
    run = await service.start_email_enrichment_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(),
        created_by=str(uuid4()),
    )
    await service.execute_email_enrichment_run(str(run["id"]))

    waiting = await service.get_email_enrichment_run(
        table["id"], str(parent["id"]), str(run["id"])
    )
    assert waiting["status"] == "waiting"
    assert len(fullenrich.batches) == 1
    assert len(fullenrich.batches[0]) == 2

    for contact in fullenrich.batches[0]:
        email = f"{contact['first_name'].lower()}@acme.com"
        payload = {
            "id": "job-1",
            "status": "IN_PROGRESS",
            "data": [
                {
                    "custom": contact["custom"],
                    "contact_info": {"email": email},
                }
            ],
        }
        await service.process_fullenrich_email_webhook(
            json.dumps(payload).encode(), "valid"
        )

    finished = await service.get_email_enrichment_run(
        table["id"], str(parent["id"]), str(run["id"])
    )
    assert finished["status"] == "succeeded"
    assert finished["succeeded_count"] == 2
    listed = await service.list_rows(table["id"], limit=10, offset=0)
    by_id = {row["id"]: row for row in listed["items"]}
    assert by_id[rows[0]["id"]]["values"][str(child["id"])] == "ada@acme.com"
    assert by_id[rows[1]["id"]]["values"][str(child["id"])] == "grace@acme.com"


@pytest.mark.asyncio
async def test_email_enrichment_run_requires_validator_and_skips_succeeded() -> None:
    service, _repository = _service()
    table, ids = await _table_with_inputs(service)
    created = await service.add_column(table["id"], _enrichment_payload(ids))
    parent = next(
        column for column in created["columns"] if column["type"] == "email_enrichment"
    )
    with pytest.raises(EmailEnrichmentUnavailableError):
        await service.start_email_enrichment_run(
            table["id"],
            str(parent["id"]),
            SheriffRunCreate(),
            created_by=str(uuid4()),
        )

    finder = FakeFinder(emails=["ada@acme.com"])
    validator = FakeValidator("ok")
    service, _repository = _service(
        email_finders={"icypeas": finder},
        email_validator=validator,
    )
    table, ids = await _table_with_inputs(service)
    created = await service.add_column(
        table["id"],
        _enrichment_payload(
            ids,
            email_enrichment=EmailEnrichmentConfig(
                providers=["icypeas"],
                first_name_column_id=ids["First name"],
                last_name_column_id=ids["Last name"],
                linkedin_column_id=ids["LinkedIn"],
                company_name_column_id=ids["Company"],
                company_domain_column_id=ids["Domain"],
            ),
        ),
    )
    parent = next(
        column for column in created["columns"] if column["type"] == "email_enrichment"
    )
    row = await service.add_row(
        table["id"],
        RowCreate(
            values={
                ids["First name"]: "Ada",
                ids["Last name"]: "Lovelace",
                ids["LinkedIn"]: "https://www.linkedin.com/in/ada",
                ids["Company"]: "Acme",
                ids["Domain"]: "acme.com",
            }
        ),
    )
    first = await service.start_email_enrichment_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_enrichment_run(str(first["id"]))
    second = await service.start_email_enrichment_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_enrichment_run(str(second["id"]))
    assert second["skipped_count"] == 1
    assert finder.calls == 1


@pytest.mark.asyncio
async def test_email_enrichment_rechecks_rejected_emails_on_a_new_run() -> None:
    finder = FakeFinder(emails=["ada@acme.com"])
    validator = FakeValidator("catch_all")
    service, _repository = _service(
        email_finders={"icypeas": finder},
        email_validator=validator,
    )
    table, ids = await _table_with_inputs(service)
    created = await service.add_column(
        table["id"],
        _enrichment_payload(
            ids,
            email_enrichment=EmailEnrichmentConfig(
                providers=["icypeas"],
                first_name_column_id=ids["First name"],
                last_name_column_id=ids["Last name"],
                linkedin_column_id=ids["LinkedIn"],
                company_name_column_id=ids["Company"],
                company_domain_column_id=ids["Domain"],
            ),
        ),
    )
    parent = next(
        column for column in created["columns"] if column["type"] == "email_enrichment"
    )
    row = await service.add_row(
        table["id"],
        RowCreate(
            values={
                ids["First name"]: "Ada",
                ids["Last name"]: "Lovelace",
                ids["LinkedIn"]: "https://www.linkedin.com/in/ada",
                ids["Company"]: "Acme",
                ids["Domain"]: "acme.com",
            }
        ),
    )

    first = await service.start_email_enrichment_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_enrichment_run(str(first["id"]))
    first_cell = (
        await service.list_rows(table["id"], limit=10, offset=0)
    )["items"][0]["values"][str(parent["id"])]
    assert first_cell["rejected_emails"] == ["ada@acme.com"]

    validator.result = "ok"
    second = await service.start_email_enrichment_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_enrichment_run(str(second["id"]))

    second_cell = (
        await service.list_rows(table["id"], limit=10, offset=0)
    )["items"][0]["values"][str(parent["id"])]
    assert validator.calls == ["ada@acme.com", "ada@acme.com"]
    assert second_cell["status"] == "succeeded"
    assert second_cell["rejected_emails"] == []


@pytest.mark.asyncio
async def test_email_enrichment_retries_candidate_after_transient_validation_failure() -> None:
    email = "ada@acme.com"
    finder = FakeFinder(emails=[email])

    class TransientValidator:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def verify(self, candidate: str) -> ValidationResult:
            self.calls.append(candidate)
            if len(self.calls) == 1:
                return ValidationResult(
                    status="rate_limited",
                    request_payload={"email": candidate},
                    http_status=429,
                    error_code="rate_limit",
                    error_message="MillionVerifier rate limit exceeded",
                )
            return ValidationResult(
                status="ok",
                request_payload={"email": candidate},
                result="ok",
            )

    validator = TransientValidator()
    service, _repository = _service(
        email_finders={"icypeas": finder},
        email_validator=validator,
    )
    table, ids = await _table_with_inputs(service)
    created = await service.add_column(
        table["id"],
        _enrichment_payload(ids, email_enrichment=_column_ids(ids)),
    )
    parent = next(
        column for column in created["columns"] if column["type"] == "email_enrichment"
    )
    row = await service.add_row(
        table["id"],
        RowCreate(
            values={
                ids["First name"]: "Ada",
                ids["Last name"]: "Lovelace",
                ids["LinkedIn"]: "https://www.linkedin.com/in/ada",
                ids["Company"]: "Acme",
                ids["Domain"]: "acme.com",
            }
        ),
    )

    first = await service.start_email_enrichment_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_enrichment_run(str(first["id"]))
    first_cell = (
        await service.list_rows(table["id"], limit=10, offset=0)
    )["items"][0]["values"][str(parent["id"])]
    assert first_cell["status"] == "failed"
    assert first_cell["error"] == "MillionVerifier rate limit exceeded"
    assert first_cell["rejected_emails"] == []

    second = await service.start_email_enrichment_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_enrichment_run(str(second["id"]))
    second_cell = (
        await service.list_rows(table["id"], limit=10, offset=0)
    )["items"][0]["values"][str(parent["id"])]

    assert finder.calls == 2
    assert validator.calls == [email, email]
    assert second_cell["status"] == "succeeded"
    assert second_cell["email"] == email
    assert second_cell["rejected_emails"] == []


@pytest.mark.asyncio
async def test_email_enrichment_cell_includes_waterfall_steps() -> None:
    icypeas = FakeFinder(emails=["test@gmail.com"])
    kitt = FakeFinder(emails=["test@gmail.com"])
    prospeo = FakeFinder(emails=["test1@gmail.com"])
    validator = FakeValidator(
        {"test@gmail.com": "catch_all", "test1@gmail.com": "ok"}
    )
    service, _repository = _service(
        email_finders={"icypeas": icypeas, "kitt": kitt, "prospeo": prospeo},
        email_validator=validator,
    )
    table, ids = await _table_with_inputs(service)
    created = await service.add_column(
        table["id"],
        _enrichment_payload(
            ids,
            email_enrichment=EmailEnrichmentConfig(
                providers=["icypeas", "kitt", "prospeo"],
                first_name_column_id=ids["First name"],
                last_name_column_id=ids["Last name"],
                linkedin_column_id=ids["LinkedIn"],
                company_name_column_id=ids["Company"],
                company_domain_column_id=ids["Domain"],
            ),
        ),
    )
    parent = next(
        column for column in created["columns"] if column["type"] == "email_enrichment"
    )
    row = await service.add_row(
        table["id"],
        RowCreate(
            values={
                ids["First name"]: "Ada",
                ids["Last name"]: "Lovelace",
                ids["LinkedIn"]: "https://www.linkedin.com/in/ada",
                ids["Company"]: "Acme",
                ids["Domain"]: "acme.com",
            }
        ),
    )
    run = await service.start_email_enrichment_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_enrichment_run(str(run["id"]))
    listed = await service.list_rows(table["id"], limit=10, offset=0)
    cell = listed["items"][0]["values"][str(parent["id"])]
    assert cell["status"] == "succeeded"
    assert cell["email"] == "test1@gmail.com"
    assert cell["provider"] == "prospeo"
    assert cell["steps"] == [
        {
            "provider": "icypeas",
            "status": "found",
            "emails": [{"email": "test@gmail.com", "validation": "invalid"}],
        },
        {
            "provider": "kitt",
            "status": "found",
            "emails": [{"email": "test@gmail.com", "validation": "skipped"}],
        },
        {
            "provider": "prospeo",
            "status": "found",
            "emails": [{"email": "test1@gmail.com", "validation": "valid"}],
        },
    ]


@pytest.mark.asyncio
async def test_email_enrichment_filters_only_support_is_empty() -> None:
    service, _repository = _service(email_validator=FakeValidator())
    table, ids = await _table_with_inputs(service)
    created = await service.add_column(table["id"], _enrichment_payload(ids))
    parent = next(
        column for column in created["columns"] if column["type"] == "email_enrichment"
    )
    with pytest.raises(TableValidationError, match="only support is_empty and is_not_empty"):
        await service.replace_filters(
            table["id"],
            TableFiltersUpdate(
                filters=[
                    TableFilter(column_id=parent["id"], operator="eq", value="x")
                ]
            ),
        )
    updated = await service.replace_filters(
        table["id"],
        TableFiltersUpdate(
            filters=[TableFilter(column_id=parent["id"], operator="is_not_empty")]
        ),
    )
    assert updated["filters"][0].operator == "is_not_empty"


@pytest.mark.asyncio
async def test_email_enrichment_create_stores_accept_catchall() -> None:
    service, _repository = _service(email_validator=FakeValidator())
    table, ids = await _table_with_inputs(service)
    created = await service.add_column(
        table["id"],
        _enrichment_payload(ids, email_enrichment=_column_ids(ids, accept_catchall=True)),
    )
    parent = next(
        column for column in created["columns"] if column["type"] == "email_enrichment"
    )
    assert parent["config"]["accept_catchall"] is True


@pytest.mark.asyncio
async def test_email_enrichment_patch_promotes_catchall_without_rerunning() -> None:
    finder = FakeFinder(emails=["ada@acme.com"])
    validator = FakeValidator("catch_all")
    service, _repository = _service(
        email_finders={"icypeas": finder},
        email_validator=validator,
    )
    table, ids = await _table_with_inputs(service)
    created = await service.add_column(
        table["id"],
        _enrichment_payload(ids, email_enrichment=_column_ids(ids)),
    )
    parent = next(
        column for column in created["columns"] if column["type"] == "email_enrichment"
    )
    child = next(
        column
        for column in created["columns"]
        if str(column.get("source_column_id") or "") == str(parent["id"])
    )
    missing = await service.add_row(
        table["id"],
        RowCreate(
            values={
                ids["First name"]: "Ada",
                ids["Last name"]: "Lovelace",
                ids["LinkedIn"]: "https://www.linkedin.com/in/ada",
                ids["Company"]: "Acme",
                ids["Domain"]: "acme.com",
            }
        ),
    )
    first = await service.start_email_enrichment_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[missing["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_enrichment_run(str(first["id"]))

    validator.result = "ok"
    valid = await service.add_row(
        table["id"],
        RowCreate(
            values={
                ids["First name"]: "Grace",
                ids["Last name"]: "Hopper",
                ids["LinkedIn"]: "https://www.linkedin.com/in/grace",
                ids["Company"]: "Acme",
                ids["Domain"]: "acme.com",
            }
        ),
    )
    second = await service.start_email_enrichment_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[valid["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_enrichment_run(str(second["id"]))
    assert finder.calls == 2
    assert validator.calls == ["ada@acme.com", "ada@acme.com"]

    updated = await service.update_column(
        table["id"],
        str(parent["id"]),
        ColumnUpdate(email_enrichment=_column_ids(ids, accept_catchall=True)),
    )
    assert updated["config"]["accept_catchall"] is True
    assert finder.calls == 2
    assert validator.calls == ["ada@acme.com", "ada@acme.com"]

    rows = {
        row["id"]: row
        for row in (await service.list_rows(table["id"], limit=10, offset=0))["items"]
    }
    promoted = rows[missing["id"]]["values"][str(parent["id"])]
    assert promoted["status"] == "succeeded"
    assert promoted["email"] == "ada@acme.com"
    assert promoted["provider"] == "icypeas"
    assert promoted["validation_result"] == "catch_all"
    assert promoted["rejected_emails"] == []
    assert promoted["steps"] == [
        {
            "provider": "icypeas",
            "status": "found",
            "emails": [{"email": "ada@acme.com", "validation": "valid"}],
        }
    ]
    assert rows[missing["id"]]["values"][str(child["id"])] == "ada@acme.com"

    kept = rows[valid["id"]]["values"][str(parent["id"])]
    assert kept["status"] == "succeeded"
    assert kept["validation_result"] == "ok"
    assert kept["email"] == "ada@acme.com"


@pytest.mark.asyncio
async def test_email_enrichment_patch_cannot_turn_off_accept_catchall() -> None:
    service, _repository = _service(email_validator=FakeValidator())
    table, ids = await _table_with_inputs(service)
    created = await service.add_column(
        table["id"],
        _enrichment_payload(ids, email_enrichment=_column_ids(ids, accept_catchall=True)),
    )
    parent = next(
        column for column in created["columns"] if column["type"] == "email_enrichment"
    )
    with pytest.raises(TableValidationError, match="cannot be turned off"):
        await service.update_column(
            table["id"],
            str(parent["id"]),
            ColumnUpdate(email_enrichment=_column_ids(ids, accept_catchall=False)),
        )
    omitted = await service.update_column(
        table["id"],
        str(parent["id"]),
        ColumnUpdate(email_enrichment=_column_ids(ids)),
    )
    assert omitted["config"]["accept_catchall"] is True


@pytest.mark.asyncio
async def test_email_enrichment_run_falls_back_to_catchall() -> None:
    finder = FakeFinder(emails=["ada@acme.com"])
    validator = FakeValidator("catch_all")
    service, _repository = _service(
        email_finders={"icypeas": finder},
        email_validator=validator,
    )
    table, ids = await _table_with_inputs(service)
    created = await service.add_column(
        table["id"],
        _enrichment_payload(ids, email_enrichment=_column_ids(ids, accept_catchall=True)),
    )
    parent = next(
        column for column in created["columns"] if column["type"] == "email_enrichment"
    )
    child = next(
        column
        for column in created["columns"]
        if str(column.get("source_column_id") or "") == str(parent["id"])
    )
    row = await service.add_row(
        table["id"],
        RowCreate(
            values={
                ids["First name"]: "Ada",
                ids["Last name"]: "Lovelace",
                ids["LinkedIn"]: "https://www.linkedin.com/in/ada",
                ids["Company"]: "Acme",
                ids["Domain"]: "acme.com",
            }
        ),
    )
    run = await service.start_email_enrichment_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_enrichment_run(str(run["id"]))
    listed = await service.list_rows(table["id"], limit=10, offset=0)
    cell = listed["items"][0]["values"][str(parent["id"])]
    assert cell["status"] == "succeeded"
    assert cell["email"] == "ada@acme.com"
    assert cell["validation_result"] == "catch_all"
    assert cell["rejected_emails"] == []
    assert listed["items"][0]["values"][str(child["id"])] == "ada@acme.com"

