from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from app.models import ImportRequest, ReplyType
from app.repositories import ConcurrentImportError, Repository
from app.smartlead.client import SmartLeadClient, SmartLeadError
from app.utils import (
    chunks,
    first_present,
    merge_non_empty,
    normalize_email,
    parse_datetime,
    to_iso,
    utc_now,
)

logger = logging.getLogger(__name__)


class ImportValidationError(Exception):
    pass


class ImportLimitExceeded(Exception):
    def __init__(self, run: dict[str, Any]) -> None:
        super().__init__("The import exceeds the configured conversation limit")
        self.run = run


def _message_direction(message: dict[str, Any]) -> str:
    direction = str(message.get("direction", "")).casefold()
    if direction in {"inbound", "outbound"}:
        return direction
    message_type = str(message.get("type", "")).casefold()
    if message_type in {"reply", "inbound"}:
        return "inbound"
    if message_type in {"sent", "outbound"}:
        return "outbound"
    return "inbound"


def _message_body(message: dict[str, Any]) -> str:
    return str(
        message.get("body")
        or message.get("email_body")
        or message.get("reply_body")
        or message.get("plain_text")
        or message.get("text")
        or ""
    )


class LeadService:
    def __init__(
        self,
        repository: Repository,
        smartlead: SmartLeadClient,
        *,
        chat_refresh_ttl_seconds: int,
    ) -> None:
        self._repository = repository
        self._smartlead = smartlead
        self._ttl = timedelta(seconds=chat_refresh_ttl_seconds)

    async def get_detail(
        self, lead_id: str, *, assigned_sdr_id: str | None = None
    ) -> dict[str, Any] | None:
        detail = await self._repository.get_lead_detail(
            lead_id, assigned_sdr_id=assigned_sdr_id
        )
        if detail is None:
            return None
        if self._is_stale(detail["lead"].get("chat_refreshed_at")):
            await self._refresh_chat(lead_id, detail["conversations"])
            detail = await self._repository.get_lead_detail(
                lead_id, assigned_sdr_id=assigned_sdr_id
            )
        return detail

    def _is_stale(self, chat_refreshed_at: Any) -> bool:
        if self._ttl.total_seconds() == 0:
            return True
        if chat_refreshed_at in (None, ""):
            return True
        refreshed = parse_datetime(chat_refreshed_at)
        return utc_now() - refreshed >= self._ttl

    async def _refresh_chat(
        self, lead_id: str, conversations: list[dict[str, Any]]
    ) -> None:
        targets = [
            conversation
            for conversation in conversations
            if conversation.get("smartlead_campaign_id") is not None
            and conversation.get("smartlead_lead_id") not in (None, "")
        ]
        if not targets:
            logger.warning(
                "SmartLead chat refresh skipped because the lead has no usable "
                "conversation metadata",
                extra={"lead_id": lead_id},
            )
            return
        results = await asyncio.gather(
            *[self._fetch_and_store(conversation) for conversation in targets]
        )
        if not all(results):
            return
        await self._repository.mark_chat_refreshed(lead_id)

    async def _fetch_and_store(self, conversation: dict[str, Any]) -> bool:
        try:
            messages = await self._smartlead.get_lead_message_history(
                campaign_id=int(conversation["smartlead_campaign_id"]),
                lead_id=str(conversation["smartlead_lead_id"]),
            )
        except SmartLeadError:
            return False
        for message in messages:
            if not isinstance(message, dict):
                continue
            try:
                await self._upsert_message(str(conversation["id"]), message)
            except ValueError:
                continue
        return True

    async def _upsert_message(
        self, conversation_id: str, message: dict[str, Any]
    ) -> None:
        message_id = message.get("id") or message.get("message_id")
        received_at = ImportService._message_received_at(message)
        dedupe_key = ImportService._reply_dedupe_key(
            conversation_id=conversation_id,
            message=message,
            received_at=received_at,
        )
        await self._repository.upsert_reply(
            {
                "conversation_id": conversation_id,
                "smartlead_message_id": str(message_id) if message_id else None,
                "dedupe_key": dedupe_key,
                "subject": message.get("subject"),
                "body": _message_body(message),
                "sent_from": message.get("sent_from")
                or message.get("from_email")
                or message.get("from"),
                "sent_to": message.get("sent_to")
                or message.get("to_email")
                or message.get("to"),
                "received_at": to_iso(received_at),
                "direction": _message_direction(message),
                "message_properties": message,
            }
        )


