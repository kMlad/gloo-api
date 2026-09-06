from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from app.heyreach.client import HeyReachClient, HeyReachError
from app.heyreach.repository import HeyReachRepository
from app.models import HeyReachImportRequest
from app.phone_enrichment.providers.linkedin import canonical_linkedin_profile
from app.repositories import ConcurrentImportError
from app.services import ImportLimitExceeded, ImportValidationError
from app.utils import (
    first_present,
    merge_non_empty,
    normalize_email,
    parse_datetime,
    to_iso,
    utc_now,
)

logger = logging.getLogger(__name__)

_REPLIED_STATUSES = {"messagereply", "inmailreply", "replied"}


class HeyReachCampaignService:
    def __init__(
        self,
        repository: HeyReachRepository,
        heyreach: HeyReachClient | None = None,
    ) -> None:
        self._repository = repository
        self._heyreach = heyreach

    async def list(self) -> list[dict[str, Any]]:
        if self._heyreach is not None:
            remote_campaigns = await self._heyreach.list_campaigns()
            await self._repository.sync_campaign_catalog(remote_campaigns)
        campaigns = await self._repository.list_campaigns()
        campaign_ids = [int(item["heyreach_campaign_id"]) for item in campaigns]
        stats = await self._repository.get_campaign_import_stats(campaign_ids)
        return [
            {
                **campaign,
                **stats.get(int(campaign["heyreach_campaign_id"]), {}),
            }
            for campaign in campaigns
        ]

    async def add(self, campaign_id: int, enabled: bool) -> dict[str, Any]:
        if self._heyreach is None:
            raise RuntimeError("HeyReach client is required to add a campaign")
        campaign = await self._heyreach.get_campaign(campaign_id)
        name = str(campaign.get("name") or f"HeyReach campaign {campaign_id}")
        return await self._repository.upsert_campaign(campaign_id, name, enabled)

    async def update(
        self, campaign_id: int, *, enabled: bool | None
    ) -> dict[str, Any] | None:
        return await self._repository.update_campaign(campaign_id, enabled=enabled)


