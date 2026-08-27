from uuid import uuid4

import pytest

from app.tables.email_enrichment import EmailEnrichmentUnavailableError
from app.tables.email_enrichment.protocol import ValidationResult
from app.tables.schemas import (
    ColumnCreate,
    ColumnUpdate,
    EmailValidationConfig,
    RowCreate,
    RowUpdate,
    SheriffRunCreate,
    TableCreate,
    TableFilter,
    TableFiltersUpdate,
)
from app.tables.service import TableValidationError
from tests.test_table_service import _service


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


class FailingValidator:
    async def verify(self, email: str) -> ValidationResult:
        return ValidationResult(
            status="failed",
            request_payload={"email": email},
            error_message="MillionVerifier request failed",
        )


async def _table_with_email(service, *, extra_columns: list[ColumnCreate] | None = None):
    columns = [ColumnCreate(name="Email")]
    if extra_columns:
        columns.extend(extra_columns)
    table = await service.create_table(
        TableCreate(name="Leads", columns=columns),
        created_by=str(uuid4()),
    )
    ids = {column["name"]: str(column["id"]) for column in table["columns"]}
    return table, ids


def _validation_payload(email_column_id: str, **overrides) -> ColumnCreate:
    values = {
        "name": "Email valid",
        "type": "email_validation",
        "email_validation": EmailValidationConfig(email_column_id=email_column_id),
    }
    values.update(overrides)
    return ColumnCreate.model_validate(values)


def _parent_and_child(created: dict):
    parent = next(
        column
        for column in created["columns"]
        if column["type"] == "email_validation"
    )
    child = next(
        column
        for column in created["columns"]
        if str(column.get("source_column_id") or "") == str(parent["id"])
    )
    return parent, child


@pytest.mark.asyncio
async def test_email_validation_column_stores_config_and_rejects_table_create() -> None:
    service, _repository = _service(email_validator=FakeValidator())
    table, ids = await _table_with_email(service)
    created = await service.add_column(
        table["id"], _validation_payload(ids["Email"])
    )
    parent, child = _parent_and_child(created)
    assert parent["config"] == {
        "email_column_id": ids["Email"],
        "validator": "millionverifier",
        "accept_catchall": False,
    }
    assert child["name"] == "Valid"
    assert child["type"] == "boolean"
    assert child["source_field"] == "valid"

    with pytest.raises(ValueError, match="email_validation columns require"):
        ColumnCreate(name="Email valid", type="email_validation")
    with pytest.raises(ValueError, match="only valid when type is email_validation"):
        ColumnCreate(
            name="Email",
            type="text",
            email_validation=EmailValidationConfig(email_column_id=ids["Email"]),
        )

    with pytest.raises(ValueError, match="after the table exists"):
        TableCreate.model_validate(
            {
                "name": "Nope",
                "columns": [
                    {
                        "name": "Email valid",
                        "type": "email_validation",
                        "email_validation": {"email_column_id": ids["Email"]},
                    }
                ],
            }
        )


@pytest.mark.asyncio
async def test_email_validation_requires_a_text_source_column() -> None:
    service, _repository = _service(email_validator=FakeValidator())
    table, ids = await _table_with_email(
        service, extra_columns=[ColumnCreate(name="Active", type="boolean")]
    )
    with pytest.raises(TableValidationError, match="must reference a text column"):
        await service.add_column(
            table["id"], _validation_payload(ids["Active"])
        )
    with pytest.raises(TableValidationError, match="Unknown input column"):
        await service.add_column(
            table["id"], _validation_payload(str(uuid4()))
        )


@pytest.mark.asyncio
async def test_email_validation_run_writes_result_and_skips_blank_rows() -> None:
    validator = FakeValidator("ok")
    service, _repository = _service(email_validator=validator)
    table, ids = await _table_with_email(service)
    created = await service.add_column(
        table["id"], _validation_payload(ids["Email"])
    )
    parent, child = _parent_and_child(created)
    filled = await service.add_row(
        table["id"],
        RowCreate(values={ids["Email"]: "Ada@Acme.com"}),
    )
    await service.add_row(table["id"], RowCreate())
    run = await service.start_email_validation_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(),
        created_by=str(uuid4()),
    )
    await service.execute_email_validation_run(str(run["id"]))
    finished = await service.get_email_validation_run(
        table["id"], str(parent["id"]), str(run["id"])
    )
    assert finished["status"] == "succeeded"
    assert finished["succeeded_count"] == 1
    assert finished["skipped_count"] == 1
    rows = await service.list_rows(table["id"], limit=100, offset=0)
    by_id = {row["id"]: row for row in rows["items"]}
    cell = by_id[filled["id"]]["values"][str(parent["id"])]
    assert cell == {
        "status": "succeeded",
        "email": "Ada@acme.com",
        "validator": "millionverifier",
        "result": "ok",
        "valid": True,
        "error": None,
    }
    assert by_id[filled["id"]]["values"][str(child["id"])] is True
    assert validator.calls == ["Ada@acme.com"]
    blank = next(row for row in rows["items"] if row["id"] != filled["id"])
    assert blank["values"][str(parent["id"])]["status"] == "skipped"
    assert blank["values"][str(child["id"])] is None

    with pytest.raises(TableValidationError, match="computed and cannot be patched"):
        await service.update_row(
            table["id"],
            filled["id"],
            RowUpdate(values={parent["id"]: "nope"}),
        )


