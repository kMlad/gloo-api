from copy import deepcopy
from uuid import uuid4

import pytest
from postgrest.exceptions import APIError

from app.tables.schemas import (
    ColumnCreate,
    ColumnUpdate,
    RowCreate,
    RowUpdate,
    TableCreate,
    TableFilter,
    TableFiltersUpdate,
    TableUpdate,
)
from app.tables.service import (
    TableConflictError,
    TableNotFoundError,
    TableService,
    TableValidationError,
)
from app.utils import to_iso, utc_now


def _now() -> str:
    return to_iso(utc_now())


def _unique_error() -> APIError:
    return APIError({"message": "duplicate key", "code": "23505"})


class FakeTableRepository:
    def __init__(self) -> None:
        self.tables: dict[str, dict] = {}
        self.columns: dict[str, dict] = {}
        self.rows: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.run_items: dict[str, dict] = {}

    async def list_tables(self) -> list[dict]:
        items = []
        for table in self.tables.values():
            table_id = table["id"]
            items.append(
                {
                    **table,
                    "column_count": sum(
                        1 for column in self.columns.values() if column["table_id"] == table_id
                    ),
                    "row_count": sum(
                        1 for row in self.rows.values() if row["table_id"] == table_id
                    ),
                }
            )
        return items

    async def get_table(self, table_id: str) -> dict | None:
        table = self.tables.get(table_id)
        return deepcopy(table) if table else None

    async def create_table(
        self, *, name: str, created_by: str, filters: list[dict] | None = None
    ) -> dict:
        table_id = str(uuid4())
        now = _now()
        table = {
            "id": table_id,
            "name": name,
            "created_by": created_by,
            "filters": filters or [],
            "created_at": now,
            "updated_at": now,
        }
        self.tables[table_id] = table
        return deepcopy(table)

    async def update_table(
        self,
        table_id: str,
        *,
        name: str | None = None,
        filters: list[dict] | None = None,
    ) -> dict | None:
        table = self.tables.get(table_id)
        if table is None:
            return None
        if name is not None:
            table["name"] = name
        if filters is not None:
            table["filters"] = filters
        table["updated_at"] = _now()
        return deepcopy(table)

    async def delete_table(self, table_id: str) -> bool:
        existed = table_id in self.tables
        self.tables.pop(table_id, None)
        for column_id, column in list(self.columns.items()):
            if column["table_id"] == table_id:
                del self.columns[column_id]
        for row_id, row in list(self.rows.items()):
            if row["table_id"] == table_id:
                del self.rows[row_id]
        for run_id, run in list(self.runs.items()):
            if run["table_id"] == table_id:
                del self.runs[run_id]
                for item_id, item in list(self.run_items.items()):
                    if item["run_id"] == run_id:
                        del self.run_items[item_id]
        return existed

    async def list_columns(self, table_id: str) -> list[dict]:
        columns = [
            deepcopy(column)
            for column in self.columns.values()
            if column["table_id"] == table_id
        ]
        return sorted(columns, key=lambda column: (column["position"], column["id"]))

    async def get_column(self, table_id: str, column_id: str) -> dict | None:
        column = self.columns.get(column_id)
        if column is None or column["table_id"] != table_id:
            return None
        return deepcopy(column)

    async def insert_columns(self, columns: list[dict]) -> list[dict]:
        inserted = []
        for column in columns:
            self._assert_unique_column(column)
            column_id = column.get("id") or str(uuid4())
            record = {**column, "id": column_id}
            self.columns[column_id] = record
            inserted.append(deepcopy(record))
        return sorted(inserted, key=lambda column: (column["position"], column["id"]))

    async def update_column(
        self,
        column_id: str,
        *,
        name: str | None = None,
        hidden: bool | None = None,
        position: int | None = None,
        config: dict | None = None,
    ) -> dict | None:
        column = self.columns.get(column_id)
        if column is None:
            return None
        updated = {**column}
        if name is not None:
            updated["name"] = name
        if hidden is not None:
            updated["hidden"] = hidden
        if position is not None:
            updated["position"] = position
        if config is not None:
            updated["config"] = config
        self._assert_unique_column(updated, ignore_id=column_id)
        updated["updated_at"] = _now()
        self.columns[column_id] = updated
        return deepcopy(updated)

    async def update_column_positions(self, assignments: list[tuple[str, int]]) -> None:
        for column_id, position in assignments:
            await self.update_column(column_id, position=position)

    async def delete_column(self, column_id: str) -> bool:
        for child_id, column in list(self.columns.items()):
            if str(column.get("source_column_id") or "") == column_id:
                del self.columns[child_id]
        existed = self.columns.pop(column_id, None) is not None
        for run_id, run in list(self.runs.items()):
            if run["column_id"] == column_id:
                del self.runs[run_id]
                for item_id, item in list(self.run_items.items()):
                    if item["run_id"] == run_id:
                        del self.run_items[item_id]
        return existed

    async def max_column_position(self, table_id: str) -> int | None:
        positions = [
            column["position"]
            for column in self.columns.values()
            if column["table_id"] == table_id
        ]
        return max(positions) if positions else None

    async def list_rows(self, table_id: str) -> list[dict]:
        rows = [
            deepcopy(row) for row in self.rows.values() if row["table_id"] == table_id
        ]
        return sorted(rows, key=lambda row: (row["position"], row["id"]))

    async def get_row(self, table_id: str, row_id: str) -> dict | None:
        row = self.rows.get(row_id)
        if row is None or row["table_id"] != table_id:
            return None
        return deepcopy(row)

    async def insert_rows(self, rows: list[dict]) -> list[dict]:
        inserted = []
        for row in rows:
            self._assert_unique_row_position(row)
            row_id = row.get("id") or str(uuid4())
            record = {**row, "id": row_id, "values": dict(row.get("values") or {})}
            self.rows[row_id] = record
            inserted.append(deepcopy(record))
        return sorted(inserted, key=lambda row: (row["position"], row["id"]))

    async def update_row_values(self, row_id: str, values: dict) -> dict | None:
        row = self.rows.get(row_id)
        if row is None:
            return None
        row["values"] = dict(values)
        row["updated_at"] = _now()
        return deepcopy(row)

    async def delete_row(self, row_id: str) -> bool:
        return self.rows.pop(row_id, None) is not None

    async def max_row_position(self, table_id: str) -> int | None:
        positions = [
            row["position"] for row in self.rows.values() if row["table_id"] == table_id
        ]
        return max(positions) if positions else None

    async def replace_row_values(self, updates: list[tuple[str, dict]]) -> None:
        for row_id, values in updates:
            await self.update_row_values(row_id, values)

    async def insert_claygent_run(self, record: dict) -> dict:
        run_id = record.get("id") or str(uuid4())
        stored = {**record, "id": run_id}
        self.runs[run_id] = stored
        return deepcopy(stored)

    async def update_claygent_run(self, run_id: str, values: dict) -> dict | None:
        run = self.runs.get(run_id)
        if run is None:
            return None
        run.update(values)
        run["updated_at"] = _now()
        return deepcopy(run)

    async def get_claygent_run(
        self, table_id: str, column_id: str, run_id: str
    ) -> dict | None:
        run = self.runs.get(run_id)
        if run is None:
            return None
        if run["table_id"] != table_id or run["column_id"] != column_id:
            return None
        return deepcopy(run)

    async def get_claygent_run_by_id(self, run_id: str) -> dict | None:
        run = self.runs.get(run_id)
        return deepcopy(run) if run else None

    async def insert_claygent_run_items(self, items: list[dict]) -> list[dict]:
        inserted = []
        for item in items:
            item_id = item.get("id") or str(uuid4())
            stored = {**item, "id": item_id}
            self.run_items[item_id] = stored
            inserted.append(deepcopy(stored))
        return inserted

    async def list_claygent_run_items(self, run_id: str) -> list[dict]:
        items = [
            deepcopy(item)
            for item in self.run_items.values()
            if item["run_id"] == run_id
        ]
        return sorted(items, key=lambda item: (item["created_at"], item["id"]))

    async def update_claygent_run_item(self, item_id: str, values: dict) -> dict | None:
        item = self.run_items.get(item_id)
        if item is None:
            return None
        item.update(values)
        item["updated_at"] = _now()
        return deepcopy(item)

    def _assert_unique_column(self, column: dict, *, ignore_id: str | None = None) -> None:
        for existing in self.columns.values():
            if existing["id"] == ignore_id:
                continue
            if existing["table_id"] != column["table_id"]:
                continue
            if existing["name"] == column["name"] or existing["position"] == column["position"]:
                raise _unique_error()

    def _assert_unique_row_position(self, row: dict) -> None:
        for existing in self.rows.values():
            if (
                existing["table_id"] == row["table_id"]
                and existing["position"] == row["position"]
            ):
                raise _unique_error()


