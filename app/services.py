from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.models import ImportRequest, ReplyType
from app.repositories import Repository
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


class ImportValidationError(Exception):
    pass


class ImportLimitExceeded(Exception):
    def __init__(self, run: dict[str, Any]) -> None:
        super().__init__("The import exceeds the configured conversation limit")
        self.run = run


class CampaignService:
    def __init__(
        self, repository: Repository, smartlead: SmartLeadClient | None = None
    ) -> None:
        self._repository = repository
        self._smartlead = smartlead

    async def list(self) -> list[dict[str, Any]]:
        return await self._repository.list_campaigns()

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

    async def run(self, request: ImportRequest) -> dict[str, Any]:
        campaigns = await self._resolve_campaigns(request.campaign_ids)
        campaign_ids = [item["smartlead_campaign_id"] for item in campaigns]
        reply_time_from = (
            to_iso(request.reply_time_from) if request.reply_time_from else None
        )
        reply_time_to = to_iso(request.reply_time_to) if request.reply_time_to else None

        run = await self._repository.create_import_run(
            campaign_ids=campaign_ids,
            reply_time_from=reply_time_from,
            reply_time_to=reply_time_to,
            max_conversations=self._max_conversations,
        )
        run_id = str(run["id"])

        try:
            categories = await self._smartlead.get_categories()
            category_ids_by_type = self._category_ids_by_reply_type(categories)
            required_reply_types = {
                reply_type
                for campaign in campaigns
                for reply_type in campaign.get("reply_types", ["positive"])
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
            campaign_groups = self._group_campaigns_by_category_ids(
                campaigns, category_ids_by_type
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
                group_by_map, group_by_email, group_detail_errors = (
                    await self._fetch_campaign_lead_details(
                        grouped_campaign_ids, category_ids
                    )
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
                except Exception as exc:
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
            disabled = sorted(
                item["smartlead_campaign_id"]
                for item in campaigns
                if not item["enabled"]
            )
            if missing:
                raise ImportValidationError(
                    f"Campaigns are not configured: {', '.join(map(str, missing))}"
                )
            if disabled:
                raise ImportValidationError(
                    f"Campaigns are disabled: {', '.join(map(str, disabled))}"
                )

        if not campaigns:
            raise ImportValidationError("No enabled SmartLead campaigns are configured")
        return campaigns

    @staticmethod
    def _category_ids_by_reply_type(
        categories: list[dict[str, Any]],
    ) -> dict[ReplyType, list[int]]:
        result: dict[ReplyType, set[int]] = {"positive": set(), "ooo": set()}
        for category in categories:
            category_id = category.get("id")
            if category_id is None:
                continue
            if str(category.get("sentiment_type") or "").casefold() == "positive":
                result["positive"].add(int(category_id))
            normalized_name = "".join(
                character
                for character in str(category.get("name") or "").casefold()
                if character.isalnum()
            )
            if normalized_name == "outofoffice":
                result["ooo"].add(int(category_id))
        return {
            reply_type: sorted(category_ids)
            for reply_type, category_ids in result.items()
        }

    @staticmethod
    def _group_campaigns_by_category_ids(
        campaigns: list[dict[str, Any]],
        category_ids_by_type: dict[ReplyType, list[int]],
    ) -> list[tuple[list[int], list[int]]]:
        grouped: dict[tuple[int, ...], list[int]] = {}
        for campaign in campaigns:
            category_ids = tuple(
                sorted(
                    {
                        category_id
                        for reply_type in campaign.get("reply_types", ["positive"])
                        for category_id in category_ids_by_type[reply_type]
                    }
                )
            )
            grouped.setdefault(category_ids, []).append(
                int(campaign["smartlead_campaign_id"])
            )
        return [
            (sorted(grouped_campaign_ids), list(category_ids))
            for category_ids, grouped_campaign_ids in grouped.items()
        ]

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
                            reported_total is not None
                            and offset >= int(reported_total)
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
                        lead = record.get("lead") if isinstance(record.get("lead"), dict) else record
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
        email = str(lead_properties.get("email") or inbox_lead.get("email") or "").strip()
        email_normalized = normalize_email(email)
        if not email_normalized or "@" not in email_normalized:
            raise ValueError("Inbox item has no valid lead email")

        custom_properties = lead_properties.get("custom_fields", {})
        if not isinstance(custom_properties, dict):
            custom_properties = {"value": custom_properties}

        inbound_messages = self._inbound_messages(item)
        if not inbound_messages:
            raise ValueError("Qualifying conversation contains no inbound messages")
        received_times = [self._message_received_at(message) for message in inbound_messages]
        observed_at = max(received_times)
        qualified_at = min(received_times)

        typed_properties = {
            "first_name": first_present(lead_properties, ["first_name", "firstname"]),
            "last_name": first_present(lead_properties, ["last_name", "lastname"]),
            "smartlead_phone_number": first_present(
                lead_properties, ["phone_number", "phone"]
            ),
            "company_name": first_present(
                lead_properties, ["company_name", "company"]
            ),
            "location": lead_properties.get("location"),
            "website": lead_properties.get("website"),
            "company_url": first_present(
                lead_properties, ["company_url", "company_website"]
            ),
            "linkedin_profile": first_present(
                lead_properties, ["linkedin_profile", "linkedin_url"]
            ),
        }
        lead = await self._repository.upsert_lead(
            email=email,
            email_normalized=email_normalized,
            observed_at=to_iso(observed_at),
            typed_properties=typed_properties,
            properties=lead_properties,
            custom_properties=custom_properties,
        )

        if not map_id:
            map_id = f"fallback:{campaign_id}:{email_normalized}"
        category = item.get("category") if isinstance(item.get("category"), dict) else {}
        category_id = category.get("id") or item.get("lead_category_id")
        category_name = category.get("name") or item.get("category_name")
        try:
            reply_type = category_type_by_id[int(category_id)]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Inbox item has no recognized reply category") from exc
        lead_external_id = detailed_lead.get("id") or inbox_lead.get("id")
        conversation = await self._repository.upsert_conversation(
            {
                "lead_id": lead["id"],
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
            }
        )

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
                    "body": str(
                        message.get("body")
                        or message.get("email_body")
                        or message.get("reply_body")
                        or ""
                    ),
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
            "campaign_id": campaign_id,
            "campaign_lead_map_id": map_id,
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