@pytest.mark.asyncio
async def test_email_validation_run_requires_validator_and_skips_succeeded() -> None:
    service, _repository = _service()
    table, ids = await _table_with_email(service)
    created = await service.add_column(
        table["id"], _validation_payload(ids["Email"])
    )
    parent = next(
        column
        for column in created["columns"]
        if column["type"] == "email_validation"
    )
    with pytest.raises(EmailEnrichmentUnavailableError):
        await service.start_email_validation_run(
            table["id"],
            str(parent["id"]),
            SheriffRunCreate(),
            created_by=str(uuid4()),
        )

    validator = FakeValidator("ok")
    service, _repository = _service(email_validator=validator)
    table, ids = await _table_with_email(service)
    created = await service.add_column(
        table["id"], _validation_payload(ids["Email"])
    )
    parent = next(
        column
        for column in created["columns"]
        if column["type"] == "email_validation"
    )
    row = await service.add_row(
        table["id"],
        RowCreate(values={ids["Email"]: "ada@acme.com"}),
    )
    first = await service.start_email_validation_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_validation_run(str(first["id"]))
    second = await service.start_email_validation_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_validation_run(str(second["id"]))
    assert second["skipped_count"] == 1
    assert validator.calls == ["ada@acme.com"]


@pytest.mark.asyncio
async def test_email_validation_treats_catchall_as_invalid_unless_accepted() -> None:
    validator = FakeValidator("catch_all")
    service, _repository = _service(email_validator=validator)
    table, ids = await _table_with_email(service)
    created = await service.add_column(
        table["id"], _validation_payload(ids["Email"])
    )
    parent, child = _parent_and_child(created)
    row = await service.add_row(
        table["id"],
        RowCreate(values={ids["Email"]: "ada@acme.com"}),
    )
    run = await service.start_email_validation_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_validation_run(str(run["id"]))
    listed = await service.list_rows(table["id"], limit=10, offset=0)
    cell = listed["items"][0]["values"][str(parent["id"])]
    assert cell["status"] == "succeeded"
    assert cell["result"] == "catch_all"
    assert cell["valid"] is False
    assert listed["items"][0]["values"][str(child["id"])] is False

    updated = await service.update_column(
        table["id"],
        str(parent["id"]),
        ColumnUpdate(
            email_validation=EmailValidationConfig(
                email_column_id=ids["Email"],
                accept_catchall=True,
            )
        ),
    )
    assert updated["config"]["accept_catchall"] is True
    listed = await service.list_rows(table["id"], limit=10, offset=0)
    cell = listed["items"][0]["values"][str(parent["id"])]
    assert cell["valid"] is True
    assert listed["items"][0]["values"][str(child["id"])] is True
    assert validator.calls == ["ada@acme.com"]


@pytest.mark.asyncio
async def test_email_validation_run_marks_provider_failures() -> None:
    service, _repository = _service(email_validator=FailingValidator())
    table, ids = await _table_with_email(service)
    created = await service.add_column(
        table["id"], _validation_payload(ids["Email"])
    )
    parent = next(
        column
        for column in created["columns"]
        if column["type"] == "email_validation"
    )
    row = await service.add_row(
        table["id"],
        RowCreate(values={ids["Email"]: "ada@acme.com"}),
    )
    run = await service.start_email_validation_run(
        table["id"],
        str(parent["id"]),
        SheriffRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_email_validation_run(str(run["id"]))
    finished = await service.get_column_run(
        table["id"], str(parent["id"]), str(run["id"])
    )
    assert finished["status"] == "failed"
    listed = await service.list_rows(table["id"], limit=10, offset=0)
    cell = listed["items"][0]["values"][str(parent["id"])]
    assert cell["status"] == "failed"
    assert cell["error"] == "MillionVerifier request failed"


@pytest.mark.asyncio
async def test_email_validation_filters_only_support_is_empty() -> None:
    service, _repository = _service(email_validator=FakeValidator())
    table, ids = await _table_with_email(service)
    created = await service.add_column(
        table["id"], _validation_payload(ids["Email"])
    )
    parent = next(
        column
        for column in created["columns"]
        if column["type"] == "email_validation"
    )
    with pytest.raises(TableValidationError, match="only support is_empty"):
        await service.replace_filters(
            table["id"],
            TableFiltersUpdate(
                filters=[
                    TableFilter(
                        column_id=parent["id"],
                        operator="eq",
                        value="ok",
                    )
                ]
            ),
        )
    await service.replace_filters(
        table["id"],
        TableFiltersUpdate(
            filters=[
                TableFilter(column_id=parent["id"], operator="is_empty"),
            ]
        ),
    )