def _service(agent=None) -> tuple[TableService, FakeTableRepository]:
    repository = FakeTableRepository()
    return TableService(repository, claygent_agent=agent), repository


@pytest.mark.asyncio
async def test_create_table_and_add_column_leaves_existing_cells_empty() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(name="Outbound", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    company_id = table["columns"][0]["id"]
    row = await service.add_row(
        table["id"],
        RowCreate(values={company_id: "Acme"}),
    )
    assert row["values"][str(company_id)] == "Acme"

    added = await service.add_column(table["id"], ColumnCreate(name="Active", type="boolean"))
    listed = await service.list_rows(table["id"], limit=100, offset=0)
    assert listed["items"][0]["values"][str(company_id)] == "Acme"
    assert listed["items"][0]["values"][str(added["id"])] is None
    stored = _repository.rows[listed["items"][0]["id"]]
    assert str(added["id"]) not in stored["values"]


@pytest.mark.asyncio
async def test_reorder_columns_persists_positions() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(
            name="Sheet",
            columns=[ColumnCreate(name="A"), ColumnCreate(name="B"), ColumnCreate(name="C")],
        ),
        created_by=str(uuid4()),
    )
    original = [column["id"] for column in table["columns"]]
    reordered = await service.reorder_columns(
        table["id"], [original[2], original[0], original[1]]
    )
    assert [column["name"] for column in reordered["columns"]] == ["C", "A", "B"]
    assert [column["position"] for column in reordered["columns"]] == [0, 1, 2]


