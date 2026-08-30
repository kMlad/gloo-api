from datetime import timedelta
from typing import Any

from postgrest.exceptions import APIError

from app.utils import chunks, merge_non_empty, parse_datetime, to_iso, utc_now
from supabase import AsyncClient


class ConcurrentImportError(Exception):
    pass


class Repository:
    def __init__(self, supabase: AsyncClient) -> None:
        self._db = supabase

    async def list_campaigns(
        self, *, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        query = self._db.table("smartlead_campaigns").select("*")
        if enabled_only:
            query = query.eq("enabled", True)
        response = await query.order("smartlead_campaign_id").execute()
        return response.data

    async def get_campaigns_by_ids(
        self, campaign_ids: list[int]
    ) -> list[dict[str, Any]]:
        if not campaign_ids:
            return []
        response = await (
            self._db.table("smartlead_campaigns")
            .select("*")
            .in_("smartlead_campaign_id", campaign_ids)
            .order("smartlead_campaign_id")
            .execute()
        )
        return response.data

    async def upsert_campaign(
        self, campaign_id: int, name: str, enabled: bool, reply_types: list[str]
    ) -> dict[str, Any]:
        now = to_iso(utc_now())
        response = await (
            self._db.table("smartlead_campaigns")
            .upsert(
                {
                    "smartlead_campaign_id": campaign_id,
                    "name": name,
                    "enabled": enabled,
                    "reply_types": reply_types,
                    "updated_at": now,
                },
                on_conflict="smartlead_campaign_id",
            )
            .execute()
        )
        return response.data[0]

    async def update_campaign(
        self,
        campaign_id: int,
        *,
        enabled: bool | None,
        reply_types: list[str] | None,
    ) -> dict[str, Any] | None:
        values: dict[str, Any] = {"updated_at": to_iso(utc_now())}
        if enabled is not None:
            values["enabled"] = enabled
        if reply_types is not None:
            values["reply_types"] = reply_types
        response = await (
            self._db.table("smartlead_campaigns")
            .update(values)
            .eq("smartlead_campaign_id", campaign_id)
            .execute()
        )
        return response.data[0] if response.data else None

    async def expire_stale_imports(self) -> None:
        now = utc_now()
        cutoff = now - timedelta(hours=2)
        await (
            self._db.table("smartlead_import_runs")
            .update(
                {
                    "status": "failed",
                    "errors": [
                        {
                            "scope": "import",
                            "message": "Import did not complete before the stale-run timeout",
                        }
                    ],
                    "completed_at": to_iso(now),
                    "updated_at": to_iso(now),
                }
            )
            .eq("status", "running")
            .lt("started_at", to_iso(cutoff))
            .execute()
        )

    async def create_import_run(
        self,
        *,
        campaign_ids: list[int],
        reply_time_from: str | None,
        reply_time_to: str | None,
        max_conversations: int,
    ) -> dict[str, Any]:
        await self.expire_stale_imports()
        try:
            response = await (
                self._db.table("smartlead_import_runs")
                .insert(
                    {
                        "status": "running",
                        "campaign_ids": campaign_ids,
                        "reply_time_from": reply_time_from,
                        "reply_time_to": reply_time_to,
                        "max_conversations": max_conversations,
                    }
                )
                .execute()
            )
        except APIError as exc:
            if exc.code == "23505":
                raise ConcurrentImportError from exc
            raise
        return response.data[0]

    async def update_import_run(
        self, run_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        values = {**values, "updated_at": to_iso(utc_now())}
        response = await (
            self._db.table("smartlead_import_runs")
            .update(values)
            .eq("id", run_id)
            .execute()
        )
        return response.data[0]

    async def get_import_run(self, run_id: str) -> dict[str, Any] | None:
        response = await (
            self._db.table("smartlead_import_runs")
            .select("*")
            .eq("id", run_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def upsert_lead(
        self,
        *,
        email: str,
        email_normalized: str,
        observed_at: str,
        typed_properties: dict[str, Any],
        properties: dict[str, Any],
        custom_properties: dict[str, Any],
    ) -> dict[str, Any]:
        existing_response = await (
            self._db.table("leads")
            .select("*")
            .eq("email_normalized", email_normalized)
            .limit(1)
            .execute()
        )
        existing = existing_response.data[0] if existing_response.data else None
        now = to_iso(utc_now())

        if existing is None:
            payload = {
                "email": email,
                "email_normalized": email_normalized,
                **typed_properties,
                "properties": properties,
                "custom_properties": custom_properties,
                "source_observed_at": observed_at,
                "updated_at": now,
            }
            response = await self._db.table("leads").insert(payload).execute()
            return response.data[0]

        incoming_is_newer = parse_datetime(observed_at) >= parse_datetime(
            existing["source_observed_at"]
        )
        if incoming_is_newer:
            merged_properties = merge_non_empty(existing["properties"], properties)
            merged_custom = merge_non_empty(
                existing["custom_properties"], custom_properties
            )
            typed_update = {
                key: value
                for key, value in typed_properties.items()
                if value is not None and value != ""
            }
            email_update = email
            source_observed_at = observed_at
        else:
            merged_properties = merge_non_empty(properties, existing["properties"])
            merged_custom = merge_non_empty(
                custom_properties, existing["custom_properties"]
            )
            typed_update = {}
            email_update = existing["email"]
            source_observed_at = existing["source_observed_at"]

        response = await (
            self._db.table("leads")
            .update(
                {
                    "email": email_update,
                    **typed_update,
                    "properties": merged_properties,
                    "custom_properties": merged_custom,
                    "source_observed_at": source_observed_at,
                    "updated_at": now,
                }
            )
            .eq("id", existing["id"])
            .execute()
        )
        return response.data[0]

    async def upsert_conversation(self, values: dict[str, Any]) -> dict[str, Any]:
        response = await (
            self._db.table("smartlead_conversations")
            .upsert(
                {**values, "updated_at": to_iso(utc_now())},
                on_conflict="smartlead_campaign_id,smartlead_campaign_lead_map_id",
            )
            .execute()
        )
        return response.data[0]

    async def upsert_lead_conversation(
        self,
        *,
        email: str,
        email_normalized: str,
        observed_at: str,
        typed_properties: dict[str, Any],
        properties: dict[str, Any],
        custom_properties: dict[str, Any],
        conversation: dict[str, Any],
    ) -> dict[str, Any]:
        """Upsert a SmartLead lead and its conversation in one transaction."""
        existing_response = await (
            self._db.table("leads")
            .select("*")
            .eq("email_normalized", email_normalized)
            .limit(1)
            .execute()
        )
        existing = existing_response.data[0] if existing_response.data else None

        if existing is None:
            lead_values = {
                "email": email,
                "email_normalized": email_normalized,
                **typed_properties,
                "properties": properties,
                "custom_properties": custom_properties,
                "source_observed_at": observed_at,
            }
        else:
            incoming_is_newer = parse_datetime(observed_at) >= parse_datetime(
                existing["source_observed_at"]
            )
            if incoming_is_newer:
                lead_values = {
                    **existing,
                    "email": email,
                    **{
                        key: value
                        for key, value in typed_properties.items()
                        if value is not None and value != ""
                    },
                    "properties": merge_non_empty(existing["properties"], properties),
                    "custom_properties": merge_non_empty(
                        existing["custom_properties"], custom_properties
                    ),
                    "source_observed_at": observed_at,
                }
            else:
                lead_values = {
                    **existing,
                    "properties": merge_non_empty(properties, existing["properties"]),
                    "custom_properties": merge_non_empty(
                        custom_properties, existing["custom_properties"]
                    ),
                }

        response = await self._db.rpc(
            "upsert_smartlead_lead_conversation",
            {
                "p_lead": lead_values,
                "p_conversation": conversation,
            },
        ).execute()
        return response.data

    async def upsert_reply(self, values: dict[str, Any]) -> dict[str, Any]:
        response = await (
            self._db.table("smartlead_replies")
            .upsert(
                {**values, "updated_at": to_iso(utc_now())},
                on_conflict="dedupe_key",
            )
            .execute()
        )
        return response.data[0]

    async def list_leads(
        self, *, limit: int, offset: int, reply_type: str | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        selection = "*"
        if reply_type is not None:
            selection += ",smartlead_conversations!inner(reply_type)"
        query = self._db.table("leads").select(selection, count="exact")
        if reply_type is not None:
            query = query.eq("smartlead_conversations.reply_type", reply_type)
        response = await (
            query.order("source_observed_at", desc=True)
            .order("id")
            .range(offset, offset + limit - 1)
            .execute()
        )
        leads = response.data
        for lead in leads:
            lead.pop("smartlead_conversations", None)
        if not leads:
            return [], response.count or 0

        lead_ids = [lead["id"] for lead in leads]
        conversations_response = await (
            self._db.table("smartlead_conversations")
            .select("id,lead_id,reply_type")
            .in_("lead_id", lead_ids)
            .execute()
        )
        conversations = conversations_response.data
        conversation_ids = [item["id"] for item in conversations]
        replies: list[dict[str, Any]] = []
        if conversation_ids:
            replies_response = await (
                self._db.table("smartlead_replies")
                .select("conversation_id,received_at")
                .in_("conversation_id", conversation_ids)
                .execute()
            )
            replies = replies_response.data

        conversations_by_lead: dict[str, list[str]] = {}
        for conversation in conversations:
            conversations_by_lead.setdefault(conversation["lead_id"], []).append(
                conversation["id"]
            )
        latest_by_conversation: dict[str, str] = {}
        for reply in replies:
            current = latest_by_conversation.get(reply["conversation_id"])
            if current is None or reply["received_at"] > current:
                latest_by_conversation[reply["conversation_id"]] = reply["received_at"]

        for lead in leads:
            ids = conversations_by_lead.get(lead["id"], [])
            timestamps = [
                latest_by_conversation[item]
                for item in ids
                if item in latest_by_conversation
            ]
            lead_conversations = [
                item for item in conversations if item["lead_id"] == lead["id"]
            ]
            lead["positive_conversation_count"] = sum(
                item.get("reply_type") == "positive" for item in lead_conversations
            )
            lead["ooo_conversation_count"] = sum(
                item.get("reply_type") == "ooo" for item in lead_conversations
            )
            lead["latest_reply_at"] = max(timestamps) if timestamps else None
        return leads, response.count or len(leads)

    async def clear_unmatched_conversation_reply_types(
        self, campaign_id: int, active_map_ids: set[str]
    ) -> int:
        response = await (
            self._db.table("smartlead_conversations")
            .select("id,smartlead_campaign_lead_map_id,reply_type")
            .eq("smartlead_campaign_id", campaign_id)
            .execute()
        )
        stale_ids = [
            str(item["id"])
            for item in response.data
            if item.get("reply_type") is not None
            and str(item["smartlead_campaign_lead_map_id"]) not in active_map_ids
        ]
        now = to_iso(utc_now())
        for stale_group in chunks(stale_ids, 100):
            await (
                self._db.table("smartlead_conversations")
                .update({"reply_type": None, "updated_at": now})
                .in_("id", stale_group)
                .execute()
            )
        return len(stale_ids)

    async def get_lead_detail(self, lead_id: str) -> dict[str, Any] | None:
        lead_response = await (
            self._db.table("leads").select("*").eq("id", lead_id).limit(1).execute()
        )
        if not lead_response.data:
            return None

        conversations_response = await (
            self._db.table("smartlead_conversations")
            .select("*")
            .eq("lead_id", lead_id)
            .order("qualified_at", desc=True)
            .execute()
        )
        conversations = conversations_response.data
        conversation_ids = [item["id"] for item in conversations]
        replies: list[dict[str, Any]] = []
        if conversation_ids:
            replies_response = await (
                self._db.table("smartlead_replies")
                .select("*")
                .in_("conversation_id", conversation_ids)
                .order("received_at")
                .execute()
            )
            replies = replies_response.data

        replies_by_conversation: dict[str, list[dict[str, Any]]] = {}
        for reply in replies:
            replies_by_conversation.setdefault(reply["conversation_id"], []).append(
                reply
            )
        for conversation in conversations:
            conversation["replies"] = replies_by_conversation.get(
                conversation["id"], []
            )

        return {"lead": lead_response.data[0], "conversations": conversations}

    async def mark_chat_refreshed(self, lead_id: str) -> None:
        now = to_iso(utc_now())
        await (
            self._db.table("leads")
            .update({"chat_refreshed_at": now, "updated_at": now})
            .eq("id", lead_id)
            .execute()
        )
