import asyncio
import logging
from typing import Any, Literal
from uuid import UUID, uuid4

from postgrest.exceptions import APIError

from app.tables.email_enrichment import (
    EmailEnrichmentUnavailableError,
    EmailFinder,
    EmailInputs,
    EmailValidator,
    run_waterfall,
)
from app.tables.email_enrichment.inputs import (
    cell_text,
    normalize_domain,
    normalize_email,
    person_linkedin,
)
from app.tables.email_enrichment.protocol import AttemptRecord, WaterfallStep
from app.tables.sheriff import (
    DEFAULT_SHERIFF_MODEL,
    SHERIFF_MODELS,
    PerplexityUsage,
    SheriffAgent,
    SheriffUnavailableError,
    InvalidPlaceholderError,
    UnknownPlaceholderError,
)
from app.tables.sheriff.prompts import (
    interpolate_prompt,
    is_not_found,
    placeholder_names,
    stringify_cell,
    unique_child_name,
)
from app.tables.csv_export import csv_filename, format_csv_cell, render_csv
from app.tables.csv_import import parse_csv, table_name_from_filename
from app.tables.repository import TableRepository, is_unique_violation
from app.tables.schemas import (
    COMPUTED_COLUMN_TYPES,
    EmailEnrichmentConfig,
    EmailValidationConfig,
    SheriffConfig,
    SheriffExpandRequest,
    SheriffOutputField,
    SheriffRunCreate,
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

logger = logging.getLogger(__name__)

_POSITION_OFFSET = 10_000
_VALIDATION_HARD_ERRORS = {"failed", "rate_limited", "timed_out"}


class TableNotFoundError(Exception):
    pass


class TableValidationError(Exception):
    pass


class TableConflictError(Exception):
    pass


class TableService:
    def __init__(
        self,
        repository: TableRepository,
        *,
        sheriff_agent: SheriffAgent | None = None,
        sheriff_concurrency: int = 3,
        email_finders: dict[str, EmailFinder] | None = None,
        email_validator: EmailValidator | None = None,
        email_concurrency: int = 3,
    ) -> None:
        self._repository = repository
        self._agent = sheriff_agent
        self._concurrency = sheriff_concurrency
        self._email_finders = email_finders or {}
        self._email_validator = email_validator
        self._email_concurrency = email_concurrency

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
        if payload.type == "sheriff":
            return await self._add_sheriff_column(
                table_id, payload, columns, next_position, now
            )
        if payload.type == "email_enrichment":
            return await self._add_email_enrichment_column(
                table_id, payload, columns, next_position, now
            )
        if payload.type == "email_validation":
            return await self._add_email_validation_column(
                table_id, payload, columns, next_position, now
            )
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
        return _column_response(inserted[0])

    async def update_column(
        self, table_id: str, column_id: str, payload: ColumnUpdate
    ) -> dict[str, Any]:
        columns = await self._require_columns(table_id)
        column = _column_by_id(columns, column_id)
        if payload.name is not None:
            _ensure_unique_column_name(payload.name, columns, ignore_id=column_id)
        config = None
        added_children = False
        if payload.sheriff is not None:
            if column["type"] != "sheriff":
                raise TableValidationError(
                    "sheriff config is only valid on sheriff columns"
                )
            config, added_children = await self._sync_sheriff_outputs(
                table_id,
                column,
                payload.sheriff,
                columns,
                parent_name=payload.name or column["name"],
            )
        if payload.email_enrichment is not None:
            if column["type"] != "email_enrichment":
                raise TableValidationError(
                    "email_enrichment config is only valid on email_enrichment columns"
                )
            email_config = _resolve_email_enrichment_patch(
                payload.email_enrichment, column.get("config")
            )
            _validate_email_input_columns(email_config, columns, column_id)
            config = _email_enrichment_config_record(email_config)
        if payload.email_validation is not None:
            if column["type"] != "email_validation":
                raise TableValidationError(
                    "email_validation config is only valid on email_validation columns"
                )
            validation_config = _resolve_email_validation_patch(
                payload.email_validation, column.get("config")
            )
            _validate_email_validation_column(
                validation_config, columns, column_id
            )
            config = _email_validation_config_record(validation_config)
        try:
            updated = await self._repository.update_column(
                column_id,
                name=payload.name,
                hidden=payload.hidden,
                config=config,
            )
        except APIError as error:
            _reraise_unique(error, "A column with this name already exists")
        if updated is None:
            raise TableNotFoundError("Column not found")
        await self._repository.update_table(table_id)
        if (
            payload.email_enrichment is not None
            and config is not None
            and config.get("accept_catchall")
        ):
            await self._reclassify_catchall_rows(table_id, column_id, columns)
        if payload.email_validation is not None and config is not None:
            await self._reclassify_validation_rows(
                table_id,
                column_id,
                columns,
                bool(config.get("accept_catchall")),
            )
        if added_children:
            return await self.get_table(table_id)
        return _column_response(updated)

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
        columns = await self._require_columns(table_id)
        column = await self._repository.get_column(table_id, column_id)
        if column is None:
            raise TableNotFoundError("Column not found")
        keys_to_strip = {column_id}
        keys_to_strip.update(
            str(item["id"])
            for item in columns
            if str(item.get("source_column_id") or "") == column_id
        )
        rows = await self._repository.list_all_rows(table_id)
        updates: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            values = dict(row.get("values") or {})
            if any(key in values for key in keys_to_strip):
                for key in keys_to_strip:
                    values.pop(key, None)
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
        if filters:
            rows = await self._repository.list_all_rows(table_id)
            matched = [
                row
                for row in rows
                if _row_matches_filters(row.get("values") or {}, filters, columns)
            ]
            page = matched[offset : offset + limit]
            total = len(matched)
        else:
            page, total = await self._repository.list_rows(
                table_id, limit=limit, offset=offset
            )
        return {
            "items": [_row_response(row, columns) for row in page],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def export_csv(
        self,
        table_id: str,
        *,
        sort_column_id: str | None = None,
        sort_direction: Literal["asc", "desc"] = "asc",
    ) -> tuple[str, bytes]:
        table, columns = await self._require_table_and_columns(table_id)
        filters = _parse_filters(table.get("filters") or [])
        _validate_filters(filters, columns)
        rows = await self._repository.list_all_rows(table_id)
        if filters:
            rows = [
                row
                for row in rows
                if _row_matches_filters(row.get("values") or {}, filters, columns)
            ]
        if sort_column_id is not None:
            rows = _sort_rows(rows, columns, sort_column_id, sort_direction)
        visible = [column for column in columns if not column.get("hidden")]
        headers = [str(column["name"]) for column in visible]
        csv_rows = [
            [
                format_csv_cell(
                    column["type"], (row.get("values") or {}).get(str(column["id"]))
                )
                for column in visible
            ]
            for row in rows
        ]
        return csv_filename(str(table["name"])), render_csv(headers, csv_rows)

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

    async def expand_sheriff_prompt(
        self, table_id: str, payload: SheriffExpandRequest
    ) -> dict[str, Any]:
        columns = await self._require_columns(table_id)
        input_columns = _resolve_input_columns(
            payload.goal, columns, extra_ids=payload.column_ids
        )
        agent = self._require_agent()
        result = await agent.expand(
            goal=payload.goal,
            column_names=[
                str(column["name"])
                for column in columns
                if column["type"] != "sheriff"
            ],
        )
        await self._record_perplexity_usage(
            result.usage,
            operation="expand",
            table_id=table_id,
        )
        return {
            "user_prompt": payload.goal,
            "enhanced_prompt": result.enhanced_prompt,
            "outputs": [field.model_dump() for field in result.outputs],
            "input_columns": [
                {"id": column["id"], "name": column["name"]}
                for column in input_columns
            ],
        }

    async def get_column(self, table_id: str, column_id: str) -> dict[str, Any]:
        columns = await self._require_columns(table_id)
        return _column_by_id(columns, column_id)

    async def start_sheriff_run(
        self,
        table_id: str,
        column_id: str,
        payload: SheriffRunCreate,
        *,
        created_by: str,
    ) -> dict[str, Any]:
        columns = await self._require_columns(table_id)
        column = _column_by_id(columns, column_id)
        if column["type"] != "sheriff":
            raise TableValidationError("Runs are only supported on sheriff columns")
        self._require_agent()
        rows = await self._repository.list_all_rows(table_id)
        selected = _select_run_rows(rows, payload.row_ids)
        now = to_iso(utc_now())
        run = await self._repository.insert_sheriff_run(
            {
                "table_id": table_id,
                "column_id": column_id,
                "created_by": created_by,
                "status": "queued",
                "row_ids": (
                    [str(row_id) for row_id in payload.row_ids]
                    if payload.row_ids is not None
                    else None
                ),
                "overwrite": payload.overwrite,
                "total_count": len(selected),
                "succeeded_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "created_at": now,
                "updated_at": now,
            }
        )
        run_id = str(run["id"])
        items: list[dict[str, Any]] = []
        cell_updates: list[tuple[str, dict[str, Any]]] = []
        skipped = 0
        for row in selected:
            row_id = str(row["id"])
            values = dict(row.get("values") or {})
            cell = values.get(column_id)
            skip = (
                not payload.overwrite
                and isinstance(cell, dict)
                and cell.get("status") == "succeeded"
            )
            item_status = "skipped" if skip else "queued"
            if skip:
                skipped += 1
            else:
                values[column_id] = _sheriff_cell(status="queued")
                cell_updates.append((row_id, values))
            items.append(
                {
                    "run_id": run_id,
                    "row_id": row_id,
                    "status": item_status,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        if items:
            await self._repository.insert_sheriff_run_items(items)
        if cell_updates:
            await self._repository.replace_row_values(cell_updates)
        await self._repository.update_sheriff_run(
            run_id, {"skipped_count": skipped, "total_count": len(selected)}
        )
        await self._repository.update_table(table_id)
        return await self.get_sheriff_run(table_id, column_id, run_id)

    async def get_sheriff_options(self, table_id: str) -> dict[str, Any]:
        await self._require_table(table_id)
        return {
            "models": list(SHERIFF_MODELS),
            "default_model": DEFAULT_SHERIFF_MODEL,
            "default_web_search": True,
        }

    async def execute_sheriff_run(self, run_id: str) -> None:
        run = await self._repository.get_sheriff_run_by_id(run_id)
        if run is None:
            return
        table_id = str(run["table_id"])
        column_id = str(run["column_id"])
        await self._repository.update_sheriff_run(run_id, {"status": "running"})
        try:
            columns = await self._require_columns(table_id)
            column = _column_by_id(columns, column_id)
            agent = self._require_agent()
            config = _parse_sheriff_config(column.get("config"))
            prompt = config.enhanced_prompt or config.user_prompt
            children = {
                str(item.get("source_field") or ""): item
                for item in columns
                if str(item.get("source_column_id") or "") == column_id
            }
            rows = {
                str(row["id"]): row
                for row in await self._repository.list_all_rows(table_id)
            }
            items = await self._repository.list_sheriff_run_items(run_id)
            semaphore = asyncio.Semaphore(self._concurrency)

            async def process(item: dict[str, Any]) -> None:
                if item["status"] == "skipped":
                    return
                async with semaphore:
                    await self._execute_sheriff_item(
                        agent=agent,
                        table_id=table_id,
                        column=column,
                        columns=columns,
                        children=children,
                        prompt=prompt,
                        outputs=config.outputs,
                        model=config.model,
                        web_search=config.web_search,
                        web_search_limit=config.web_search_limit,
                        item=item,
                        row=rows.get(str(item["row_id"])),
                    )

            await asyncio.gather(*(process(item) for item in items))
        except Exception as error:
            await self._fail_open_run_items(
                run_id,
                str(error),
                table_id=table_id,
                column_id=column_id,
            )
        await self._finalize_sheriff_run(run_id)
        await self._repository.update_table(table_id)

    async def get_sheriff_run(
        self, table_id: str, column_id: str, run_id: str
    ) -> dict[str, Any]:
        await self._require_table(table_id)
        run = await self._repository.get_sheriff_run(table_id, column_id, run_id)
        if run is None:
            raise TableNotFoundError("Run not found")
        items = await self._repository.list_sheriff_run_items(run_id)
        return {**run, "items": items}

    async def start_email_enrichment_run(
        self,
        table_id: str,
        column_id: str,
        payload: SheriffRunCreate,
        *,
        created_by: str,
    ) -> dict[str, Any]:
        columns = await self._require_columns(table_id)
        column = _column_by_id(columns, column_id)
        if column["type"] != "email_enrichment":
            raise TableValidationError(
                "Email enrichment runs are only supported on email_enrichment columns"
            )
        self._require_email_validator()
        config = _parse_email_enrichment_config(column.get("config"))
        _validate_email_input_columns(config, columns, column_id)
        rows = await self._repository.list_all_rows(table_id)
        selected = _select_run_rows(rows, payload.row_ids)
        now = to_iso(utc_now())
        run = await self._repository.insert_email_enrichment_run(
            {
                "table_id": table_id,
                "column_id": column_id,
                "created_by": created_by,
                "status": "queued",
                "row_ids": (
                    [str(row_id) for row_id in payload.row_ids]
                    if payload.row_ids is not None
                    else None
                ),
                "overwrite": payload.overwrite,
                "total_count": len(selected),
                "succeeded_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "not_found_count": 0,
                "created_at": now,
                "updated_at": now,
            }
        )
        run_id = str(run["id"])
        items: list[dict[str, Any]] = []
        cell_updates: list[tuple[str, dict[str, Any]]] = []
        skipped = 0
        for row in selected:
            row_id = str(row["id"])
            values = dict(row.get("values") or {})
            cell = values.get(column_id)
            skip = (
                not payload.overwrite
                and isinstance(cell, dict)
                and cell.get("status") == "succeeded"
            )
            item_status = "skipped" if skip else "queued"
            if skip:
                skipped += 1
            else:
                values[column_id] = _email_enrichment_cell(status="queued")
                cell_updates.append((row_id, values))
            items.append(
                {
                    "run_id": run_id,
                    "row_id": row_id,
                    "status": item_status,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        if items:
            await self._repository.insert_email_enrichment_run_items(items)
        if cell_updates:
            await self._repository.replace_row_values(cell_updates)
        await self._repository.update_email_enrichment_run(
            run_id, {"skipped_count": skipped, "total_count": len(selected)}
        )
        await self._repository.update_table(table_id)
        return await self.get_email_enrichment_run(table_id, column_id, run_id)

    async def execute_email_enrichment_run(self, run_id: str) -> None:
        run = await self._repository.get_email_enrichment_run_by_id(run_id)
        if run is None:
            return
        table_id = str(run["table_id"])
        column_id = str(run["column_id"])
        await self._repository.update_email_enrichment_run(run_id, {"status": "running"})
        try:
            columns = await self._require_columns(table_id)
            column = _column_by_id(columns, column_id)
            validator = self._require_email_validator()
            config = _parse_email_enrichment_config(column.get("config"))
            children = {
                str(item.get("source_field") or ""): item
                for item in columns
                if str(item.get("source_column_id") or "") == column_id
            }
            rows = {
                str(row["id"]): row
                for row in await self._repository.list_all_rows(table_id)
            }
            items = await self._repository.list_email_enrichment_run_items(run_id)
            semaphore = asyncio.Semaphore(self._email_concurrency)

            async def process(item: dict[str, Any]) -> None:
                if item["status"] == "skipped":
                    return
                async with semaphore:
                    await self._execute_email_enrichment_item(
                        table_id=table_id,
                        column=column,
                        columns=columns,
                        children=children,
                        config=config,
                        validator=validator,
                        item=item,
                        row=rows.get(str(item["row_id"])),
                    )

            await asyncio.gather(*(process(item) for item in items))
        except Exception as error:
            await self._fail_open_email_run_items(
                run_id,
                str(error),
                table_id=table_id,
                column_id=column_id,
            )
        await self._finalize_email_enrichment_run(run_id)
        await self._repository.update_table(table_id)

    async def get_email_enrichment_run(
        self, table_id: str, column_id: str, run_id: str
    ) -> dict[str, Any]:
        await self._require_table(table_id)
        run = await self._repository.get_email_enrichment_run(
            table_id, column_id, run_id
        )
        if run is None:
            raise TableNotFoundError("Run not found")
        items = await self._repository.list_email_enrichment_run_items(run_id)
        return {**run, "items": items}

    async def get_column_run(
        self, table_id: str, column_id: str, run_id: str
    ) -> dict[str, Any]:
        await self._require_table(table_id)
        sheriff = await self._repository.get_sheriff_run(table_id, column_id, run_id)
        if sheriff is not None:
            items = await self._repository.list_sheriff_run_items(run_id)
            return {**sheriff, "not_found_count": 0, "items": items}
        validation = await self._repository.get_email_validation_run(
            table_id, column_id, run_id
        )
        if validation is not None:
            items = await self._repository.list_email_validation_run_items(run_id)
            return {**validation, "items": items}
        return await self.get_email_enrichment_run(table_id, column_id, run_id)

    async def start_email_validation_run(
        self,
        table_id: str,
        column_id: str,
        payload: SheriffRunCreate,
        *,
        created_by: str,
    ) -> dict[str, Any]:
        columns = await self._require_columns(table_id)
        column = _column_by_id(columns, column_id)
        if column["type"] != "email_validation":
            raise TableValidationError(
                "Email validation runs are only supported on email_validation columns"
            )
        self._require_email_validator()
        config = _parse_email_validation_config(column.get("config"))
        _validate_email_validation_column(config, columns, column_id)
        rows = await self._repository.list_all_rows(table_id)
        selected = _select_run_rows(rows, payload.row_ids)
        now = to_iso(utc_now())
        run = await self._repository.insert_email_validation_run(
            {
                "table_id": table_id,
                "column_id": column_id,
                "created_by": created_by,
                "status": "queued",
                "row_ids": (
                    [str(row_id) for row_id in payload.row_ids]
                    if payload.row_ids is not None
                    else None
                ),
                "overwrite": payload.overwrite,
                "total_count": len(selected),
                "succeeded_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "not_found_count": 0,
                "created_at": now,
                "updated_at": now,
            }
        )
        run_id = str(run["id"])
        items: list[dict[str, Any]] = []
        cell_updates: list[tuple[str, dict[str, Any]]] = []
        skipped = 0
        for row in selected:
            row_id = str(row["id"])
            values = dict(row.get("values") or {})
            cell = values.get(column_id)
            skip = (
                not payload.overwrite
                and isinstance(cell, dict)
                and cell.get("status") == "succeeded"
            )
            item_status = "skipped" if skip else "queued"
            if skip:
                skipped += 1
            else:
                values[column_id] = _email_validation_cell(status="queued")
                cell_updates.append((row_id, values))
            items.append(
                {
                    "run_id": run_id,
                    "row_id": row_id,
                    "status": item_status,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        if items:
            await self._repository.insert_email_validation_run_items(items)
        if cell_updates:
            await self._repository.replace_row_values(cell_updates)
        await self._repository.update_email_validation_run(
            run_id, {"skipped_count": skipped, "total_count": len(selected)}
        )
        await self._repository.update_table(table_id)
        return await self.get_email_validation_run(table_id, column_id, run_id)

    async def execute_email_validation_run(self, run_id: str) -> None:
        run = await self._repository.get_email_validation_run_by_id(run_id)
        if run is None:
            return
        table_id = str(run["table_id"])
        column_id = str(run["column_id"])
        await self._repository.update_email_validation_run(run_id, {"status": "running"})
        try:
            columns = await self._require_columns(table_id)
            column = _column_by_id(columns, column_id)
            validator = self._require_email_validator()
            config = _parse_email_validation_config(column.get("config"))
            children = {
                str(item.get("source_field") or ""): item
                for item in columns
                if str(item.get("source_column_id") or "") == column_id
            }
            rows = {
                str(row["id"]): row
                for row in await self._repository.list_all_rows(table_id)
            }
            items = await self._repository.list_email_validation_run_items(run_id)
            semaphore = asyncio.Semaphore(self._email_concurrency)

            async def process(item: dict[str, Any]) -> None:
                if item["status"] == "skipped":
                    return
                async with semaphore:
                    await self._execute_email_validation_item(
                        table_id=table_id,
                        column=column,
                        children=children,
                        config=config,
                        validator=validator,
                        item=item,
                        row=rows.get(str(item["row_id"])),
                    )

            await asyncio.gather(*(process(item) for item in items))
        except Exception as error:
            await self._fail_open_email_validation_run_items(
                run_id,
                str(error),
                table_id=table_id,
                column_id=column_id,
            )
        await self._finalize_email_validation_run(run_id)
        await self._repository.update_table(table_id)

    async def get_email_validation_run(
        self, table_id: str, column_id: str, run_id: str
    ) -> dict[str, Any]:
        await self._require_table(table_id)
        run = await self._repository.get_email_validation_run(
            table_id, column_id, run_id
        )
        if run is None:
            raise TableNotFoundError("Run not found")
        items = await self._repository.list_email_validation_run_items(run_id)
        return {**run, "items": items}

    def _require_agent(self) -> SheriffAgent:
        if self._agent is None:
            raise SheriffUnavailableError("Sheriff is not configured")
        return self._agent

    def _require_email_validator(self) -> EmailValidator:
        if self._email_validator is None:
            raise EmailEnrichmentUnavailableError(
                "MillionVerifier is not configured"
            )
        return self._email_validator

    async def _add_email_enrichment_column(
        self,
        table_id: str,
        payload: ColumnCreate,
        columns: list[dict[str, Any]],
        next_position: int,
        now: str,
    ) -> dict[str, Any]:
        assert payload.email_enrichment is not None
        _validate_email_input_columns(payload.email_enrichment, columns)
        config = _email_enrichment_config_record(payload.email_enrichment)
        parent_id = str(uuid4())
        taken = {str(column["name"]) for column in columns}
        taken.add(payload.name)
        child_name = unique_child_name(payload.name, "email", taken)
        records = [
            {
                "id": parent_id,
                "table_id": table_id,
                "name": payload.name,
                "type": "email_enrichment",
                "position": next_position,
                "hidden": False,
                "config": config,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": str(uuid4()),
                "table_id": table_id,
                "name": child_name,
                "type": "text",
                "position": next_position + 1,
                "hidden": False,
                "source_column_id": parent_id,
                "source_field": "email",
                "created_at": now,
                "updated_at": now,
            },
        ]
        try:
            await self._repository.insert_columns(records)
        except APIError as error:
            _reraise_unique(error, "A column with this name already exists")
        await self._repository.update_table(table_id)
        return await self.get_table(table_id)

    async def _add_email_validation_column(
        self,
        table_id: str,
        payload: ColumnCreate,
        columns: list[dict[str, Any]],
        next_position: int,
        now: str,
    ) -> dict[str, Any]:
        assert payload.email_validation is not None
        _validate_email_validation_column(payload.email_validation, columns)
        parent_id = str(uuid4())
        taken = {str(column["name"]) for column in columns}
        taken.add(payload.name)
        child_name = unique_child_name(payload.name, "valid", taken)
        records = [
            {
                "id": parent_id,
                "table_id": table_id,
                "name": payload.name,
                "type": "email_validation",
                "position": next_position,
                "hidden": False,
                "config": _email_validation_config_record(payload.email_validation),
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": str(uuid4()),
                "table_id": table_id,
                "name": child_name,
                "type": "boolean",
                "position": next_position + 1,
                "hidden": False,
                "source_column_id": parent_id,
                "source_field": "valid",
                "created_at": now,
                "updated_at": now,
            },
        ]
        try:
            await self._repository.insert_columns(records)
        except APIError as error:
            _reraise_unique(error, "A column with this name already exists")
        await self._repository.update_table(table_id)
        return await self.get_table(table_id)

    async def _execute_email_validation_item(
        self,
        *,
        table_id: str,
        column: dict[str, Any],
        children: dict[str, dict[str, Any]],
        config: EmailValidationConfig,
        validator: EmailValidator,
        item: dict[str, Any],
        row: dict[str, Any] | None,
    ) -> None:
        item_id = str(item["id"])
        column_id = str(column["id"])
        if row is None:
            await self._repository.update_email_validation_run_item(
                item_id,
                {"status": "failed", "error_message": "Row not found"},
            )
            return
        row_id = str(row["id"])
        fresh = await self._repository.get_row(table_id, row_id)
        if fresh is None:
            await self._repository.update_email_validation_run_item(
                item_id,
                {"status": "failed", "error_message": "Row not found"},
            )
            return
        values = dict(fresh.get("values") or {})
        values[column_id] = _email_validation_cell(status="running")
        await self._repository.replace_row_values([(row_id, values)])
        await self._repository.update_email_validation_run_item(
            item_id, {"status": "running"}
        )
        email = normalize_email(values.get(str(config.email_column_id)))
        if email is None:
            values[column_id] = _email_validation_cell(
                status="skipped",
                error="Mapped email column is required",
            )
            await self._repository.replace_row_values([(row_id, values)])
            await self._repository.update_email_validation_run_item(
                item_id,
                {
                    "status": "skipped",
                    "error_message": "Mapped email column is required",
                },
            )
            return
        try:
            verification = await validator.verify(email)
        except Exception as error:
            values[column_id] = _email_validation_cell(
                status="failed", email=email, error=str(error)
            )
            await self._repository.replace_row_values([(row_id, values)])
            await self._repository.update_email_validation_run_item(
                item_id,
                {"status": "failed", "error_message": str(error)},
            )
            return
        if verification.status in _VALIDATION_HARD_ERRORS:
            message = verification.error_message or "MillionVerifier request failed"
            values[column_id] = _email_validation_cell(
                status="failed",
                email=email,
                result=verification.result,
                error=message,
            )
            await self._repository.replace_row_values([(row_id, values)])
            await self._repository.update_email_validation_run_item(
                item_id,
                {"status": "failed", "error_message": message},
            )
            return
        valid = _email_is_valid(verification.result, config.accept_catchall)
        _write_email_validation_child(values, children, valid)
        values[column_id] = _email_validation_cell(
            status="succeeded",
            email=email,
            result=verification.result,
            valid=valid,
        )
        await self._repository.replace_row_values([(row_id, values)])
        await self._repository.update_email_validation_run_item(
            item_id, {"status": "succeeded", "error_message": None}
        )

    async def _reclassify_validation_rows(
        self,
        table_id: str,
        column_id: str,
        columns: list[dict[str, Any]],
        accept_catchall: bool,
    ) -> None:
        children = {
            str(item.get("source_field") or ""): item
            for item in columns
            if str(item.get("source_column_id") or "") == column_id
        }
        rows = await self._repository.list_all_rows(table_id)
        updates: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            values = dict(row.get("values") or {})
            cell = values.get(column_id)
            if not isinstance(cell, dict) or cell.get("status") != "succeeded":
                continue
            result = cell.get("result") if isinstance(cell.get("result"), str) else None
            valid = _email_is_valid(result, accept_catchall)
            child = children.get("valid")
            child_id = str(child["id"]) if child is not None else None
            if cell.get("valid") is valid and (
                child_id is None or values.get(child_id) is valid
            ):
                continue
            values[column_id] = {**cell, "valid": valid}
            _write_email_validation_child(values, children, valid)
            updates.append((str(row["id"]), values))
        if updates:
            await self._repository.replace_row_values(updates)

    async def _fail_open_email_validation_run_items(
        self,
        run_id: str,
        message: str,
        *,
        table_id: str,
        column_id: str,
    ) -> None:
        items = await self._repository.list_email_validation_run_items(run_id)
        for item in items:
            if item["status"] not in {"queued", "running"}:
                continue
            await self._repository.update_email_validation_run_item(
                str(item["id"]),
                {"status": "failed", "error_message": message},
            )
            row = await self._repository.get_row(table_id, str(item["row_id"]))
            if row is None:
                continue
            values = dict(row.get("values") or {})
            existing = values.get(column_id)
            email = existing.get("email") if isinstance(existing, dict) else None
            values[column_id] = _email_validation_cell(
                status="failed",
                email=email if isinstance(email, str) else None,
                error=message,
            )
            await self._repository.replace_row_values([(str(item["row_id"]), values)])

    async def _finalize_email_validation_run(self, run_id: str) -> None:
        items = await self._repository.list_email_validation_run_items(run_id)
        succeeded = sum(1 for item in items if item["status"] == "succeeded")
        failed = sum(1 for item in items if item["status"] == "failed")
        skipped = sum(1 for item in items if item["status"] == "skipped")
        if failed and succeeded:
            status = "partial"
        elif failed:
            status = "failed"
        else:
            status = "succeeded"
        await self._repository.update_email_validation_run(
            run_id,
            {
                "status": status,
                "succeeded_count": succeeded,
                "failed_count": failed,
                "skipped_count": skipped,
                "not_found_count": 0,
                "total_count": len(items),
                "completed_at": to_iso(utc_now()),
            },
        )

    async def _execute_email_enrichment_item(
        self,
        *,
        table_id: str,
        column: dict[str, Any],
        columns: list[dict[str, Any]],
        children: dict[str, dict[str, Any]],
        config: EmailEnrichmentConfig,
        validator: EmailValidator,
        item: dict[str, Any],
        row: dict[str, Any] | None,
    ) -> None:
        item_id = str(item["id"])
        column_id = str(column["id"])
        if row is None:
            await self._repository.update_email_enrichment_run_item(
                item_id,
                {"status": "failed", "error_message": "Row not found"},
            )
            return
        row_id = str(row["id"])
        fresh = await self._repository.get_row(table_id, row_id)
        if fresh is None:
            await self._repository.update_email_enrichment_run_item(
                item_id,
                {"status": "failed", "error_message": "Row not found"},
            )
            return
        values = dict(fresh.get("values") or {})
        existing = values.get(column_id)
        rejected = (
            list(existing.get("rejected_emails") or [])
            if isinstance(existing, dict)
            else []
        )
        values[column_id] = _email_enrichment_cell(
            status="running", rejected_emails=rejected
        )
        await self._repository.replace_row_values([(row_id, values)])
        await self._repository.update_email_enrichment_run_item(
            item_id, {"status": "running"}
        )
        inputs = _email_inputs_from_row(config, columns, values)
        if inputs is None:
            values[column_id] = _email_enrichment_cell(
                status="skipped",
                rejected_emails=rejected,
                error="Mapped input columns are required",
            )
            await self._repository.replace_row_values([(row_id, values)])
            await self._repository.update_email_enrichment_run_item(
                item_id,
                {
                    "status": "skipped",
                    "error_message": "Mapped input columns are required",
                },
            )
            return
        try:
            outcome = await run_waterfall(
                providers=list(config.providers),
                finders=self._email_finders,
                validator=validator,
                inputs=inputs,
                rejected_emails=rejected,
                accept_catchall=config.accept_catchall,
            )
        except Exception as error:
            values[column_id] = _email_enrichment_cell(
                status="failed", rejected_emails=rejected, error=str(error)
            )
            await self._repository.replace_row_values([(row_id, values)])
            await self._repository.update_email_enrichment_run_item(
                item_id,
                {"status": "failed", "error_message": str(error)},
            )
            return
        child = children.get("email")
        if outcome.status == "succeeded" and outcome.email and child is not None:
            values[str(child["id"])] = outcome.email
        values[column_id] = _email_enrichment_cell(
            status=outcome.status,
            email=outcome.email,
            provider=outcome.provider,
            validation_result=outcome.validation_result,
            rejected_emails=outcome.rejected_emails,
            steps=outcome.steps,
            error=outcome.error,
        )
        await self._repository.replace_row_values([(row_id, values)])
        await self._repository.update_email_enrichment_run_item(
            item_id,
            {
                "status": outcome.status,
                "error_message": outcome.error,
            },
        )
        await self._persist_email_attempts(
            run_id=str(item["run_id"]),
            item_id=item_id,
            attempts=outcome.attempts,
        )

    async def _reclassify_catchall_rows(
        self,
        table_id: str,
        column_id: str,
        columns: list[dict[str, Any]],
    ) -> None:
        rows = await self._repository.list_all_rows(table_id)
        not_found: list[dict[str, Any]] = []
        for row in rows:
            cell = (row.get("values") or {}).get(column_id)
            if isinstance(cell, dict) and cell.get("status") == "not_found":
                not_found.append(row)
        if not not_found:
            return
        runs = await self._repository.list_email_enrichment_runs_for_column(column_id)
        if not runs:
            return
        run_ids = [str(run["id"]) for run in runs]
        items = await self._repository.list_email_enrichment_run_items_for_runs(run_ids)
        attempts = await self._repository.list_email_enrichment_catchall_attempts(run_ids)
        if not attempts:
            return
        item_row = {str(item["id"]): str(item["row_id"]) for item in items}
        run_created = {
            str(run["id"]): str(run.get("created_at") or "") for run in runs
        }
        by_row: dict[str, list[dict[str, Any]]] = {}
        for attempt in attempts:
            row_id = item_row.get(str(attempt.get("item_id") or ""))
            if not row_id or not attempt.get("email_candidate"):
                continue
            by_row.setdefault(row_id, []).append(attempt)
        child = next(
            (
                column
                for column in columns
                if str(column.get("source_column_id") or "") == column_id
                and column.get("source_field") == "email"
            ),
            None,
        )
        updates: list[tuple[str, dict[str, Any]]] = []
        for row in not_found:
            row_id = str(row["id"])
            row_attempts = by_row.get(row_id)
            if not row_attempts:
                continue
            latest_run_id = str(
                max(
                    row_attempts,
                    key=lambda item: (
                        run_created.get(str(item.get("run_id") or ""), ""),
                        str(item.get("run_id") or ""),
                    ),
                ).get("run_id")
                or ""
            )
            latest = [
                item
                for item in row_attempts
                if str(item.get("run_id") or "") == latest_run_id
            ]
            first = min(latest, key=lambda item: int(item.get("sequence") or 0))
            email = first.get("email_candidate")
            if not isinstance(email, str) or not email:
                continue
            values = dict(row.get("values") or {})
            cell = values.get(column_id)
            if not isinstance(cell, dict):
                continue
            provider = _provider_for_email(cell, email)
            if provider is None:
                continue
            values[column_id] = _promote_catchall_cell(
                cell, email=email, provider=provider
            )
            if child is not None:
                values[str(child["id"])] = email
            updates.append((row_id, values))
        if updates:
            await self._repository.replace_row_values(updates)

    async def _persist_email_attempts(
        self, *, run_id: str, item_id: str, attempts: list[AttemptRecord]
    ) -> None:
        if not attempts:
            return
        now = to_iso(utc_now())
        records = []
        for attempt in attempts:
            records.append(
                {
                    "run_id": run_id,
                    "item_id": item_id,
                    "provider": attempt.provider,
                    "sequence": attempt.sequence,
                    "status": attempt.status,
                    "request_payload": attempt.request_payload,
                    "response_payload": attempt.response_payload,
                    "response_headers": attempt.response_headers or {},
                    "http_status": attempt.http_status,
                    "external_request_id": attempt.external_request_id,
                    "email_candidate": attempt.email_candidate,
                    "validation_result": attempt.validation_result,
                    "error_code": attempt.error_code,
                    "error_message": attempt.error_message,
                    "completed_at": now,
                }
            )
        await self._repository.insert_email_enrichment_attempts(records)

    async def _fail_open_email_run_items(
        self,
        run_id: str,
        message: str,
        *,
        table_id: str,
        column_id: str,
    ) -> None:
        items = await self._repository.list_email_enrichment_run_items(run_id)
        for item in items:
            if item["status"] not in {"queued", "running"}:
                continue
            await self._repository.update_email_enrichment_run_item(
                str(item["id"]),
                {"status": "failed", "error_message": message},
            )
            row = await self._repository.get_row(table_id, str(item["row_id"]))
            if row is None:
                continue
            values = dict(row.get("values") or {})
            existing = values.get(column_id)
            rejected = (
                list(existing.get("rejected_emails") or [])
                if isinstance(existing, dict)
                else []
            )
            values[column_id] = _email_enrichment_cell(
                status="failed", rejected_emails=rejected, error=message
            )
            await self._repository.replace_row_values([(str(item["row_id"]), values)])

    async def _finalize_email_enrichment_run(self, run_id: str) -> None:
        items = await self._repository.list_email_enrichment_run_items(run_id)
        succeeded = sum(1 for item in items if item["status"] == "succeeded")
        failed = sum(1 for item in items if item["status"] == "failed")
        skipped = sum(1 for item in items if item["status"] == "skipped")
        not_found = sum(1 for item in items if item["status"] == "not_found")
        if failed and (succeeded or not_found):
            status = "partial"
        elif failed:
            status = "failed"
        else:
            status = "succeeded"
        await self._repository.update_email_enrichment_run(
            run_id,
            {
                "status": status,
                "succeeded_count": succeeded,
                "failed_count": failed,
                "skipped_count": skipped,
                "not_found_count": not_found,
                "total_count": len(items),
                "completed_at": to_iso(utc_now()),
            },
        )

    async def _add_sheriff_column(
        self,
        table_id: str,
        payload: ColumnCreate,
        columns: list[dict[str, Any]],
        next_position: int,
        now: str,
    ) -> dict[str, Any]:
        assert payload.sheriff is not None
        _resolve_input_columns(
            payload.sheriff.enhanced_prompt or payload.sheriff.user_prompt,
            columns,
        )
        config = _sheriff_config_record(payload.sheriff, columns)
        parent_id = str(uuid4())
        records: list[dict[str, Any]] = [
            {
                "id": parent_id,
                "table_id": table_id,
                "name": payload.name,
                "type": "sheriff",
                "position": next_position,
                "hidden": False,
                "config": config,
                "created_at": now,
                "updated_at": now,
            }
        ]
        taken = {str(column["name"]) for column in columns}
        taken.add(payload.name)
        for index, field in enumerate(payload.sheriff.outputs):
            child_name = unique_child_name(payload.name, field.key, taken)
            taken.add(child_name)
            records.append(
                _sheriff_child_record(
                    table_id=table_id,
                    name=child_name,
                    field=field,
                    position=next_position + 1 + index,
                    source_column_id=parent_id,
                    now=now,
                )
            )
        try:
            await self._repository.insert_columns(records)
        except APIError as error:
            _reraise_unique(error, "A column with this name already exists")
        await self._repository.update_table(table_id)
        return await self.get_table(table_id)

    async def _sync_sheriff_outputs(
        self,
        table_id: str,
        column: dict[str, Any],
        sheriff: SheriffConfig,
        columns: list[dict[str, Any]],
        *,
        parent_name: str,
    ) -> tuple[dict[str, Any], bool]:
        _resolve_input_columns(sheriff.enhanced_prompt or sheriff.user_prompt, columns)
        existing_children = [
            item
            for item in columns
            if str(item.get("source_column_id") or "") == str(column["id"])
        ]
        by_field = {
            str(item.get("source_field") or ""): item for item in existing_children
        }
        new_fields: list[SheriffOutputField] = []
        for field in sheriff.outputs:
            child = by_field.get(field.key)
            if child is None:
                new_fields.append(field)
                continue
            if child["type"] != field.type:
                raise TableValidationError(
                    f"Cannot change the type of output {field.key}"
                )
        if new_fields:
            next_position = 0
            max_position = await self._repository.max_column_position(table_id)
            if max_position is not None:
                next_position = max_position + 1
            now = to_iso(utc_now())
            taken = {str(item["name"]) for item in columns}
            if parent_name != column["name"]:
                taken.discard(column["name"])
                taken.add(parent_name)
            records = []
            for index, field in enumerate(new_fields):
                child_name = unique_child_name(parent_name, field.key, taken)
                taken.add(child_name)
                records.append(
                    _sheriff_child_record(
                        table_id=table_id,
                        name=child_name,
                        field=field,
                        position=next_position + index,
                        source_column_id=str(column["id"]),
                        now=now,
                    )
                )
            try:
                await self._repository.insert_columns(records)
            except APIError as error:
                _reraise_unique(error, "A column with this name already exists")
        return _sheriff_config_record(sheriff, columns), bool(new_fields)

    async def _execute_sheriff_item(
        self,
        *,
        agent: SheriffAgent,
        table_id: str,
        column: dict[str, Any],
        columns: list[dict[str, Any]],
        children: dict[str, dict[str, Any]],
        prompt: str,
        outputs: list[SheriffOutputField],
        model: str,
        web_search: bool,
        web_search_limit: int | None,
        item: dict[str, Any],
        row: dict[str, Any] | None,
    ) -> None:
        item_id = str(item["id"])
        column_id = str(column["id"])
        if row is None:
            await self._repository.update_sheriff_run_item(
                item_id,
                {"status": "failed", "error_message": "Row not found"},
            )
            return
        row_id = str(row["id"])
        fresh = await self._repository.get_row(table_id, row_id)
        if fresh is None:
            await self._repository.update_sheriff_run_item(
                item_id,
                {"status": "failed", "error_message": "Row not found"},
            )
            return
        values = dict(fresh.get("values") or {})
        values[column_id] = _sheriff_cell(status="running")
        await self._repository.replace_row_values([(row_id, values)])
        await self._repository.update_sheriff_run_item(item_id, {"status": "running"})
        try:
            interpolated = interpolate_prompt(
                prompt,
                _interpolation_values(columns, values),
                invalid_names=_sheriff_column_names(columns),
            )
            result = await agent.research(
                prompt=interpolated,
                outputs=outputs,
                model=model,
                web_search=web_search,
                web_search_limit=web_search_limit,
            )
        except (UnknownPlaceholderError, InvalidPlaceholderError) as error:
            values[column_id] = _sheriff_cell(status="failed", error=str(error))
            await self._repository.replace_row_values([(row_id, values)])
            await self._repository.update_sheriff_run_item(
                item_id,
                {"status": "failed", "error_message": str(error)},
            )
            return
        except Exception as error:
            values[column_id] = _sheriff_cell(status="failed", error=str(error))
            await self._repository.replace_row_values([(row_id, values)])
            await self._repository.update_sheriff_run_item(
                item_id,
                {"status": "failed", "error_message": str(error)},
            )
            return
        output, values = _apply_research_output(values, children, outputs, result.output)
        values[column_id] = _sheriff_cell(
            status="succeeded",
            confidence=result.confidence,
            confidence_reason=result.confidence_reason,
            sources=[source.model_dump() for source in result.sources],
            output=output,
        )
        await self._repository.replace_row_values([(row_id, values)])
        await self._repository.update_sheriff_run_item(
            item_id,
            {
                "status": "succeeded",
                "error_message": None,
                "model_response": result.raw or result.model_dump(exclude={"raw"}),
            },
        )
        await self._record_perplexity_usage(
            result.usage,
            operation="research",
            table_id=table_id,
            column_id=column_id,
            run_id=str(item["run_id"]),
            run_item_id=item_id,
        )

    async def _fail_open_run_items(
        self,
        run_id: str,
        message: str,
        *,
        table_id: str,
        column_id: str,
    ) -> None:
        items = await self._repository.list_sheriff_run_items(run_id)
        for item in items:
            if item["status"] not in {"queued", "running"}:
                continue
            await self._repository.update_sheriff_run_item(
                str(item["id"]),
                {"status": "failed", "error_message": message},
            )
            row = await self._repository.get_row(table_id, str(item["row_id"]))
            if row is None:
                continue
            values = dict(row.get("values") or {})
            values[column_id] = _sheriff_cell(status="failed", error=message)
            await self._repository.replace_row_values([(str(item["row_id"]), values)])

    async def _finalize_sheriff_run(self, run_id: str) -> None:
        items = await self._repository.list_sheriff_run_items(run_id)
        succeeded = sum(1 for item in items if item["status"] == "succeeded")
        failed = sum(1 for item in items if item["status"] == "failed")
        skipped = sum(1 for item in items if item["status"] == "skipped")
        if failed and succeeded:
            status = "partial"
        elif failed:
            status = "failed"
        else:
            status = "succeeded"
        await self._repository.update_sheriff_run(
            run_id,
            {
                "status": status,
                "succeeded_count": succeeded,
                "failed_count": failed,
                "skipped_count": skipped,
                "total_count": len(items),
                "completed_at": to_iso(utc_now()),
            },
        )

    async def _record_perplexity_usage(
        self,
        usage: PerplexityUsage | None,
        *,
        operation: Literal["expand", "research"],
        table_id: str,
        column_id: str | None = None,
        run_id: str | None = None,
        run_item_id: str | None = None,
    ) -> None:
        if usage is None:
            return
        try:
            await self._repository.insert_perplexity_usage(
                usage.to_record(
                    operation=operation,
                    table_id=table_id,
                    column_id=column_id,
                    run_id=run_id,
                    run_item_id=run_item_id,
                )
            )
        except Exception:
            logger.exception(
                "failed to persist perplexity usage operation=%s table_id=%s",
                operation,
                table_id,
            )

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
        "columns": [_column_response(column) for column in columns],
    }


def _column_response(column: dict[str, Any]) -> dict[str, Any]:
    return {
        **column,
        "config": column.get("config"),
        "source_column_id": column.get("source_column_id"),
        "source_field": column.get("source_field"),
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
        "logic": item.logic,
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
        if column_type in COMPUTED_COLUMN_TYPES and item.operator not in {
            "is_empty",
            "is_not_empty",
        }:
            raise TableValidationError(
                f"{column_type} columns only support is_empty and is_not_empty filters"
            )
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
    if not filters:
        return True
    columns_by_id = {str(column["id"]): column for column in columns}
    matched = _clause_matches(values, filters[0], columns_by_id)
    for item in filters[1:]:
        clause = _clause_matches(values, item, columns_by_id)
        if item.logic == "or":
            matched = matched or clause
        else:
            matched = matched and clause
    return matched


def _clause_matches(
    values: dict[str, Any],
    item: TableFilter,
    columns_by_id: dict[str, dict[str, Any]],
) -> bool:
    column_id = str(item.column_id)
    cell = values.get(column_id)
    empty = cell is None or cell == ""
    if item.operator == "is_empty":
        return empty
    if item.operator == "is_not_empty":
        return not empty
    if empty:
        return False
    if item.operator == "eq":
        return cell == item.value
    column = columns_by_id[column_id]
    if column["type"] != "text" or not isinstance(cell, str):
        return False
    return str(item.value).casefold() in cell.casefold()


def _sort_rows(
    rows: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    sort_column_id: str,
    sort_direction: Literal["asc", "desc"],
) -> list[dict[str, Any]]:
    column = next(
        (item for item in columns if str(item["id"]) == sort_column_id),
        None,
    )
    if column is None:
        raise TableValidationError("sort_column_id was not found on this table")
    column_id = str(column["id"])
    reverse = sort_direction == "desc"

    def sort_key(row: dict[str, Any]) -> tuple[str, int, str]:
        cell = format_csv_cell(
            column["type"], (row.get("values") or {}).get(column_id)
        )
        return (cell.casefold(), int(row.get("position") or 0), str(row["id"]))

    return sorted(rows, key=sort_key, reverse=reverse)


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
        column_type = column["type"]
        if column_type in COMPUTED_COLUMN_TYPES:
            raise TableValidationError(
                f"Column {column['name']} is computed and cannot be patched"
            )
        if value is None or value == "":
            result.pop(key, None)
            continue
        if column_type == "text":
            if not isinstance(value, str):
                raise TableValidationError(f"Column {column['name']} requires a string")
            result[key] = value
            continue
        if not isinstance(value, bool):
            raise TableValidationError(f"Column {column['name']} requires a boolean")
        result[key] = value
    return result


def _sheriff_child_record(
    *,
    table_id: str,
    name: str,
    field: SheriffOutputField,
    position: int,
    source_column_id: str,
    now: str,
) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "table_id": table_id,
        "name": name,
        "type": field.type,
        "position": position,
        "hidden": False,
        "source_column_id": source_column_id,
        "source_field": field.key,
        "created_at": now,
        "updated_at": now,
    }


def _sheriff_config_record(
    sheriff: SheriffConfig, columns: list[dict[str, Any]]
) -> dict[str, Any]:
    prompt = sheriff.enhanced_prompt or sheriff.user_prompt
    input_columns = _resolve_input_columns(prompt, columns)
    return {
        "user_prompt": sheriff.user_prompt,
        "enhanced_prompt": sheriff.enhanced_prompt,
        "outputs": [field.model_dump() for field in sheriff.outputs],
        "input_column_ids": [str(column["id"]) for column in input_columns],
        "web_search": sheriff.web_search,
        "web_search_limit": sheriff.web_search_limit,
        "model": sheriff.model,
    }


def _parse_sheriff_config(raw: Any) -> SheriffConfig:
    if not isinstance(raw, dict):
        raise TableValidationError("Sheriff column is missing config")
    payload: dict[str, Any] = {
        "user_prompt": raw.get("user_prompt") or "",
        "enhanced_prompt": raw.get("enhanced_prompt"),
        "outputs": raw.get("outputs") or [],
    }
    if "web_search" in raw:
        payload["web_search"] = raw["web_search"]
    if "web_search_limit" in raw:
        payload["web_search_limit"] = raw["web_search_limit"]
    if raw.get("model"):
        payload["model"] = raw["model"]
    return SheriffConfig.model_validate(payload)


def _resolve_input_columns(
    prompt: str,
    columns: list[dict[str, Any]],
    extra_ids: list[UUID] | None = None,
) -> list[dict[str, Any]]:
    by_name = {str(column["name"]): column for column in columns}
    by_id = {str(column["id"]): column for column in columns}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(column: dict[str, Any]) -> None:
        column_id = str(column["id"])
        if column_id in seen:
            return
        if column["type"] in COMPUTED_COLUMN_TYPES:
            raise TableValidationError(
                f"Column {column['name']} cannot be used as a sheriff input"
            )
        seen.add(column_id)
        selected.append(column)

    for name in placeholder_names(prompt):
        column = by_name.get(name)
        if column is None:
            raise TableValidationError(f"Unknown column placeholder {{{{{name}}}}}")
        add(column)
    for extra_id in extra_ids or []:
        column = by_id.get(str(extra_id))
        if column is None:
            raise TableValidationError(f"Unknown column {extra_id}")
        add(column)
    return selected


def _select_run_rows(
    rows: list[dict[str, Any]], row_ids: list[UUID] | None
) -> list[dict[str, Any]]:
    if row_ids is None:
        return rows
    by_id = {str(row["id"]): row for row in rows}
    selected: list[dict[str, Any]] = []
    for row_id in row_ids:
        row = by_id.get(str(row_id))
        if row is None:
            raise TableValidationError(f"Unknown row {row_id}")
        selected.append(row)
    return selected


def _interpolation_values(
    columns: list[dict[str, Any]], values: dict[str, Any]
) -> dict[str, str]:
    result: dict[str, str] = {}
    for column in columns:
        if column["type"] in COMPUTED_COLUMN_TYPES:
            continue
        result[str(column["name"])] = stringify_cell(values.get(str(column["id"])))
    return result


def _sheriff_column_names(columns: list[dict[str, Any]]) -> set[str]:
    return {
        str(column["name"])
        for column in columns
        if column["type"] in COMPUTED_COLUMN_TYPES
    }


def _sheriff_cell(
    *,
    status: str,
    confidence: str | None = None,
    confidence_reason: str | None = None,
    sources: list[dict[str, Any]] | None = None,
    output: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "sources": sources or [],
        "output": output,
        "error": error,
    }


def _apply_research_output(
    values: dict[str, Any],
    children: dict[str, dict[str, Any]],
    outputs: list[SheriffOutputField],
    raw_output: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    cell_output: dict[str, Any] = {}
    for field in outputs:
        raw = raw_output.get(field.key)
        stored: Any = None
        if field.type == "boolean":
            stored = raw if isinstance(raw, bool) else None
        elif not is_not_found(raw):
            stored = str(raw)
            if is_not_found(stored):
                stored = None
        cell_output[field.key] = stored
        child = children.get(field.key)
        if child is None:
            continue
        child_id = str(child["id"])
        if stored is None:
            values.pop(child_id, None)
        else:
            values[child_id] = stored
    return cell_output, values


def _email_enrichment_config_record(config: EmailEnrichmentConfig) -> dict[str, Any]:
    return {
        "providers": list(config.providers),
        "validator": config.validator,
        "accept_catchall": config.accept_catchall,
        "first_name_column_id": str(config.first_name_column_id),
        "last_name_column_id": str(config.last_name_column_id),
        "linkedin_column_id": str(config.linkedin_column_id),
        "company_name_column_id": str(config.company_name_column_id),
        "company_domain_column_id": str(config.company_domain_column_id),
    }


def _resolve_email_enrichment_patch(
    payload: EmailEnrichmentConfig, stored_raw: Any
) -> EmailEnrichmentConfig:
    stored_accept = False
    if isinstance(stored_raw, dict):
        stored_accept = bool(stored_raw.get("accept_catchall", False))
    if "accept_catchall" not in payload.model_fields_set:
        return payload.model_copy(update={"accept_catchall": stored_accept})
    if stored_accept and not payload.accept_catchall:
        raise TableValidationError("accept_catchall cannot be turned off")
    return payload


def _provider_for_email(cell: dict[str, Any], email: str) -> str | None:
    for step in cell.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for item in step.get("emails") or []:
            if isinstance(item, dict) and item.get("email") == email:
                provider = step.get("provider")
                return provider if isinstance(provider, str) and provider else None
    return None


def _promote_catchall_cell(
    cell: dict[str, Any], *, email: str, provider: str
) -> dict[str, Any]:
    rejected = [
        item for item in (cell.get("rejected_emails") or []) if item != email
    ]
    steps: list[dict[str, Any]] = []
    marked = False
    for step in cell.get("steps") or []:
        if not isinstance(step, dict):
            continue
        emails: list[dict[str, Any]] = []
        for item in step.get("emails") or []:
            if not isinstance(item, dict):
                continue
            if (
                not marked
                and item.get("email") == email
                and item.get("validation") == "invalid"
            ):
                emails.append({**item, "validation": "valid"})
                marked = True
            else:
                emails.append(item)
        steps.append({**step, "emails": emails})
    return {
        "status": "succeeded",
        "email": email,
        "provider": provider,
        "validator": cell.get("validator") or "millionverifier",
        "validation_result": "catch_all",
        "rejected_emails": rejected,
        "steps": steps,
        "error": None,
    }


def _parse_email_enrichment_config(raw: Any) -> EmailEnrichmentConfig:
    if not isinstance(raw, dict):
        raise TableValidationError("Email enrichment column is missing config")
    return EmailEnrichmentConfig.model_validate(raw)


def _validate_email_input_columns(
    config: EmailEnrichmentConfig,
    columns: list[dict[str, Any]],
    parent_id: str | None = None,
) -> None:
    by_id = {str(column["id"]): column for column in columns}
    labels = {
        "first_name_column_id": config.first_name_column_id,
        "last_name_column_id": config.last_name_column_id,
        "linkedin_column_id": config.linkedin_column_id,
        "company_name_column_id": config.company_name_column_id,
        "company_domain_column_id": config.company_domain_column_id,
    }
    for label, column_id in labels.items():
        column = by_id.get(str(column_id))
        if column is None:
            raise TableValidationError(f"Unknown input column {column_id}")
        if parent_id is not None and str(column["id"]) == parent_id:
            raise TableValidationError("email enrichment cannot map itself as an input")
        if column["type"] != "text":
            raise TableValidationError(
                f"{label} must reference a text column ({column['name']})"
            )
        source_parent = str(column.get("source_column_id") or "")
        if parent_id is not None and source_parent == parent_id:
            raise TableValidationError(
                "email enrichment cannot map its own child column as an input"
            )


def _email_inputs_from_row(
    config: EmailEnrichmentConfig,
    columns: list[dict[str, Any]],
    values: dict[str, Any],
) -> EmailInputs | None:
    first_name = cell_text(values.get(str(config.first_name_column_id)))
    last_name = cell_text(values.get(str(config.last_name_column_id)))
    linkedin_raw = cell_text(values.get(str(config.linkedin_column_id)))
    company_name = cell_text(values.get(str(config.company_name_column_id)))
    domain_raw = values.get(str(config.company_domain_column_id))
    domain = normalize_domain(domain_raw)
    if not first_name or not last_name or not linkedin_raw or not company_name or not domain:
        return None
    return EmailInputs(
        first_name=first_name,
        last_name=last_name,
        company_name=company_name,
        domain=domain,
        linkedin_url=person_linkedin(linkedin_raw) or linkedin_raw,
    )


def _email_enrichment_cell(
    *,
    status: str,
    email: str | None = None,
    provider: str | None = None,
    validation_result: str | None = None,
    rejected_emails: list[str] | None = None,
    steps: list[WaterfallStep] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "email": email,
        "provider": provider,
        "validator": "millionverifier",
        "validation_result": validation_result,
        "rejected_emails": rejected_emails or [],
        "steps": [step.as_dict() for step in steps or []],
        "error": error,
    }


def _email_validation_config_record(config: EmailValidationConfig) -> dict[str, Any]:
    return {
        "email_column_id": str(config.email_column_id),
        "validator": config.validator,
        "accept_catchall": config.accept_catchall,
    }


def _resolve_email_validation_patch(
    payload: EmailValidationConfig, stored_raw: Any
) -> EmailValidationConfig:
    stored_accept = False
    if isinstance(stored_raw, dict):
        stored_accept = bool(stored_raw.get("accept_catchall", False))
    if "accept_catchall" not in payload.model_fields_set:
        return payload.model_copy(update={"accept_catchall": stored_accept})
    return payload


def _parse_email_validation_config(raw: Any) -> EmailValidationConfig:
    if not isinstance(raw, dict):
        raise TableValidationError("Email validation column is missing config")
    return EmailValidationConfig.model_validate(raw)


def _validate_email_validation_column(
    config: EmailValidationConfig,
    columns: list[dict[str, Any]],
    parent_id: str | None = None,
) -> None:
    by_id = {str(column["id"]): column for column in columns}
    column = by_id.get(str(config.email_column_id))
    if column is None:
        raise TableValidationError(f"Unknown input column {config.email_column_id}")
    if parent_id is not None and str(column["id"]) == parent_id:
        raise TableValidationError("email validation cannot map itself as an input")
    if column["type"] != "text":
        raise TableValidationError(
            f"email_column_id must reference a text column ({column['name']})"
        )
    source_parent = str(column.get("source_column_id") or "")
    if parent_id is not None and source_parent == parent_id:
        raise TableValidationError(
            "email validation cannot map its own child column as an input"
        )


def _email_is_valid(result: str | None, accept_catchall: bool) -> bool:
    if result == "ok":
        return True
    return accept_catchall and result == "catch_all"


def _email_validation_cell(
    *,
    status: str,
    email: str | None = None,
    result: str | None = None,
    valid: bool | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "email": email,
        "validator": "millionverifier",
        "result": result,
        "valid": valid,
        "error": error,
    }


def _write_email_validation_child(
    values: dict[str, Any],
    children: dict[str, dict[str, Any]],
    valid: bool,
) -> None:
    child = children.get("valid")
    if child is None:
        return
    values[str(child["id"])] = valid