@pytest.mark.asyncio
async def test_filters_eq_contains_and_is_empty() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(
            name="Leads",
            columns=[
                ColumnCreate(name="Company"),
                ColumnCreate(name="Active", type="boolean"),
            ],
        ),
        created_by=str(uuid4()),
    )
    company_id = table["columns"][0]["id"]
    active_id = table["columns"][1]["id"]
    await service.add_row(table["id"], RowCreate(values={company_id: "Acme", active_id: True}))
    await service.add_row(table["id"], RowCreate(values={company_id: "Globex"}))
    await service.add_row(table["id"], RowCreate(values={company_id: "Initech", active_id: False}))

    await service.replace_filters(
        table["id"],
        TableFiltersUpdate(
            filters=[TableFilter(column_id=company_id, operator="contains", value="ac")]
        ),
    )
    matched = await service.list_rows(table["id"], limit=100, offset=0)
    assert [item["values"][str(company_id)] for item in matched["items"]] == ["Acme"]

    await service.replace_filters(
        table["id"],
        TableFiltersUpdate(
            filters=[TableFilter(column_id=active_id, operator="eq", value=True)]
        ),
    )
    matched = await service.list_rows(table["id"], limit=100, offset=0)
    assert matched["total"] == 1
    assert matched["items"][0]["values"][str(company_id)] == "Acme"

    await service.replace_filters(
        table["id"],
        TableFiltersUpdate(
            filters=[TableFilter(column_id=active_id, operator="is_empty")]
        ),
    )
    matched = await service.list_rows(table["id"], limit=100, offset=0)
    assert matched["total"] == 1
    assert matched["items"][0]["values"][str(company_id)] == "Globex"

    with pytest.raises(TableValidationError, match="contains"):
        await service.replace_filters(
            table["id"],
            TableFiltersUpdate(
                filters=[
                    TableFilter(column_id=active_id, operator="contains", value="yes")
                ]
            ),
        )