class CampaignService:
    def __init__(
        self, repository: Repository, smartlead: SmartLeadClient | None = None
    ) -> None:
        self._repository = repository
        self._smartlead = smartlead

    async def list(self) -> list[dict[str, Any]]:
        if self._smartlead is not None:
            remote_campaigns = await self._smartlead.list_campaigns()
            await self._repository.sync_campaign_catalog(remote_campaigns)
        campaigns = await self._repository.list_campaigns()
        campaign_ids = [int(item["smartlead_campaign_id"]) for item in campaigns]
        stats = await self._repository.get_campaign_import_stats(campaign_ids)
        return [
            {
                **campaign,
                **stats.get(int(campaign["smartlead_campaign_id"]), {}),
            }
            for campaign in campaigns
        ]

    async def add(
        self, campaign_id: int, enabled: bool, reply_types: list[ReplyType]
    ) -> dict[str, Any]:
        if self._smartlead is None:
            raise RuntimeError("SmartLead client is required to add a campaign")
        campaign = await self._smartlead.get_campaign(campaign_id)
        name = str(campaign.get("name") or f"SmartLead campaign {campaign_id}")
        return await self._repository.upsert_campaign(
            campaign_id, name, enabled, reply_types
        )

    async def update(
        self,
        campaign_id: int,
        *,
        enabled: bool | None,
        reply_types: list[ReplyType] | None,
    ) -> dict[str, Any] | None:
        return await self._repository.update_campaign(
            campaign_id, enabled=enabled, reply_types=reply_types
        )


