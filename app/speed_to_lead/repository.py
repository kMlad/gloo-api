from typing import Any

from postgrest.exceptions import APIError

from app.utils import to_iso, utc_now
from supabase import AsyncClient, AuthApiError, AuthError


class SpeedToLeadRepository:
    def __init__(self, supabase: AsyncClient) -> None:
        self._db = supabase

    async def get_smartlead_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        response = await (
            self._db.table("smartlead_campaigns")
            .select("*")
            .eq("smartlead_campaign_id", campaign_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def update_smartlead_campaign(
        self,
        campaign_id: int,
        *,
        enabled: bool,
        sdr_id: str | None,
        webhook_id: str | None,
    ) -> dict[str, Any] | None:
        response = await (
            self._db.table("smartlead_campaigns")
            .update(
                {
                    "speed_to_lead_enabled": enabled,
                    "speed_to_lead_sdr_id": sdr_id,
                    "smartlead_webhook_id": webhook_id,
                    "updated_at": to_iso(utc_now()),
                }
            )
            .eq("smartlead_campaign_id", campaign_id)
            .execute()
        )
        return response.data[0] if response.data else None

    async def find_smartlead_conversation(
        self, *, campaign_id: int, email_normalized: str
    ) -> dict[str, Any] | None:
        """Return the stored conversation for a lead email in a campaign, if any."""
        lead_response = await (
            self._db.table("leads")
            .select("id")
            .eq("email_normalized", email_normalized)
            .limit(1)
            .execute()
        )
        if not lead_response.data:
            return None
        response = await (
            self._db.table("smartlead_conversations")
            .select("*")
            .eq("lead_id", lead_response.data[0]["id"])
            .eq("smartlead_campaign_id", campaign_id)
            .order("qualified_at", desc=True)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def get_event_by_dedupe_key(self, dedupe_key: str) -> dict[str, Any] | None:
        response = await (
            self._db.table("speed_to_lead_events")
            .select("*")
            .eq("dedupe_key", dedupe_key)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def insert_event(self, values: dict[str, Any]) -> dict[str, Any] | None:
        """Insert an event; return None when the dedupe key already exists."""
        try:
            response = await (
                self._db.table("speed_to_lead_events").insert(values).execute()
            )
        except APIError as exc:
            if exc.code == "23505":
                return None
            raise
        return response.data[0]

    async def get_event_by_enrichment_run(
        self, enrichment_run_id: str
    ) -> dict[str, Any] | None:
        response = await (
            self._db.table("speed_to_lead_events")
            .select("*")
            .eq("enrichment_run_id", enrichment_run_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def get_user_email(self, user_id: str) -> str | None:
        try:
            response = await self._db.auth.admin.get_user_by_id(user_id)
        except (AuthApiError, AuthError):
            return None
        user = response.user if response is not None else None
        return user.email if user is not None else None

    async def list_events(
        self,
        *,
        limit: int,
        offset: int,
        include_handled: bool = False,
        visible_to_sdr_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return events newest first with the embedded lead row under ``lead``.

        Unhandled is the SDR workflow status ``new``, regardless of phone
        availability or enrichment progress. Keep these filters on the inner
        join so visibility, counts, and pagination use the same lead rows.
        """
        query = self._db.table("speed_to_lead_events").select(
            "*,leads!inner(*)", count="exact"
        )
        if not include_handled:
            query = query.eq("leads.status", "new")
        if visible_to_sdr_id is not None:
            query = query.eq("leads.assigned_sdr_id", visible_to_sdr_id)
        response = await (
            query.order("replied_at", desc=True)
            .order("id")
            .range(offset, offset + limit - 1)
            .execute()
        )
        events = []
        for row in response.data:
            lead = row.pop("leads", None)
            events.append({**row, "lead": lead if isinstance(lead, dict) else None})
        return events, response.count or len(events)

    async def get_enrichment_runs(
        self, run_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        unique_ids = list(dict.fromkeys(run_ids))
        if not unique_ids:
            return {}
        response = await (
            self._db.table("phone_enrichment_runs")
            .select("*")
            .in_("id", unique_ids)
            .execute()
        )
        return {str(row["id"]): row for row in response.data}

    async def update_event(
        self, event_id: str, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        response = await (
            self._db.table("speed_to_lead_events")
            .update({**values, "updated_at": to_iso(utc_now())})
            .eq("id", event_id)
            .execute()
        )
        return response.data[0] if response.data else None

    async def assign_lead_if_unassigned(
        self, lead_id: str, *, sdr_id: str
    ) -> bool:
        now = to_iso(utc_now())
        response = await (
            self._db.table("leads")
            .update(
                {
                    "assigned_sdr_id": sdr_id,
                    "assigned_by": None,
                    "assigned_at": now,
                    "updated_at": now,
                }
            )
            .eq("id", lead_id)
            .is_("assigned_sdr_id", "null")
            .select("id")
            .execute()
        )
        return bool(response.data)

    async def get_heyreach_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        response = await (
            self._db.table("heyreach_campaigns")
            .select("*")
            .eq("heyreach_campaign_id", campaign_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def update_heyreach_campaign(
        self,
        campaign_id: int,
        *,
        enabled: bool,
        sdr_id: str | None,
        webhook_id: str | None,
    ) -> dict[str, Any] | None:
        response = await (
            self._db.table("heyreach_campaigns")
            .update(
                {
                    "speed_to_lead_enabled": enabled,
                    "speed_to_lead_sdr_id": sdr_id,
                    "heyreach_webhook_id": webhook_id,
                    "updated_at": to_iso(utc_now()),
                }
            )
            .eq("heyreach_campaign_id", campaign_id)
            .execute()
        )
        return response.data[0] if response.data else None