class HeyReachImportService:
    def __init__(
        self,
        repository: HeyReachRepository,
        heyreach: HeyReachClient,
        *,
        max_conversations: int,
    ) -> None:
        self._repository = repository
        self._heyreach = heyreach
        self._max_conversations = max_conversations
        self._linkedin_accounts: dict[int, dict[str, Any]] | None = None

    async def start(
        self,
        request: HeyReachImportRequest,
        *,
        idempotency_key: str | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        campaigns = await self._resolve_campaigns(request.campaign_ids)
        campaign_ids = [item["heyreach_campaign_id"] for item in campaigns]
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
                reply_time_from=reply_time_from,
                reply_time_to=reply_time_to,
                max_conversations=self._max_conversations,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
            )
        except ConcurrentImportError:
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
        request = HeyReachImportRequest(
            campaign_ids=[int(value) for value in run["campaign_ids"]],
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
            logger.exception("HeyReach import run %s failed", run_id)
            run = await self._repository.get_import_run(run_id)
            if run is not None and run["status"] in {"queued", "running"}:
                await self._finish_run(
                    run_id,
                    status="failed",
                    errors=[{"scope": "import", "message": str(exc)}],
                )

    async def _execute(
        self,
        run_id: str,
        request: HeyReachImportRequest,
        campaigns: list[dict[str, Any]],
    ) -> dict[str, Any]:
        campaign_ids = [int(item["heyreach_campaign_id"]) for item in campaigns]
        try:
            qualifying: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            for campaign_id in campaign_ids:
                items, fetch_errors = await self._fetch_replied_leads(
                    campaign_id, request
                )
                qualifying.extend(items)
                errors.extend(fetch_errors)

            count = len(qualifying)
            await self._repository.update_import_run(
                run_id, {"qualifying_conversation_count": count}
            )
            if count > self._max_conversations:
                completed = await self._finish_run(
                    run_id,
                    status="rejected",
                    errors=[
                        *errors,
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

            lead_ids: set[str] = set()
            conversations_processed = 0
            replies_processed = 0
            for item in qualifying:
                try:
                    result = await self._persist_item(item)
                except Exception as exc:  # noqa: BLE001 - isolate one conversation
                    errors.append(
                        {
                            "scope": "conversation",
                            "campaign_id": self._campaign_id(item),
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
        except HeyReachError as exc:
            return await self._finish_run(
                run_id,
                status="failed",
                errors=[{"scope": "heyreach", "message": str(exc)}],
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
            found = {item["heyreach_campaign_id"] for item in campaigns}
            missing = sorted(set(requested_ids) - found)
            if missing:
                remote_campaigns = []
                for campaign_id in missing:
                    campaign = await self._heyreach.get_campaign(campaign_id)
                    remote_campaigns.append({**campaign, "id": campaign_id})
                await self._repository.sync_campaign_catalog(remote_campaigns)
                campaigns = await self._repository.get_campaigns_by_ids(requested_ids)

        if not campaigns:
            raise ImportValidationError("No enabled HeyReach campaigns are configured")
        return campaigns

    @staticmethod
    def _matches_request(
        run: dict[str, Any], request: HeyReachImportRequest, campaign_ids: list[int]
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
            and stored_from == request.reply_time_from
            and stored_to == request.reply_time_to
        )

    async def _fetch_replied_leads(
        self, campaign_id: int, request: HeyReachImportRequest
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        offset = 0
        while True:
            try:
                page = await self._heyreach.get_campaign_leads_page(
                    campaign_id=campaign_id,
                    offset=offset,
                    limit=100,
                    time_from=request.reply_time_from,
                    time_to=request.reply_time_to,
                )
            except HeyReachError as exc:
                errors.append(
                    {
                        "scope": "campaign_leads",
                        "campaign_id": campaign_id,
                        "offset": offset,
                        "message": str(exc),
                    }
                )
                break
            records = page.get("items", [])
            for record in records:
                if not isinstance(record, dict):
                    continue
                if not self._is_replied(record):
                    continue
                items.append({**record, "_campaign_id": campaign_id})
            offset += len(records)
            if len(records) < 100:
                break
        return items, errors

    async def _persist_item(self, item: dict[str, Any]) -> dict[str, Any] | None:
        campaign_id = self._campaign_id(item)
        if campaign_id is None:
            raise ValueError("Campaign lead is missing its campaign ID")
        profile = self._profile(item)
        linkedin = canonical_linkedin_profile(
            first_present(
                profile,
                ["profileUrl", "profile_url", "linkedinUrl", "linkedin_url"],
            )
            or first_present(item, ["profileUrl", "linkedinUrl"])
        )
        if linkedin is None:
            raise ValueError("Campaign lead has no LinkedIn profile URL")

        email = str(
            first_present(
                profile,
                ["emailAddress", "email_address", "email", "enrichedEmailAddress"],
            )
            or ""
        ).strip()
        email_normalized = normalize_email(email)
        if not email_normalized or "@" not in email_normalized:
            email = None
            email_normalized = None

        custom_fields = profile.get("customUserFields") or profile.get("custom_fields")
        custom_properties = self._custom_properties(custom_fields)
        conversation_meta = await self._conversation_for_lead(campaign_id, linkedin)
        messages = conversation_meta.get("messages") or []
        inbound = [message for message in messages if self._message_direction(message) == "inbound"]
        received_times = [
            self._message_received_at(message) for message in inbound or messages
        ]
        last_action = item.get("lastActionTime") or item.get("creationTime")
        if last_action:
            try:
                received_times.append(parse_datetime(last_action))
            except ValueError:
                pass
        if not received_times:
            observed_at = utc_now()
        else:
            observed_at = max(received_times)
        qualified_at = min(received_times) if received_times else observed_at

        typed_properties = {
            "first_name": first_present(profile, ["firstName", "first_name"]),
            "last_name": first_present(profile, ["lastName", "last_name"]),
            "smartlead_phone_number": first_present(
                profile, ["phoneNumber", "phone_number", "phone"]
            ),
            "company_name": first_present(profile, ["companyName", "company_name", "company"]),
            "location": first_present(profile, ["location", "address"]),
            "website": first_present(profile, ["website", "companyWebsite"]),
            "company_url": first_present(
                profile, ["companyUrl", "company_url", "companyWebsite"]
            ),
            "linkedin_profile": linkedin,
            "linkedin_profile_normalized": linkedin,
        }
        conversation_id = str(
            conversation_meta.get("id")
            or f"fallback:{campaign_id}:{linkedin}"
        )
        lead_external_id = item.get("id") or profile.get("id")
        account_id = self._linkedin_account_id(item, conversation_meta)
        sender_name = await self._resolve_sender_name(
            item, conversation_meta, account_id
        )
        persisted = await self._repository.upsert_lead_conversation(
            email=email,
            email_normalized=email_normalized,
            observed_at=to_iso(observed_at),
            typed_properties=typed_properties,
            properties=merge_non_empty(item, profile),
            custom_properties=custom_properties,
            conversation={
                "heyreach_campaign_id": campaign_id,
                "heyreach_conversation_id": conversation_id,
                "heyreach_lead_id": (
                    str(lead_external_id) if lead_external_id is not None else None
                ),
                "linkedin_account_id": account_id,
                "linkedin_sender_name": sender_name,
                "reply_type": "positive",
                "qualified_at": to_iso(qualified_at),
                "lead_properties": {
                    **profile,
                    "_campaign_record": item,
                },
                "custom_properties": custom_properties,
            },
        )
        lead = persisted["lead"]
        conversation = persisted["conversation"]
        reply_count = 0
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = message.get("id") or message.get("messageId")
            received_at = self._message_received_at(message)
            direction = self._message_direction(message)
            await self._repository.upsert_reply(
                {
                    "conversation_id": conversation["id"],
                    "heyreach_message_id": str(message_id) if message_id else None,
                    "dedupe_key": self._reply_dedupe_key(
                        conversation_id=str(conversation["id"]),
                        message=message,
                        received_at=received_at,
                    ),
                    "subject": message.get("subject"),
                    "body": self._message_body(message),
                    "sent_from": self._sent_from(
                        message, direction=direction, sender_name=sender_name
                    ),
                    "sent_to": message.get("sent_to"),
                    "received_at": to_iso(received_at),
                    "direction": direction,
                    "message_properties": message,
                }
            )
            reply_count += 1
        return {
            "lead_id": str(lead["id"]),
            "conversation_id": str(conversation["id"]),
            "campaign_id": campaign_id,
            "reply_count": reply_count,
        }

    async def _conversation_for_lead(
        self, campaign_id: int, linkedin: str
    ) -> dict[str, Any]:
        try:
            page = await self._heyreach.get_conversations_page(
                campaign_ids=[campaign_id],
                lead_profile_url=linkedin,
                offset=0,
                limit=10,
            )
        except HeyReachError:
            return {}
        conversation = next(
            (item for item in page.get("items", []) if isinstance(item, dict)),
            {},
        )
        if not conversation:
            return {}
        account_id = conversation.get("linkedInAccountId") or conversation.get(
            "linkedinAccountId"
        )
        conversation_id = conversation.get("id") or conversation.get("conversationId")
        messages = conversation.get("messages")
        if (
            isinstance(account_id, int | str)
            and conversation_id not in (None, "")
            and not isinstance(messages, list)
        ):
            try:
                chatroom = await self._heyreach.get_chatroom(
                    account_id=int(account_id),
                    conversation_id=str(conversation_id),
                )
                messages = chatroom.get("messages")
                conversation = {**conversation, **chatroom}
            except HeyReachError:
                messages = []
        if isinstance(messages, list):
            conversation["messages"] = [item for item in messages if isinstance(item, dict)]
        else:
            conversation["messages"] = []
        return conversation

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

    @classmethod
    def _is_replied(cls, item: dict[str, Any]) -> bool:
        status = str(
            item.get("leadMessageStatus") or item.get("lead_message_status") or ""
        ).casefold()
        return status in _REPLIED_STATUSES or "reply" in status

    @staticmethod
    def _campaign_id(item: dict[str, Any]) -> int | None:
        value = item.get("_campaign_id") or item.get("campaignId")
        campaign = item.get("campaign")
        if value is None and isinstance(campaign, dict):
            value = campaign.get("id")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _profile(item: dict[str, Any]) -> dict[str, Any]:
        for key in ("linkedInUserProfile", "linkedinUserProfile", "profile", "lead"):
            value = item.get(key)
            if isinstance(value, dict):
                return value
        return item

    @staticmethod
    def _custom_properties(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            result: dict[str, Any] = {}
            for field in value:
                if not isinstance(field, dict):
                    continue
                name = field.get("name") or field.get("key")
                if name:
                    result[str(name)] = field.get("value")
            return result
        if value in (None, ""):
            return {}
        return {"value": value}

    @staticmethod
    def _message_body(message: dict[str, Any]) -> str:
        return str(
            message.get("body")
            or message.get("text")
            or message.get("message")
            or message.get("content")
            or ""
        )

    @staticmethod
    def _message_direction(message: dict[str, Any]) -> str:
        direction = str(message.get("direction", "")).casefold()
        if direction in {"inbound", "outbound"}:
            return direction
        for key in ("isFromMe", "isSender", "isOutgoing"):
            if message.get(key) is True:
                return "outbound"
            if message.get(key) is False:
                return "inbound"
        sender = str(message.get("sender") or "").strip().casefold()
        if sender in {"", "you", "me"}:
            return "outbound"
        return "inbound"

    @staticmethod
    def _message_received_at(message: dict[str, Any]) -> datetime:
        value = (
            message.get("createdAt")
            or message.get("sentAt")
            or message.get("timestamp")
            or message.get("received_at")
            or message.get("created_at")
        )
        return parse_datetime(value, default=utc_now())

    @staticmethod
    def _reply_dedupe_key(
        *, conversation_id: str, message: dict[str, Any], received_at: datetime
    ) -> str:
        message_id = message.get("id") or message.get("messageId")
        if message_id:
            material = f"{conversation_id}:id:{message_id}"
        else:
            material = json.dumps(
                {
                    "conversation_id": conversation_id,
                    "received_at": to_iso(received_at),
                    "body": message.get("body") or message.get("text"),
                    "sender": message.get("sender"),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def _resolve_sender_name(
        self,
        item: dict[str, Any],
        conversation: dict[str, Any],
        account_id: int | None,
    ) -> str | None:
        name = self.linkedin_sender_name(item, conversation)
        if name or account_id is None:
            return name
        account = await self._linkedin_account(account_id)
        return self.linkedin_sender_name(account)

    async def _linkedin_account(self, account_id: int) -> dict[str, Any] | None:
        if self._linkedin_accounts is None:
            try:
                accounts = await self._heyreach.list_linkedin_accounts()
            except HeyReachError:
                accounts = []
            self._linkedin_accounts = {}
            for account in accounts:
                if not isinstance(account, dict):
                    continue
                value = account.get("id")
                try:
                    self._linkedin_accounts[int(value)] = account
                except (TypeError, ValueError):
                    continue
        cached = self._linkedin_accounts.get(account_id)
        if cached is not None:
            return cached
        try:
            account = await self._heyreach.get_linkedin_account(account_id)
        except HeyReachError:
            return None
        if isinstance(account, dict):
            self._linkedin_accounts[account_id] = account
            return account
        return None

    @classmethod
    def linkedin_sender_name(cls, *sources: Any) -> str | None:
        for source in sources:
            if isinstance(source, str):
                cleaned = cls._clean_sender_name(source)
                if cleaned:
                    return cleaned
                continue
            name = cls._extract_linkedin_sender_name(source)
            if name:
                return name
        return None

    @classmethod
    def _extract_linkedin_sender_name(cls, source: Any) -> str | None:
        if not isinstance(source, dict):
            return None
        full = first_present(
            source,
            [
                "linkedInSenderFullName",
                "linkedinSenderFullName",
                "fullName",
                "full_name",
            ],
        )
        cleaned = cls._clean_sender_name(full)
        if cleaned:
            return cleaned
        first = first_present(source, ["firstName", "first_name"])
        last = first_present(source, ["lastName", "last_name"])
        combined = " ".join(
            str(part).strip()
            for part in (first, last)
            if part not in (None, "")
        ).strip()
        cleaned = cls._clean_sender_name(combined)
        if cleaned:
            return cleaned
        cleaned = cls._clean_sender_name(first_present(source, ["name"]))
        if cleaned:
            return cleaned
        nested = (
            source.get("linkedInAccount")
            or source.get("linkedinAccount")
            or source.get("account")
        )
        if nested is not source:
            return cls._extract_linkedin_sender_name(nested)
        return None

    @staticmethod
    def _linkedin_account_id(*sources: dict[str, Any]) -> int | None:
        for source in sources:
            value = (
                source.get("linkedInAccountId")
                or source.get("linkedin_account_id")
                or source.get("linkedinAccountId")
                or source.get("linkedInSenderId")
                or source.get("linkedinSenderId")
            )
            nested = (
                source.get("linkedInAccount")
                or source.get("linkedinAccount")
                or source.get("account")
            )
            if value is None and isinstance(nested, dict):
                value = nested.get("id")
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return None

    @classmethod
    def _sent_from(
        cls,
        message: dict[str, Any],
        *,
        direction: str,
        sender_name: str | None,
    ) -> str | None:
        raw = message.get("sender") or message.get("sent_from")
        text = cls._clean_sender_name(raw)
        if direction == "outbound":
            return sender_name or text
        return text

    @staticmethod
    def _clean_sender_name(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text or text.casefold() in {"you", "me", "us"}:
            return None
        return text
