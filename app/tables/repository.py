from typing import Any

from postgrest.exceptions import APIError
from supabase import AsyncClient

from app.utils import chunks, to_iso, utc_now

_ROW_INSERT_CHUNK = 100


def _embedded_count(value: Any) -> int:
    if isinstance(value, list) and value:
        item = value[0]
        if isinstance(item, dict) and "count" in item:
            return int(item["count"])
    if isinstance(value, dict) and "count" in value:
        return int(value["count"])
    return 0


class TableRepository:
    def __init__(self, supabase: AsyncClient) -> None:
        self._db = supabase

    async def list_tables(self) -> list[dict[str, Any]]:
        response = await (
            self._db.table("tables")
            .select(
                "id, name, created_by, created_at, updated_at,"
                " table_columns(count), table_rows(count)"
            )
            .order("updated_at", desc=True)
            .execute()
        )
        items: list[dict[str, Any]] = []
        for row in response.data:
            items.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "created_by": row.get("created_by"),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "column_count": _embedded_count(row.get("table_columns")),
                    "row_count": _embedded_count(row.get("table_rows")),
                }
            )
        return items

    async def get_table(self, table_id: str) -> dict[str, Any] | None:
        response = await (
            self._db.table("tables").select("*").eq("id", table_id).limit(1).execute()
        )
        return response.data[0] if response.data else None

    async def create_table(
        self, *, name: str, created_by: str, filters: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        now = to_iso(utc_now())
        response = await (
            self._db.table("tables")
            .insert(
                {
                    "name": name,
                    "created_by": created_by,
                    "filters": filters or [],
                    "created_at": now,
                    "updated_at": now,
                }
            )
            .execute()
        )
        return response.data[0]

    async def update_table(
        self,
        table_id: str,
        *,
        name: str | None = None,
        filters: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        values: dict[str, Any] = {"updated_at": to_iso(utc_now())}
        if name is not None:
            values["name"] = name
        if filters is not None:
            values["filters"] = filters
        response = await (
            self._db.table("tables").update(values).eq("id", table_id).execute()
        )
        return response.data[0] if response.data else None

    async def delete_table(self, table_id: str) -> bool:
        response = await (
            self._db.table("tables").delete().eq("id", table_id).execute()
        )
        return bool(response.data)

    async def list_columns(self, table_id: str) -> list[dict[str, Any]]:
        response = await (
            self._db.table("table_columns")
            .select("*")
            .eq("table_id", table_id)
            .order("position")
            .order("id")
            .execute()
        )
        return response.data

    async def get_column(self, table_id: str, column_id: str) -> dict[str, Any] | None:
        response = await (
            self._db.table("table_columns")
            .select("*")
            .eq("table_id", table_id)
            .eq("id", column_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def insert_columns(self, columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not columns:
            return []
        response = await self._db.table("table_columns").insert(columns).execute()
        return sorted(response.data, key=lambda column: (column["position"], column["id"]))

    async def update_column(
        self,
        column_id: str,
        *,
        name: str | None = None,
        hidden: bool | None = None,
        position: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        values: dict[str, Any] = {"updated_at": to_iso(utc_now())}
        if name is not None:
            values["name"] = name
        if hidden is not None:
            values["hidden"] = hidden
        if position is not None:
            values["position"] = position
        if config is not None:
            values["config"] = config
        response = await (
            self._db.table("table_columns").update(values).eq("id", column_id).execute()
        )
        return response.data[0] if response.data else None

    async def update_column_positions(
        self, assignments: list[tuple[str, int]]
    ) -> None:
        now = to_iso(utc_now())
        for column_id, position in assignments:
            await (
                self._db.table("table_columns")
                .update({"position": position, "updated_at": now})
                .eq("id", column_id)
                .execute()
            )

    async def delete_column(self, column_id: str) -> bool:
        response = await (
            self._db.table("table_columns").delete().eq("id", column_id).execute()
        )
        return bool(response.data)

    async def max_column_position(self, table_id: str) -> int | None:
        response = await (
            self._db.table("table_columns")
            .select("position")
            .eq("table_id", table_id)
            .order("position", desc=True)
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        return int(response.data[0]["position"])

    async def list_rows(self, table_id: str) -> list[dict[str, Any]]:
        response = await (
            self._db.table("table_rows")
            .select("*")
            .eq("table_id", table_id)
            .order("position")
            .order("id")
            .execute()
        )
        return response.data

    async def get_row(self, table_id: str, row_id: str) -> dict[str, Any] | None:
        response = await (
            self._db.table("table_rows")
            .select("*")
            .eq("table_id", table_id)
            .eq("id", row_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def insert_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        inserted: list[dict[str, Any]] = []
        for group in chunks(rows, _ROW_INSERT_CHUNK):
            response = await self._db.table("table_rows").insert(group).execute()
            inserted.extend(response.data)
        return sorted(inserted, key=lambda row: (row["position"], row["id"]))

    async def update_row_values(
        self, row_id: str, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        response = await (
            self._db.table("table_rows")
            .update({"values": values, "updated_at": to_iso(utc_now())})
            .eq("id", row_id)
            .execute()
        )
        return response.data[0] if response.data else None

    async def delete_row(self, row_id: str) -> bool:
        response = await (
            self._db.table("table_rows").delete().eq("id", row_id).execute()
        )
        return bool(response.data)

    async def max_row_position(self, table_id: str) -> int | None:
        response = await (
            self._db.table("table_rows")
            .select("position")
            .eq("table_id", table_id)
            .order("position", desc=True)
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        return int(response.data[0]["position"])

    async def replace_row_values(self, updates: list[tuple[str, dict[str, Any]]]) -> None:
        now = to_iso(utc_now())
        for row_id, values in updates:
            await (
                self._db.table("table_rows")
                .update({"values": values, "updated_at": now})
                .eq("id", row_id)
                .execute()
            )

    async def insert_claygent_run(self, record: dict[str, Any]) -> dict[str, Any]:
        response = await self._db.table("table_claygent_runs").insert(record).execute()
        return response.data[0]

    async def update_claygent_run(
        self, run_id: str, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        payload = {"updated_at": to_iso(utc_now()), **values}
        response = await (
            self._db.table("table_claygent_runs")
            .update(payload)
            .eq("id", run_id)
            .execute()
        )
        return response.data[0] if response.data else None

    async def get_claygent_run(
        self, table_id: str, column_id: str, run_id: str
    ) -> dict[str, Any] | None:
        response = await (
            self._db.table("table_claygent_runs")
            .select("*")
            .eq("id", run_id)
            .eq("table_id", table_id)
            .eq("column_id", column_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def get_claygent_run_by_id(self, run_id: str) -> dict[str, Any] | None:
        response = await (
            self._db.table("table_claygent_runs")
            .select("*")
            .eq("id", run_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def insert_claygent_run_items(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not items:
            return []
        response = await (
            self._db.table("table_claygent_run_items").insert(items).execute()
        )
        return response.data

    async def list_claygent_run_items(self, run_id: str) -> list[dict[str, Any]]:
        response = await (
            self._db.table("table_claygent_run_items")
            .select("*")
            .eq("run_id", run_id)
            .order("created_at")
            .order("id")
            .execute()
        )
        return response.data

    async def update_claygent_run_item(
        self, item_id: str, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        payload = {"updated_at": to_iso(utc_now()), **values}
        response = await (
            self._db.table("table_claygent_run_items")
            .update(payload)
            .eq("id", item_id)
            .execute()
        )
        return response.data[0] if response.data else None


def is_unique_violation(error: APIError) -> bool:
    return error.code == "23505"
