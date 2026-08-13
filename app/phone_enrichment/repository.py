from datetime import timedelta
from typing import Any

from postgrest.exceptions import APIError
from supabase import AsyncClient

from app.utils import parse_datetime, to_iso, utc_now


class ActiveLeadEnrichmentError(Exception):
    pass


class EnrichmentRepository:
    def __init__(self, supabase: AsyncClient) -> None:
        self._db = supabase

    async def get_run_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        response = await (
            self._db.table("phone_enrichment_runs")
            .select("*")
            .eq("idempotency_key", key)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def expire_stale_runs(self) -> None:
        cutoff = to_iso(utc_now() - timedelta(hours=2))
        stale_response = await (
            self._db.table("phone_enrichment_runs")
            .select("id")
            .eq("status", "running")
            .lt("started_at", cutoff)
            .execute()
        )
        now = to_iso(utc_now())
        for stale in stale_response.data:
            run_id = str(stale["id"])
            await (
                self._db.table("phone_enrichment_items")
                .update(
                    {
                        "status": "failed",
                        "had_provider_error": True,
                        "error_message": "Enrichment did not complete before the stale-run timeout",
                        "completed_at": now,
                        "updated_at": now,
                    }
                )
                .eq("run_id", run_id)
                .eq("status", "running")
                .execute()
            )
            await self.update_run(
                run_id,
                {
                    "status": "failed",
                    "errors": [
                        {
                            "scope": "run",
                            "message": "Enrichment did not complete before the stale-run timeout",
                        }
                    ],
                    "completed_at": now,
                },
            )

    async def create_run(
        self,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        selection_mode: str,
        requested_lead_ids: list[str],
        requested_limit: int,
        leads_selected: int,
    ) -> dict[str, Any]:
        await self.expire_stale_runs()
        response = await (
            self._db.table("phone_enrichment_runs")
            .insert(
                {
                    "idempotency_key": idempotency_key,
                    "request_fingerprint": request_fingerprint,
                    "selection_mode": selection_mode,
                    "requested_lead_ids": requested_lead_ids,
                    "requested_limit": requested_limit,
                    "status": "running",
                    "leads_selected": leads_selected,
                }
            )
            .execute()
        )
        return response.data[0]

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        response = await (
            self._db.table("phone_enrichment_runs")
            .select("*")
            .eq("id", run_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def update_run(self, run_id: str, values: dict[str, Any]) -> dict[str, Any]:
        response = await (
            self._db.table("phone_enrichment_runs")
            .update({**values, "updated_at": to_iso(utc_now())})
            .eq("id", run_id)
            .execute()
        )
        return response.data[0]

    async def get_selected_leads(self, lead_ids: list[str]) -> list[dict[str, Any]]:
        if not lead_ids:
            return []
        response = await (
            self._db.table("leads").select("*").in_("id", lead_ids).execute()
        )
        by_id = {str(item["id"]): item for item in response.data}
        ordered = [by_id[lead_id] for lead_id in lead_ids if lead_id in by_id]
        return await self._attach_replies(ordered)

    async def list_eligible_leads(self, limit: int) -> list[dict[str, Any]]:
        response = await (
            self._db.table("leads")
            .select("*")
            .or_("enriched_phone_number.is.null,enriched_phone_number.eq.")
            .order("source_observed_at", desc=True)
            .order("id")
            .limit(limit)
            .execute()
        )
        return await self._attach_replies(response.data)

    async def _attach_replies(
        self, leads: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not leads:
            return []
        lead_ids = [str(item["id"]) for item in leads]
        conversations_response = await (
            self._db.table("smartlead_conversations")
            .select("id,lead_id")
            .in_("lead_id", lead_ids)
            .execute()
        )
        conversation_to_lead = {
            str(item["id"]): str(item["lead_id"])
            for item in conversations_response.data
        }
        replies: list[dict[str, Any]] = []
        if conversation_to_lead:
            replies_response = await (
                self._db.table("smartlead_replies")
                .select("id,conversation_id,body,received_at")
                .in_("conversation_id", list(conversation_to_lead))
                .order("received_at", desc=True)
                .execute()
            )
            replies = replies_response.data

        replies_by_lead: dict[str, list[dict[str, Any]]] = {}
        for reply in replies:
            lead_id = conversation_to_lead.get(str(reply["conversation_id"]))
            if lead_id is not None:
                replies_by_lead.setdefault(lead_id, []).append(reply)
        for lead in leads:
            lead["inbound_replies"] = replies_by_lead.get(str(lead["id"]), [])
        return leads

    async def create_item(
        self, run_id: str, lead_id: str, *, status: str = "running"
    ) -> dict[str, Any]:
        values: dict[str, Any] = {
            "run_id": run_id,
            "lead_id": lead_id,
            "status": status,
        }
        if status not in {"running", "waiting"}:
            values["completed_at"] = to_iso(utc_now())
        try:
            response = await (
                self._db.table("phone_enrichment_items").insert(values).execute()
            )
        except APIError as exc:
            if exc.code == "23505" and status == "running":
                raise ActiveLeadEnrichmentError from exc
            raise
        return response.data[0]

    async def update_item(
        self, item_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        response = await (
            self._db.table("phone_enrichment_items")
            .update({**values, "updated_at": to_iso(utc_now())})
            .eq("id", item_id)
            .execute()
        )
        return response.data[0]

    async def get_item(self, item_id: str) -> dict[str, Any] | None:
        response = await (
            self._db.table("phone_enrichment_items")
            .select("*")
            .eq("id", item_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def create_attempt(
        self,
        *,
        run_id: str,
        item_id: str,
        lead_id: str,
        provider: str,
        sequence: int,
        status: str,
        request_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await (
            self._db.table("phone_enrichment_attempts")
            .insert(
                {
                    "run_id": run_id,
                    "item_id": item_id,
                    "lead_id": lead_id,
                    "provider": provider,
                    "sequence": sequence,
                    "status": status,
                    "request_payload": request_payload or {},
                }
            )
            .execute()
        )
        return response.data[0]

    async def update_attempt(
        self, attempt_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        response = await (
            self._db.table("phone_enrichment_attempts")
            .update({**values, "updated_at": to_iso(utc_now())})
            .eq("id", attempt_id)
            .execute()
        )
        return response.data[0]

    async def get_attempts_by_external_id(
        self, external_request_id: str
    ) -> list[dict[str, Any]]:
        response = await (
            self._db.table("phone_enrichment_attempts")
            .select("*")
            .eq("provider", "fullenrich")
            .eq("external_request_id", external_request_id)
            .order("created_at")
            .execute()
        )
        return response.data

    async def update_empty_lead_phone(
        self, lead_id: str, phone: str, source: str
    ) -> bool:
        response = await (
            self._db.table("leads")
            .update(
                {
                    "enriched_phone_number": phone,
                    "phone_source": source,
                    "updated_at": to_iso(utc_now()),
                }
            )
            .eq("id", lead_id)
            .or_("enriched_phone_number.is.null,enriched_phone_number.eq.")
            .execute()
        )
        return bool(response.data)

    async def get_run_detail(self, run_id: str) -> dict[str, Any] | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        items_response = await (
            self._db.table("phone_enrichment_items")
            .select("*")
            .eq("run_id", run_id)
            .order("created_at")
            .order("id")
            .execute()
        )
        attempts_response = await (
            self._db.table("phone_enrichment_attempts")
            .select("*")
            .eq("run_id", run_id)
            .order("sequence")
            .order("created_at")
            .execute()
        )
        attempts_by_item: dict[str, list[dict[str, Any]]] = {}
        for attempt in attempts_response.data:
            attempts_by_item.setdefault(str(attempt["item_id"]), []).append(attempt)
        for item in items_response.data:
            item["attempts"] = attempts_by_item.get(str(item["id"]), [])
        run["items"] = items_response.data
        return run

    async def list_items(self, run_id: str) -> list[dict[str, Any]]:
        response = await (
            self._db.table("phone_enrichment_items")
            .select("*")
            .eq("run_id", run_id)
            .execute()
        )
        return response.data

    async def acquire_reconciliation(
        self, run_id: str, minimum_seconds: int
    ) -> bool:
        run = await self.get_run(run_id)
        if run is None:
            return False
        last_value = run.get("last_reconciled_at")
        if last_value is not None:
            if parse_datetime(str(last_value)) > utc_now() - timedelta(
                seconds=minimum_seconds
            ):
                return False
        now = to_iso(utc_now())
        query = self._db.table("phone_enrichment_runs").update(
            {"last_reconciled_at": now, "updated_at": now}
        ).eq("id", run_id)
        if last_value is None:
            query = query.is_("last_reconciled_at", "null")
        else:
            query = query.eq("last_reconciled_at", last_value)
        response = await query.execute()
        return bool(response.data)
