from datetime import timedelta
from typing import Any

from postgrest.exceptions import APIError

from app.repositories import ConcurrentImportError
from app.utils import to_iso, utc_now
from supabase import AsyncClient


class HeyReachRepository:
    def __init__(self, supabase: AsyncClient) -> None:
        self._db = supabase

    async def list_campaigns(
        self, *, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        query = self._db.table("heyreach_campaigns").select("*")
        if enabled_only:
            query = query.eq("enabled", True)
        response = await query.order("heyreach_campaign_id").execute()
        return response.data

    async def sync_campaign_catalog(
        self, campaigns: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        remote_campaigns = [item for item in campaigns if item.get("id") is not None]
        if not remote_campaigns:
            return []
        now = to_iso(utc_now())
        campaign_ids = [int(item["id"]) for item in remote_campaigns]
        existing_response = await (
            self._db.table("heyreach_campaigns")
            .select("heyreach_campaign_id,enabled,created_at")
            .in_("heyreach_campaign_id", campaign_ids)
            .execute()
        )
        existing_by_id = {
            int(item["heyreach_campaign_id"]): item
            for item in existing_response.data
        }
        values = [
            {
                "heyreach_campaign_id": int(campaign["id"]),
                "name": str(
                    campaign.get("name") or f"HeyReach campaign {campaign['id']}"
                ),
                "status": (
                    str(campaign["status"])
                    if campaign.get("status") is not None
                    else None
                ),
                "last_synced_at": now,
                "enabled": existing_by_id.get(int(campaign["id"]), {}).get(
                    "enabled", True
                ),
                "created_at": existing_by_id.get(int(campaign["id"]), {}).get(
                    "created_at", now
                ),
                "updated_at": now,
            }
            for campaign in remote_campaigns
        ]
        response = await (
            self._db.table("heyreach_campaigns")
            .upsert(values, on_conflict="heyreach_campaign_id")
            .execute()
        )
        return response.data

    async def get_campaign_import_stats(
        self, campaign_ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        if not campaign_ids:
            return {}
        conversations_response = await (
            self._db.table("heyreach_conversations")
            .select("lead_id,heyreach_campaign_id")
            .in_("heyreach_campaign_id", campaign_ids)
            .execute()
        )
        latest_response = await self._db.rpc(
            "latest_heyreach_imports", {"p_campaign_ids": campaign_ids}
        ).execute()
        stats: dict[int, dict[str, Any]] = {}
        lead_sets: dict[int, set[str]] = {}
        for conversation in conversations_response.data:
            campaign_id = int(conversation["heyreach_campaign_id"])
            lead_sets.setdefault(campaign_id, set()).add(str(conversation["lead_id"]))
        last_imports: dict[int, dict[str, Any] | None] = {}
        for campaign_id in campaign_ids:
            last_imports[campaign_id] = None
            stats[campaign_id] = {
                "ever_imported": bool(lead_sets.get(campaign_id)),
                "imported_lead_count": len(lead_sets.get(campaign_id, set())),
                "last_imported_at": None,
                "last_import_run_id": None,
                "last_import": None,
            }
        run_ids: list[str] = []
        latest_rows: list[tuple[int, dict[str, Any]]] = []
        for item in latest_response.data:
            run = item.get("run")
            if not isinstance(run, dict) or run.get("id") is None:
                continue
            campaign_id = int(item["heyreach_campaign_id"])
            latest_rows.append((campaign_id, run))
            run_ids.append(str(run["id"]))
        enrichments = await self.get_latest_phone_enrichments_by_import_run(run_ids)
        for campaign_id, run in latest_rows:
            snapshot = self._campaign_last_import(
                run, enrichments.get(str(run["id"]))
            )
            last_imports[campaign_id] = snapshot
            stats[campaign_id]["last_import"] = snapshot
            stats[campaign_id]["last_import_run_id"] = snapshot["id"]
            stats[campaign_id]["last_imported_at"] = (
                snapshot.get("completed_at") or snapshot.get("started_at")
            )
        return stats

    @staticmethod
    def _campaign_last_import(
        run: dict[str, Any], enrichment: dict[str, Any] | None
    ) -> dict[str, Any]:
        return {
            "id": run["id"],
            "status": run.get("status"),
            "campaign_ids": run.get("campaign_ids") or [],
            "reply_types": ["positive"],
            "leads_processed": run.get("leads_processed") or 0,
            "conversations_processed": run.get("conversations_processed") or 0,
            "qualifying_conversation_count": run.get("qualifying_conversation_count")
            or 0,
            "errors": run.get("errors") or [],
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
            "last_enrichment": enrichment,
        }

    async def get_latest_phone_enrichments_by_import_run(
        self, import_run_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        unique_ids = list(dict.fromkeys(import_run_ids))
        if not unique_ids:
            return {}
        response = await (
            self._db.table("phone_enrichment_runs")
            .select("*")
            .in_("source_import_run_id", unique_ids)
            .order("created_at", desc=True)
            .order("id")
            .execute()
        )
        latest: dict[str, dict[str, Any]] = {}
        for row in response.data:
            source_id = row.get("source_import_run_id")
            if source_id is None:
                continue
            key = str(source_id)
            if key not in latest:
                latest[key] = row
        return latest

    async def attach_last_enrichments(
        self, runs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        enrichments = await self.get_latest_phone_enrichments_by_import_run(
            [str(run["id"]) for run in runs if run.get("id") is not None]
        )
        for run in runs:
            run["last_enrichment"] = enrichments.get(str(run["id"]))
        return runs

    async def get_campaigns_by_ids(
        self, campaign_ids: list[int]
    ) -> list[dict[str, Any]]:
        if not campaign_ids:
            return []
        response = await (
            self._db.table("heyreach_campaigns")
            .select("*")
            .in_("heyreach_campaign_id", campaign_ids)
            .order("heyreach_campaign_id")
            .execute()
        )
        return response.data

    async def upsert_campaign(
        self, campaign_id: int, name: str, enabled: bool
    ) -> dict[str, Any]:
        now = to_iso(utc_now())
        response = await (
            self._db.table("heyreach_campaigns")
            .upsert(
                {
                    "heyreach_campaign_id": campaign_id,
                    "name": name,
                    "enabled": enabled,
                    "updated_at": now,
                },
                on_conflict="heyreach_campaign_id",
            )
            .execute()
        )
        return response.data[0]

    async def update_campaign(
        self, campaign_id: int, *, enabled: bool | None
    ) -> dict[str, Any] | None:
        values: dict[str, Any] = {"updated_at": to_iso(utc_now())}
        if enabled is not None:
            values["enabled"] = enabled
        response = await (
            self._db.table("heyreach_campaigns")
            .update(values)
            .eq("heyreach_campaign_id", campaign_id)
            .execute()
        )
        return response.data[0] if response.data else None

    async def expire_stale_imports(self) -> None:
        now = utc_now()
        cutoff = now - timedelta(hours=2)
        await (
            self._db.table("heyreach_import_runs")
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
            .in_("status", ["queued", "running"])
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
        requested_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        await self.expire_stale_imports()
        try:
            response = await (
                self._db.table("heyreach_import_runs")
                .insert(
                    {
                        "status": "queued",
                        "campaign_ids": campaign_ids,
                        "reply_time_from": reply_time_from,
                        "reply_time_to": reply_time_to,
                        "max_conversations": max_conversations,
                        "requested_by": requested_by,
                        "idempotency_key": idempotency_key,
                    }
                )
                .execute()
            )
        except APIError as exc:
            if exc.code == "23505":
                raise ConcurrentImportError from exc
            raise
        return response.data[0]

    async def get_import_run_by_idempotency_key(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        response = await (
            self._db.table("heyreach_import_runs")
            .select("*")
            .eq("idempotency_key", idempotency_key)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def list_import_runs(
        self, *, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        response = await (
            self._db.table("heyreach_import_runs")
            .select("*", count="exact")
            .order("created_at", desc=True)
            .order("id")
            .range(offset, offset + limit - 1)
            .execute()
        )
        return response.data, response.count or len(response.data)

    async def upsert_import_run_item(
        self,
        *,
        run_id: str,
        lead_id: str,
        conversation_id: str,
        campaign_id: int,
    ) -> dict[str, Any]:
        response = await (
            self._db.table("heyreach_import_run_items")
            .upsert(
                {
                    "run_id": run_id,
                    "lead_id": lead_id,
                    "conversation_id": conversation_id,
                    "heyreach_campaign_id": campaign_id,
                },
                on_conflict="run_id,conversation_id",
            )
            .execute()
        )
        return response.data[0]

    async def get_import_run_lead_ids(self, run_id: str) -> list[str]:
        response = await (
            self._db.table("heyreach_import_run_items")
            .select("lead_id")
            .eq("run_id", run_id)
            .execute()
        )
        return list(dict.fromkeys(str(item["lead_id"]) for item in response.data))

    async def update_import_run(
        self, run_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        values = {**values, "updated_at": to_iso(utc_now())}
        response = await (
            self._db.table("heyreach_import_runs")
            .update(values)
            .eq("id", run_id)
            .execute()
        )
        return response.data[0]

    async def claim_import_run(self, run_id: str) -> dict[str, Any] | None:
        response = await (
            self._db.table("heyreach_import_runs")
            .update({"status": "running", "updated_at": to_iso(utc_now())})
            .eq("id", run_id)
            .eq("status", "queued")
            .execute()
        )
        return response.data[0] if response.data else None

    async def get_import_run(self, run_id: str) -> dict[str, Any] | None:
        response = await (
            self._db.table("heyreach_import_runs")
            .select("*")
            .eq("id", run_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def upsert_lead_conversation(
        self,
        *,
        observed_at: str,
        typed_properties: dict[str, Any],
        properties: dict[str, Any],
        custom_properties: dict[str, Any],
        conversation: dict[str, Any],
        email: str | None = None,
        email_normalized: str | None = None,
    ) -> dict[str, Any]:
        lead_values = {
            "email": email,
            "email_normalized": email_normalized,
            **typed_properties,
            "properties": properties,
            "custom_properties": custom_properties,
            "source_observed_at": observed_at,
        }
        response = await self._db.rpc(
            "upsert_heyreach_lead_conversation",
            {
                "p_lead": lead_values,
                "p_conversation": conversation,
            },
        ).execute()
        return response.data

    async def upsert_reply(self, values: dict[str, Any]) -> dict[str, Any]:
        response = await (
            self._db.table("heyreach_replies")
            .upsert(
                {**values, "updated_at": to_iso(utc_now())},
                on_conflict="dedupe_key",
            )
            .execute()
        )
        return response.data[0]

    async def get_conversations_for_lead(
        self, lead_id: str
    ) -> list[dict[str, Any]]:
        conversations_response = await (
            self._db.table("heyreach_conversations")
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
                self._db.table("heyreach_replies")
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
        return conversations

    async def conversations_for_leads(
        self, lead_ids: list[str]
    ) -> list[dict[str, Any]]:
        if not lead_ids:
            return []
        response = await (
            self._db.table("heyreach_conversations")
            .select(
                "id,lead_id,heyreach_campaign_id,reply_type,qualified_at"
            )
            .in_("lead_id", lead_ids)
            .execute()
        )
        return response.data

    async def reply_times_for_conversations(
        self, conversation_ids: list[str]
    ) -> list[dict[str, Any]]:
        if not conversation_ids:
            return []
        response = await (
            self._db.table("heyreach_replies")
            .select("conversation_id,received_at")
            .in_("conversation_id", conversation_ids)
            .execute()
        )
        return response.data
