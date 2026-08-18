from typing import Any
from uuid import UUID

from postgrest.exceptions import APIError

from app.tables.csv_import import parse_csv, table_name_from_filename
from app.tables.repository import TableRepository, is_unique_violation
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
from app.utils import to_iso, utc_now

_POSITION_OFFSET = 10_000


class TableNotFoundError(Exception):
    pass


class TableValidationError(Exception):
    pass


class TableConflictError(Exception):
    pass


class TableService:
    def __init__(self, repository: TableRepository) -> None:
        self._repository = repository

    async def list_tables(self) -> dict[str, Any]:
        items = await self._repository.list_tables()
        return {"items": items}

    async def create_table(self, payload: TableCreate, created_by: str) -> dict[str, Any]:
        table = await self._repository.create_table(
            name=payload.name, created_by=created_by
        )
        try:
            await self._insert_columns(str(table["id"]), payload.columns)
        except Exception:
            await self._repository.delete_table(str(table["id"]))
            raise
        return await self.get_table(str(table["id"]))

    async def import_csv(
        self,
        *,
        content: bytes,
        filename: str | None,
        name: str | None,
        created_by: str,
    ) -> dict[str, Any]:
        parsed = parse_csv(content)
        table_name = (name or "").strip() or table_name_from_filename(filename)
        table = await self._repository.create_table(
            name=table_name, created_by=created_by
        )
        table_id = str(table["id"])
        try:
            columns = await self._insert_columns(
                table_id,
                [ColumnCreate(name=header, type="text") for header in parsed.headers],
            )
            now = to_iso(utc_now())
            rows = []
            for position, cells in enumerate(parsed.rows):
                values: dict[str, Any] = {}
                for column, cell in zip(columns, cells, strict=True):
                    if cell != "":
                        values[str(column["id"])] = cell
                rows.append(
                    {
                        "table_id": table_id,
                        "position": position,
                        "values": values,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            await self._repository.insert_rows(rows)
        except Exception:
            await self._repository.delete_table(table_id)
            raise
        return await self.get_table(table_id)

    async def get_table(self, table_id: str) -> dict[str, Any]:
        table = await self._require_table(table_id)
        columns = await self._repository.list_columns(table_id)
        return _table_response(table, columns)

    async def update_table(self, table_id: str, payload: TableUpdate) -> dict[str, Any]:
        await self._require_table(table_id)
        updated = await self._repository.update_table(table_id, name=payload.name)
        if updated is None:
            raise TableNotFoundError("Table not found")
        columns = await self._repository.list_columns(table_id)
        return _table_response(updated, columns)

    async def replace_filters(
        self, table_id: str, payload: TableFiltersUpdate
    ) -> dict[str, Any]:
        columns = await self._require_columns(table_id)
        _validate_filters(payload.filters, columns)
        updated = await self._repository.update_table(
            table_id,
            filters=[_filter_to_record(item) for item in payload.filters],
        )
        if updated is None:
            raise TableNotFoundError("Table not found")
        return _table_response(updated, columns)

    async def delete_table(self, table_id: str) -> None:
        await self._require_table(table_id)
        await self._repository.delete_table(table_id)

    async def add_column(self, table_id: str, payload: ColumnCreate) -> dict[str, Any]:
        columns = await self._require_columns(table_id)
        _ensure_unique_column_name(payload.name, columns)
        next_position = 0
        max_position = await self._repository.max_column_position(table_id)
        if max_position is not None:
            next_position = max_position + 1
        now = to_iso(utc_now())
        try:
            inserted = await self._repository.insert_columns(
                [
                    {
                        "table_id": table_id,
                        "name": payload.name,
                        "type": payload.type,
                        "position": next_position,
                        "hidden": False,
                        "created_at": now,
                        "updated_at": now,
                    }
                ]
            )
        except APIError as error:
            _reraise_unique(error, "A column with this name already exists")
        await self._repository.update_table(table_id)
        return inserted[0]

    async def update_column(
        self, table_id: str, column_id: str, payload: ColumnUpdate
    ) -> dict[str, Any]:
        columns = await self._require_columns(table_id)
        _column_by_id(columns, column_id)
        if payload.name is not None:
            _ensure_unique_column_name(payload.name, columns, ignore_id=column_id)
        try:
            updated = await self._repository.update_column(
                column_id,
                name=payload.name,
                hidden=payload.hidden,
            )
        except APIError as error:
            _reraise_unique(error, "A column with this name already exists")
        if updated is None:
            raise TableNotFoundError("Column not found")
        await self._repository.update_table(table_id)
        return updated

    async def reorder_columns(
        self, table_id: str, column_ids: list[UUID]
    ) -> dict[str, Any]:
        columns = await self._require_columns(table_id)
        existing = [str(column["id"]) for column in columns]
        requested = [str(column_id) for column_id in column_ids]
        if sorted(requested) != sorted(existing):
            raise TableValidationError(
                "column_ids must include each column in the table exactly once"
            )
        await self._repository.update_column_positions(
            [
                (column_id, index + _POSITION_OFFSET)
                for index, column_id in enumerate(requested)
            ]
        )
        await self._repository.update_column_positions(
            [(column_id, index) for index, column_id in enumerate(requested)]
        )
        await self._repository.update_table(table_id)
        return await self.get_table(table_id)

    async def delete_column(self, table_id: str, column_id: str) -> None:
        await self._require_columns(table_id)
        column = await self._repository.get_column(table_id, column_id)
        if column is None:
            raise TableNotFoundError("Column not found")
        rows = await self._repository.list_rows(table_id)
        updates: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            values = dict(row.get("values") or {})
            if column_id in values:
                values.pop(column_id)
                updates.append((str(row["id"]), values))
        if updates:
            await self._repository.replace_row_values(updates)
        await self._repository.delete_column(column_id)
        await self._repository.update_table(table_id)

    async def list_rows(
        self, table_id: str, *, limit: int, offset: int
    ) -> dict[str, Any]:
        table, columns = await self._require_table_and_columns(table_id)
        filters = _parse_filters(table.get("filters") or [])
        _validate_filters(filters, columns)
        rows = await self._repository.list_rows(table_id)
        matched = [
            row
            for row in rows
            if _row_matches_filters(row.get("values") or {}, filters, columns)
        ]
        page = matched[offset : offset + limit]
        return {
            "items": [_row_response(row, columns) for row in page],
            "total": len(matched),
            "limit": limit,
            "offset": offset,
        }

    async def add_row(self, table_id: str, payload: RowCreate) -> dict[str, Any]:
        columns = await self._require_columns(table_id)
        values = _normalize_values(payload.values, columns, merge_with=None)
        next_position = 0
        max_position = await self._repository.max_row_position(table_id)
        if max_position is not None:
            next_position = max_position + 1
        now = to_iso(utc_now())
        inserted = await self._repository.insert_rows(
            [
                {
                    "table_id": table_id,
                    "position": next_position,
                    "values": values,
                    "created_at": now,
                    "updated_at": now,
                }
            ]
        )
        await self._repository.update_table(table_id)
        return _row_response(inserted[0], columns)

    async def update_row(
        self, table_id: str, row_id: str, payload: RowUpdate
    ) -> dict[str, Any]:
        columns = await self._require_columns(table_id)
        row = await self._repository.get_row(table_id, row_id)
        if row is None:
            raise TableNotFoundError("Row not found")
        values = _normalize_values(
            payload.values, columns, merge_with=dict(row.get("values") or {})
        )
        updated = await self._repository.update_row_values(row_id, values)
        if updated is None:
            raise TableNotFoundError("Row not found")
        await self._repository.update_table(table_id)
        return _row_response(updated, columns)

    async def delete_row(self, table_id: str, row_id: str) -> None:
        await self._require_table(table_id)
        row = await self._repository.get_row(table_id, row_id)
        if row is None:
            raise TableNotFoundError("Row not found")
        await self._repository.delete_row(row_id)
        await self._repository.update_table(table_id)

    async def _require_table(self, table_id: str) -> dict[str, Any]:
        table = await self._repository.get_table(table_id)
        if table is None:
            raise TableNotFoundError("Table not found")
        return table

    async def _require_columns(self, table_id: str) -> list[dict[str, Any]]:
        await self._require_table(table_id)
        return await self._repository.list_columns(table_id)

    async def _require_table_and_columns(
        self, table_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        table = await self._require_table(table_id)
        columns = await self._repository.list_columns(table_id)
        return table, columns

    async def _insert_columns(
        self, table_id: str, columns: list[ColumnCreate]
    ) -> list[dict[str, Any]]:
        if not columns:
            return []
        now = to_iso(utc_now())
        records = [
            {
                "table_id": table_id,
                "name": column.name,
                "type": column.type,
                "position": index,
                "hidden": False,
                "created_at": now,
                "updated_at": now,
            }
            for index, column in enumerate(columns)
        ]
        try:
            return await self._repository.insert_columns(records)
        except APIError as error:
            _reraise_unique(error, "A column with this name already exists")
            raise


def _reraise_unique(error: APIError, message: str) -> None:
    if is_unique_violation(error):
        raise TableConflictError(message) from error
    raise error


def _table_response(
    table: dict[str, Any], columns: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        **table,
        "filters": _parse_filters(table.get("filters") or []),
        "columns": columns,
    }


def _row_response(
    row: dict[str, Any], columns: list[dict[str, Any]]
) -> dict[str, Any]:
    stored = dict(row.get("values") or {})
    values: dict[str, Any] = {}
    for column in columns:
        key = str(column["id"])
        cell = stored.get(key)
        values[key] = None if cell is None or cell == "" else cell
    return {**row, "values": values}


def _parse_filters(raw: Any) -> list[TableFilter]:
    if not isinstance(raw, list):
        return []
    return [TableFilter.model_validate(item) for item in raw]


def _filter_to_record(item: TableFilter) -> dict[str, Any]:
    return {
        "column_id": str(item.column_id),
        "operator": item.operator,
        "value": item.value,
    }


def _validate_filters(
    filters: list[TableFilter], columns: list[dict[str, Any]]
) -> None:
    columns_by_id = {str(column["id"]): column for column in columns}
    for item in filters:
        column = columns_by_id.get(str(item.column_id))
        if column is None:
            raise TableValidationError(
                f"Filter column {item.column_id} was not found on this table"
            )
        column_type = column["type"]
        if item.operator == "contains" and column_type != "text":
            raise TableValidationError("contains filters can only be used on text columns")
        if item.operator == "eq":
            if column_type == "text" and not isinstance(item.value, str):
                raise TableValidationError("text eq filters require a string value")
            if column_type == "boolean" and not isinstance(item.value, bool):
                raise TableValidationError("boolean eq filters require a boolean value")


def _row_matches_filters(
    values: dict[str, Any],
    filters: list[TableFilter],
    columns: list[dict[str, Any]],
) -> bool:
    columns_by_id = {str(column["id"]): column for column in columns}
    for item in filters:
        column_id = str(item.column_id)
        cell = values.get(column_id)
        empty = cell is None or cell == ""
        if item.operator == "is_empty":
            if not empty:
                return False
            continue
        if empty:
            return False
        if item.operator == "eq":
            if cell != item.value:
                return False
            continue
        column = columns_by_id[column_id]
        if column["type"] != "text" or not isinstance(cell, str):
            return False
        needle = str(item.value)
        if needle.casefold() not in cell.casefold():
            return False
    return True


def _column_by_id(columns: list[dict[str, Any]], column_id: str) -> dict[str, Any]:
    for column in columns:
        if str(column["id"]) == column_id:
            return column
    raise TableNotFoundError("Column not found")


def _ensure_unique_column_name(
    name: str, columns: list[dict[str, Any]], *, ignore_id: str | None = None
) -> None:
    for column in columns:
        if ignore_id is not None and str(column["id"]) == ignore_id:
            continue
        if column["name"] == name:
            raise TableConflictError("A column with this name already exists")


def _normalize_values(
    incoming: dict[UUID, Any],
    columns: list[dict[str, Any]],
    *,
    merge_with: dict[str, Any] | None,
) -> dict[str, Any]:
    columns_by_id = {str(column["id"]): column for column in columns}
    result = dict(merge_with or {})
    for column_id, value in incoming.items():
        key = str(column_id)
        column = columns_by_id.get(key)
        if column is None:
            raise TableValidationError(f"Unknown column {key}")
        if value is None or value == "":
            result.pop(key, None)
            continue
        column_type = column["type"]
        if column_type == "text":
            if not isinstance(value, str):
                raise TableValidationError(f"Column {column['name']} requires a string")
            result[key] = value
            continue
        if not isinstance(value, bool):
            raise TableValidationError(f"Column {column['name']} requires a boolean")
        result[key] = value
    return result