class ImportService:
    def __init__(
        self,
        repository: Repository,
        smartlead: SmartLeadClient,
        *,
        max_conversations: int,
    ) -> None:
        self._repository = repository
        self._smartlead = smartlead
        self._max_conversations = max_conversations

    async def start(
        self,
        request: ImportRequest,
        *,
        idempotency_key: str | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        campaigns = await self._resolve_campaigns(request.campaign_ids)
        campaign_ids = [item["smartlead_campaign_id"] for item in campaigns]
        reply_time_from = (
            to_iso(request.reply_time_from) if request.reply_time_from else None
        )
        reply_time_to = to_iso(request.reply_time_to) if request.reply_time_to else None

        if idempotency_key is not None:
            existing = await self._repository.get_import_run_by_idempotency_key(
                idempotency_key
            )
            if existing is not None:
                if not self._matches_request(existing, request, campaign_ids):
                    raise ImportValidationError(
                        "Idempotency-Key was already used for a different import"
                    )
                return existing

        try:
            return await self._repository.create_import_run(
                campaign_ids=campaign_ids,
                reply_types=list(request.reply_types),
                reply_time_from=reply_time_from,
                reply_time_to=reply_time_to,
                max_conversations=self._max_conversations,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
            )
        except ConcurrentImportError:
            # A duplicate idempotent request can race between the read above and
            # the insert. Resolve that race to the original run; preserve the
            # conflict for a genuinely different or concurrently active import.
            if idempotency_key is None:
                raise
            existing = await self._repository.get_import_run_by_idempotency_key(
                idempotency_key
            )
            if existing is None or not self._matches_request(
                existing, request, campaign_ids
            ):
                raise
            return existing

    async def execute(self, run_id: str) -> dict[str, Any]:
        run = await self._repository.get_import_run(run_id)
        if run is None:
            raise ImportValidationError("Import run not found")
        if run["status"] != "queued":
            return run
        claimed = await self._repository.claim_import_run(run_id)
        if claimed is None:
            current = await self._repository.get_import_run(run_id)
            if current is None:
                raise ImportValidationError("Import run not found")
            return current
        run = claimed
        request = ImportRequest(
            campaign_ids=[int(value) for value in run["campaign_ids"]],
            reply_types=list(run.get("reply_types") or ["positive"]),
            reply_time_from=(
                parse_datetime(run["reply_time_from"])
                if run.get("reply_time_from")
                else None
            ),
            reply_time_to=(
                parse_datetime(run["reply_time_to"])
                if run.get("reply_time_to")
                else None
            ),
        )
        campaigns = await self._resolve_campaigns(request.campaign_ids)
        return await self._execute(run_id, request, campaigns)

    async def execute_background(self, run_id: str) -> None:
        try:
            await self.execute(run_id)
        except (ImportValidationError, ImportLimitExceeded):
            return
        except Exception as exc:
            logger.exception("SmartLead import run %s failed", run_id)
            run = await self._repository.get_import_run(run_id)
            if run is not None and run["status"] in {"queued", "running"}:
                await self._finish_run(
                    run_id,
                    status="failed",
                    errors=[{"scope": "import", "message": str(exc)}],
                )

    async def run(self, request: ImportRequest) -> dict[str, Any]:
        run = await self.start(request)
        if run["status"] not in {"queued", "running"}:
            return run
        campaigns = await self._resolve_campaigns(request.campaign_ids)
        run_id = str(run["id"])
        await self._repository.update_import_run(run_id, {"status": "running"})
        return await self._execute(run_id, request, campaigns)

    async def _execute(
        self,
        run_id: str,
        request: ImportRequest,
        campaigns: list[dict[str, Any]],
    ) -> dict[str, Any]:
        campaign_ids = [item["smartlead_campaign_id"] for item in campaigns]

        try:
            categories = await self._smartlead.get_categories()
            category_ids_by_type = self._category_ids_by_reply_type(categories)
            required_reply_types = {
                reply_type
                for reply_type in request.reply_types
            }
            missing_reply_types = sorted(
                reply_type
                for reply_type in required_reply_types
                if not category_ids_by_type[reply_type]
            )
            if missing_reply_types:
                missing_labels = ", ".join(missing_reply_types)
                completed = await self._finish_run(
                    run_id,
                    status="rejected",
                    errors=[
                        {
                            "scope": "categories",
                            "message": (
                                "SmartLead has no categories for configured reply "
                                f"types: {missing_labels}"
                            ),
                        }
                    ],
                )
                raise ImportValidationError(
                    "SmartLead has no categories for configured reply types "
                    f"{missing_labels} (run {completed['id']})"
                )

            category_type_by_id = {
                category_id: reply_type
                for reply_type, category_ids in category_ids_by_type.items()
                for category_id in category_ids
            }
            await self._repository.update_import_run(
                run_id,
                {
                    "resolved_categories": {
                        reply_type: [
                            {
                                "id": category_id,
                                "name": next(
                                    (
                                        str(category.get("name") or "")
                                        for category in categories
                                        if int(category.get("id", -1)) == category_id
                                    ),
                                    "",
                                ),
                            }
                            for category_id in category_ids_by_type[reply_type]
                        ]
                        for reply_type in request.reply_types
                    }
                },
            )
            campaign_groups = self._group_campaigns_by_category_ids(
                campaigns, category_ids_by_type, request.reply_types
            )
            count = 0
            preflight_errors: list[dict[str, Any]] = []
            for grouped_campaign_ids, category_ids in campaign_groups:
                group_count, group_errors = await self._preflight(
                    grouped_campaign_ids, category_ids, request
                )
                count += group_count
                preflight_errors.extend(group_errors)
            await self._repository.update_import_run(
                run_id, {"qualifying_conversation_count": count}
            )
            if count > self._max_conversations:
                completed = await self._finish_run(
                    run_id,
                    status="rejected",
                    errors=[
                        *preflight_errors,
                        {
                            "scope": "limit",
                            "message": (
                                f"Import contains {count} conversations; the limit is "
                                f"{self._max_conversations}. Narrow the campaigns or reply dates."
                            ),
                        },
                    ],
                    qualifying_conversation_count=count,
                )
                raise ImportLimitExceeded(completed)

            detail_by_map: dict[tuple[int, str], dict[str, Any]] = {}
            detail_by_email: dict[tuple[int, str], dict[str, Any]] = {}
            detail_errors: list[dict[str, Any]] = []
            inbox_items: list[dict[str, Any]] = []
            inbox_errors: list[dict[str, Any]] = []
            for grouped_campaign_ids, category_ids in campaign_groups:
                (
                    group_by_map,
                    group_by_email,
                    group_detail_errors,
                ) = await self._fetch_campaign_lead_details(
                    grouped_campaign_ids, category_ids
                )
                group_items, group_inbox_errors = await self._fetch_inbox_items(
                    grouped_campaign_ids, category_ids, request
                )
                detail_by_map.update(group_by_map)
                detail_by_email.update(group_by_email)
                detail_errors.extend(group_detail_errors)
                inbox_items.extend(group_items)
                inbox_errors.extend(group_inbox_errors)
            errors = [*preflight_errors, *detail_errors, *inbox_errors]

            inbox_items.sort(key=self._item_observed_at)
            lead_ids: set[str] = set()
            active_map_ids_by_campaign: dict[int, set[str]] = {
                campaign_id: set() for campaign_id in campaign_ids
            }
            conversations_processed = 0
            replies_processed = 0

            for item in inbox_items:
                try:
                    result = await self._persist_item(
                        item,
                        detail_by_map,
                        detail_by_email,
                        category_type_by_id,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one conversation
                    errors.append(
                        {
                            "scope": "conversation",
                            "campaign_id": self._campaign_id(item),
                            "campaign_lead_map_id": self._campaign_lead_map_id(item),
                            "message": str(exc),
                        }
                    )
                    continue

                if result is None:
                    continue
                lead_ids.add(result["lead_id"])
                await self._repository.upsert_import_run_item(
                    run_id=run_id,
                    lead_id=result["lead_id"],
                    conversation_id=result["conversation_id"],
                    campaign_id=result["campaign_id"],
                    reply_type=result["reply_type"],
                )
                active_map_ids_by_campaign[result["campaign_id"]].add(
                    result["campaign_lead_map_id"]
                )
                conversations_processed += 1
                replies_processed += result["reply_count"]

            processed_any = conversations_processed > 0
            if errors and processed_any:
                status = "partial"
            elif errors:
                status = "failed"
            else:
                status = "succeeded"

            if (
                status == "succeeded"
                and request.reply_time_from is None
                and request.reply_time_to is None
            ):
                for campaign_id in campaign_ids:
                    await self._repository.clear_unmatched_conversation_reply_types(
                        campaign_id, active_map_ids_by_campaign[campaign_id]
                    )

            return await self._finish_run(
                run_id,
                status=status,
                errors=errors,
                qualifying_conversation_count=count,
                leads_processed=len(lead_ids),
                conversations_processed=conversations_processed,
                replies_processed=replies_processed,
            )
        except (ImportValidationError, ImportLimitExceeded):
            raise
        except SmartLeadError as exc:
            return await self._finish_run(
                run_id,
                status="failed",
                errors=[{"scope": "smartlead", "message": str(exc)}],
            )
        except Exception as exc:
            await self._finish_run(
                run_id,
                status="failed",
                errors=[{"scope": "import", "message": str(exc)}],
            )
            raise

    async def _resolve_campaigns(
        self, requested_ids: list[int] | None
    ) -> list[dict[str, Any]]:
        if requested_ids is None:
            campaigns = await self._repository.list_campaigns(enabled_only=True)
        else:
            campaigns = await self._repository.get_campaigns_by_ids(requested_ids)
            found = {item["smartlead_campaign_id"] for item in campaigns}
            missing = sorted(set(requested_ids) - found)
            if missing:
                remote_campaigns = []
                for campaign_id in missing:
                    campaign = await self._smartlead.get_campaign(campaign_id)
                    remote_campaigns.append({**campaign, "id": campaign_id})
                await self._repository.sync_campaign_catalog(remote_campaigns)
                campaigns = await self._repository.get_campaigns_by_ids(requested_ids)

        if not campaigns:
            raise ImportValidationError("No enabled SmartLead campaigns are configured")
        return campaigns

    @staticmethod
    def _matches_request(
        run: dict[str, Any], request: ImportRequest, campaign_ids: list[int]
    ) -> bool:
        stored_from = (
            parse_datetime(run["reply_time_from"])
            if run.get("reply_time_from")
            else None
        )
        stored_to = (
            parse_datetime(run["reply_time_to"]) if run.get("reply_time_to") else None
        )
        return (
            [int(value) for value in run.get("campaign_ids", [])] == campaign_ids
            and list(run.get("reply_types") or ["positive"])
            == list(request.reply_types)
            and stored_from == request.reply_time_from
            and stored_to == request.reply_time_to
        )

    @staticmethod
    def _category_ids_by_reply_type(
        categories: list[dict[str, Any]],
    ) -> dict[ReplyType, list[int]]:
        result: dict[ReplyType, set[int]] = {"positive": set(), "ooo": set()}
        for category in categories:
            category_id = category.get("id")
            if category_id is None:
                continue
            normalized_name = "".join(
                character
                for character in str(category.get("name") or "").casefold()
                if character.isalnum()
            )
            if normalized_name in {"outofoffice", "ooo"}:
                result["ooo"].add(int(category_id))
            elif str(category.get("sentiment_type") or "").casefold() == "positive":
                result["positive"].add(int(category_id))
        return {
            reply_type: sorted(category_ids)
            for reply_type, category_ids in result.items()
        }

    @staticmethod
    def _group_campaigns_by_category_ids(
        campaigns: list[dict[str, Any]],
        category_ids_by_type: dict[ReplyType, list[int]],
        reply_types: list[ReplyType],
    ) -> list[tuple[list[int], list[int]]]:
        category_ids = sorted(
            {
                category_id
                for reply_type in reply_types
                for category_id in category_ids_by_type[reply_type]
            }
        )
        campaign_ids = sorted(
            int(campaign["smartlead_campaign_id"]) for campaign in campaigns
        )
        return [(campaign_ids, category_ids)]

    async def _preflight(
        self,
        campaign_ids: list[int],
        category_ids: list[int],
        request: ImportRequest,
    ) -> tuple[int, list[dict[str, Any]]]:
        total = 0
        errors: list[dict[str, Any]] = []
        seen: set[str] = set()
        for campaign_group in chunks(campaign_ids, 5):
            for category_group in chunks(category_ids, 10):
                offset = 0
                while True:
                    try:
                        page = await self._smartlead.get_inbox_page(
                            campaign_ids=campaign_group,
                            category_ids=category_group,
                            offset=offset,
                            limit=20,
                            fetch_message_history=False,
                            reply_time_from=request.reply_time_from,
                            reply_time_to=request.reply_time_to,
                        )
                    except SmartLeadError as exc:
                        errors.append(
                            {
                                "scope": "preflight",
                                "campaign_ids": campaign_group,
                                "category_ids": category_group,
                                "message": str(exc),
                            }
                        )
                        break

                    messages = page.get("messages", [])
                    if not isinstance(messages, list):
                        messages = []
                    reported_total = page.get("total_count")
                    if reported_total is not None:
                        total += int(reported_total)
                        break

                    for item in messages:
                        if not isinstance(item, dict):
                            continue
                        map_id = self._campaign_lead_map_id(item)
                        key = (
                            f"{self._campaign_id(item)}:{map_id}"
                            if map_id is not None
                            else (
                                f"{self._campaign_id(item)}:"
                                f"{normalize_email(self._inbox_lead(item).get('email'))}"
                            )
                        )
                        if key not in seen:
                            seen.add(key)
                            total += 1
                    offset += len(messages)
                    if len(messages) < 20:
                        break
        return total, errors

    async def _fetch_inbox_items(
        self,
        campaign_ids: list[int],
        category_ids: list[int],
        request: ImportRequest,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        seen: set[str] = set()

        for campaign_group in chunks(campaign_ids, 5):
            for category_group in chunks(category_ids, 10):
                offset = 0
                while True:
                    try:
                        page = await self._smartlead.get_inbox_page(
                            campaign_ids=campaign_group,
                            category_ids=category_group,
                            offset=offset,
                            limit=20,
                            fetch_message_history=True,
                            reply_time_from=request.reply_time_from,
                            reply_time_to=request.reply_time_to,
                        )
                    except SmartLeadError as exc:
                        errors.append(
                            {
                                "scope": "inbox",
                                "campaign_ids": campaign_group,
                                "category_ids": category_group,
                                "offset": offset,
                                "message": str(exc),
                            }
                        )
                        break

                    messages = page.get("messages", [])
                    if not isinstance(messages, list):
                        messages = []
                    for item in messages:
                        if not isinstance(item, dict):
                            continue
                        map_id = self._campaign_lead_map_id(item)
                        key = (
                            f"{self._campaign_id(item)}:{map_id}"
                            if map_id is not None
                            else (
                                f"{self._campaign_id(item)}:"
                                f"{normalize_email(self._inbox_lead(item).get('email'))}"
                            )
                        )
                        if key not in seen:
                            results.append(item)
                            seen.add(key)

                    offset += len(messages)
                    reported_total = page.get("total_count")
                    if (
                        not messages
                        or len(messages) < 20
                        or (
                            reported_total is not None and offset >= int(reported_total)
                        )
                    ):
                        break
        return results, errors

    async def _fetch_campaign_lead_details(
        self, campaign_ids: list[int], category_ids: list[int]
    ) -> tuple[
        dict[tuple[int, str], dict[str, Any]],
        dict[tuple[int, str], dict[str, Any]],
        list[dict[str, Any]],
    ]:
        by_map: dict[tuple[int, str], dict[str, Any]] = {}
        by_email: dict[tuple[int, str], dict[str, Any]] = {}
        errors: list[dict[str, Any]] = []

        for campaign_id in campaign_ids:
            for category_id in category_ids:
                offset = 0
                while True:
                    try:
                        page = await self._smartlead.get_campaign_leads_page(
                            campaign_id=campaign_id,
                            category_id=category_id,
                            offset=offset,
                            limit=100,
                        )
                    except SmartLeadError as exc:
                        errors.append(
                            {
                                "scope": "campaign_leads",
                                "campaign_id": campaign_id,
                                "category_id": category_id,
                                "offset": offset,
                                "message": str(exc),
                            }
                        )
                        break

                    records = page.get("data", page.get("leads", []))
                    if not isinstance(records, list):
                        records = []
                    for record in records:
                        if not isinstance(record, dict):
                            continue
                        lead = (
                            record.get("lead")
                            if isinstance(record.get("lead"), dict)
                            else record
                        )
                        map_id = record.get("campaign_lead_map_id") or lead.get(
                            "campaign_lead_map_id"
                        )
                        if map_id is not None:
                            by_map[(campaign_id, str(map_id))] = record
                        email = normalize_email(lead.get("email"))
                        if email:
                            by_email[(campaign_id, email)] = record

                    offset += len(records)
                    if len(records) < 100:
                        break
        return by_map, by_email, errors

    async def _persist_item(
        self,
        item: dict[str, Any],
        detail_by_map: dict[tuple[int, str], dict[str, Any]],
        detail_by_email: dict[tuple[int, str], dict[str, Any]],
        category_type_by_id: dict[int, ReplyType],
    ) -> dict[str, Any] | None:
        campaign_id = self._campaign_id(item)
        if campaign_id is None:
            raise ValueError("Inbox item is missing its campaign ID")

        inbox_lead = self._inbox_lead(item)
        map_id_value = self._campaign_lead_map_id(item)
        map_id = str(map_id_value) if map_id_value is not None else ""
        email_normalized = normalize_email(inbox_lead.get("email"))
        detail_record = detail_by_map.get((campaign_id, map_id))
        if detail_record is None and email_normalized:
            detail_record = detail_by_email.get((campaign_id, email_normalized))
        detail_record = detail_record or {}
        detailed_lead = (
            detail_record.get("lead")
            if isinstance(detail_record.get("lead"), dict)
            else detail_record
        )
        lead_properties = merge_non_empty(inbox_lead, detailed_lead)
        email = str(
            lead_properties.get("email") or inbox_lead.get("email") or ""
        ).strip()
        email_normalized = normalize_email(email)
        if not email_normalized or "@" not in email_normalized:
            raise ValueError("Inbox item has no valid lead email")

        custom_properties = lead_properties.get("custom_fields", {})
        if not isinstance(custom_properties, dict):
            custom_properties = {"value": custom_properties}

        inbound_messages = self._inbound_messages(item)
        if not inbound_messages:
            raise ValueError("Qualifying conversation contains no inbound messages")
        received_times = [
            self._message_received_at(message) for message in inbound_messages
        ]
        observed_at = max(received_times)
        qualified_at = min(received_times)

        typed_properties = {
            "first_name": first_present(lead_properties, ["first_name", "firstname"]),
            "last_name": first_present(lead_properties, ["last_name", "lastname"]),
            "smartlead_phone_number": first_present(
                lead_properties, ["phone_number", "phone"]
            ),
            "company_name": first_present(lead_properties, ["company_name", "company"]),
            "location": lead_properties.get("location"),
            "website": lead_properties.get("website"),
            "company_url": first_present(
                lead_properties, ["company_url", "company_website"]
            ),
            "linkedin_profile": first_present(
                lead_properties, ["linkedin_profile", "linkedin_url"]
            ),
        }
        if not map_id:
            map_id = f"fallback:{campaign_id}:{email_normalized}"
        category = (
            item.get("category") if isinstance(item.get("category"), dict) else {}
        )
        category_id = category.get("id") or item.get("lead_category_id")
        category_name = category.get("name") or item.get("category_name")
        try:
            reply_type = category_type_by_id[int(category_id)]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Inbox item has no recognized reply category") from exc
        lead_external_id = detailed_lead.get("id") or inbox_lead.get("id")
        persisted = await self._repository.upsert_lead_conversation(
            email=email,
            email_normalized=email_normalized,
            observed_at=to_iso(observed_at),
            typed_properties=typed_properties,
            properties=lead_properties,
            custom_properties=custom_properties,
            conversation={
                "smartlead_campaign_id": campaign_id,
                "smartlead_campaign_lead_map_id": map_id,
                "smartlead_lead_id": (
                    str(lead_external_id) if lead_external_id is not None else None
                ),
                "positive_category_id": category_id,
                "positive_category_name": category_name,
                "reply_type": reply_type,
                "qualified_at": to_iso(qualified_at),
                "lead_properties": {
                    **lead_properties,
                    "_campaign_record": detail_record,
                },
                "custom_properties": custom_properties,
            },
        )
        lead = persisted["lead"]
        conversation = persisted["conversation"]

        for message in inbound_messages:
            message_id = message.get("id") or message.get("message_id")
            received_at = self._message_received_at(message)
            dedupe_key = self._reply_dedupe_key(
                conversation_id=str(conversation["id"]),
                message=message,
                received_at=received_at,
            )
            await self._repository.upsert_reply(
                {
                    "conversation_id": conversation["id"],
                    "smartlead_message_id": str(message_id) if message_id else None,
                    "dedupe_key": dedupe_key,
                    "subject": message.get("subject"),
                    "body": _message_body(message),
                    "sent_from": message.get("sent_from")
                    or message.get("from_email")
                    or message.get("from"),
                    "sent_to": message.get("sent_to")
                    or message.get("to_email")
                    or message.get("to"),
                    "received_at": to_iso(received_at),
                    "message_properties": message,
                }
            )

        return {
            "lead_id": str(lead["id"]),
            "conversation_id": str(conversation["id"]),
            "campaign_id": campaign_id,
            "campaign_lead_map_id": map_id,
            "reply_type": reply_type,
            "reply_count": len(inbound_messages),
        }

    async def _finish_run(
        self,
        run_id: str,
        *,
        status: str,
        errors: list[dict[str, Any]],
        qualifying_conversation_count: int | None = None,
        leads_processed: int = 0,
        conversations_processed: int = 0,
        replies_processed: int = 0,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {
            "status": status,
            "errors": errors,
            "leads_processed": leads_processed,
            "conversations_processed": conversations_processed,
            "replies_processed": replies_processed,
            "completed_at": to_iso(utc_now()),
        }
        if qualifying_conversation_count is not None:
            values["qualifying_conversation_count"] = qualifying_conversation_count
        return await self._repository.update_import_run(run_id, values)

    @staticmethod
    def _campaign_id(item: dict[str, Any]) -> int | None:
        campaign = item.get("campaign")
        value = (
            campaign.get("id")
            if isinstance(campaign, dict)
            else item.get("campaign_id") or item.get("email_campaign_id")
        )
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _campaign_lead_map_id(item: dict[str, Any]) -> Any:
        return item.get("campaign_lead_map_id") or item.get("email_lead_map_id")

    @staticmethod
    def _inbox_lead(item: dict[str, Any]) -> dict[str, Any]:
        if isinstance(item.get("lead"), dict):
            return item["lead"]
        return {
            key: value
            for key, value in {
                "id": item.get("email_lead_id"),
                "campaign_lead_map_id": item.get("email_lead_map_id"),
                "email": item.get("lead_email"),
                "first_name": item.get("lead_first_name"),
                "last_name": item.get("lead_last_name"),
                "revenue": item.get("revenue"),
            }.items()
            if value not in (None, "")
        }

    @classmethod
    def _inbound_messages(cls, item: dict[str, Any]) -> list[dict[str, Any]]:
        history = item.get("message_history") or item.get("email_history")
        messages = history if isinstance(history, list) else []
        inbound = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            direction = str(message.get("direction", "")).casefold()
            message_type = str(message.get("type", "")).casefold()
            if direction == "inbound" or message_type == "reply":
                inbound.append(message)

        if inbound:
            return inbound
        last_message = item.get("last_message")
        return [last_message] if isinstance(last_message, dict) else []

    @staticmethod
    def _message_received_at(message: dict[str, Any]) -> datetime:
        value = (
            message.get("received_at")
            or message.get("time_replied")
            or message.get("time")
            or message.get("sent_at")
        )
        return parse_datetime(value)

    @classmethod
    def _item_observed_at(cls, item: dict[str, Any]) -> datetime:
        messages = cls._inbound_messages(item)
        if not messages:
            return datetime.min.replace(tzinfo=utc_now().tzinfo)
        return max(cls._message_received_at(message) for message in messages)

    @staticmethod
    def _reply_dedupe_key(
        *, conversation_id: str, message: dict[str, Any], received_at: datetime
    ) -> str:
        message_id = message.get("id") or message.get("message_id")
        if message_id:
            material = f"{conversation_id}:id:{message_id}"
        else:
            material = json.dumps(
                {
                    "conversation_id": conversation_id,
                    "received_at": to_iso(received_at),
                    "subject": message.get("subject"),
                    "body": message.get("body")
                    or message.get("email_body")
                    or message.get("reply_body"),
                    "sent_from": message.get("sent_from") or message.get("from_email"),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