@pytest.mark.asyncio
async def test_cell_merge_and_clear() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(
            name="Sheet",
            columns=[ColumnCreate(name="Name"), ColumnCreate(name="Active", type="boolean")],
        ),
        created_by=str(uuid4()),
    )
    name_id = table["columns"][0]["id"]
    active_id = table["columns"][1]["id"]
    row = await service.add_row(
        table["id"],
        RowCreate(values={name_id: "Pat", active_id: True}),
    )
    updated = await service.update_row(
        table["id"],
        row["id"],
        RowUpdate(values={name_id: "Lee"}),
    )
    assert updated["values"][str(name_id)] == "Lee"
    assert updated["values"][str(active_id)] is True

    cleared = await service.update_row(
        table["id"],
        row["id"],
        RowUpdate(values={active_id: None}),
    )
    assert cleared["values"][str(active_id)] is None
    assert str(active_id) not in _repository.rows[row["id"]]["values"]

    with pytest.raises(TableValidationError, match="boolean"):
        await service.update_row(
            table["id"],
            row["id"],
            RowUpdate(values={active_id: "yes"}),
        )


@pytest.mark.asyncio
async def test_import_csv_creates_text_columns_and_rolls_back_on_failure() -> None:
    service, repository = _service()
    created_by = str(uuid4())
    table = await service.import_csv(
        content=b"Company,Name\nAcme,Pat\nGlobex,Lee\n",
        filename="outbound.csv",
        name=None,
        created_by=created_by,
    )
    assert table["name"] == "outbound"
    assert [column["name"] for column in table["columns"]] == ["Company", "Name"]
    assert all(column["type"] == "text" for column in table["columns"])
    rows = await service.list_rows(table["id"], limit=100, offset=0)
    assert rows["total"] == 2
    company_id = str(table["columns"][0]["id"])
    assert rows["items"][0]["values"][company_id] == "Acme"

    original_insert = repository.insert_rows

    async def fail_insert(rows):
        raise RuntimeError("insert failed")

    repository.insert_rows = fail_insert  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await service.import_csv(
            content=b"Name\nPat\n",
            filename="broken.csv",
            name=None,
            created_by=created_by,
        )
    repository.insert_rows = original_insert  # type: ignore[method-assign]
    listed = await service.list_tables()
    assert listed["items"][0]["name"] == "outbound"
    assert len(listed["items"]) == 1


@pytest.mark.asyncio
async def test_duplicate_column_name_and_missing_table() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Name")]),
        created_by=str(uuid4()),
    )
    with pytest.raises(TableConflictError, match="already exists"):
        await service.add_column(table["id"], ColumnCreate(name="Name"))
    with pytest.raises(TableNotFoundError):
        await service.get_table(str(uuid4()))


@pytest.mark.asyncio
async def test_rename_hide_and_delete_column_strips_values() -> None:
    service, repository = _service()
    table = await service.create_table(
        TableCreate(
            name="Sheet",
            columns=[ColumnCreate(name="Keep"), ColumnCreate(name="Drop")],
        ),
        created_by=str(uuid4()),
    )
    keep_id = str(table["columns"][0]["id"])
    drop_id = str(table["columns"][1]["id"])
    row = await service.add_row(
        table["id"],
        RowCreate(values={table["columns"][0]["id"]: "a", table["columns"][1]["id"]: "b"}),
    )
    hidden = await service.update_column(
        table["id"], drop_id, ColumnUpdate(name="Gone", hidden=True)
    )
    assert hidden["name"] == "Gone"
    assert hidden["hidden"] is True
    await service.delete_column(table["id"], drop_id)
    stored = repository.rows[row["id"]]
    assert drop_id not in stored["values"]
    assert stored["values"][keep_id] == "a"
    remaining = await service.get_table(table["id"])
    assert [column["name"] for column in remaining["columns"]] == ["Keep"]


@pytest.mark.asyncio
async def test_rename_table_and_delete_row() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(name="Old", columns=[ColumnCreate(name="Name")]),
        created_by=str(uuid4()),
    )
    renamed = await service.update_table(table["id"], TableUpdate(name="New"))
    assert renamed["name"] == "New"
    row = await service.add_row(table["id"], RowCreate())
    await service.delete_row(table["id"], row["id"])
    listed = await service.list_rows(table["id"], limit=100, offset=0)
    assert listed["total"] == 0
    await service.delete_table(table["id"])
    with pytest.raises(TableNotFoundError):
        await service.get_table(table["id"])
