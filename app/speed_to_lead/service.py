from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.phone_enrichment.parser import reply_to_text
from app.phone_enrichment.schemas import PhoneEnrichmentRequest
from app.phone_enrichment.service import (
    EnrichmentConflictError,
    EnrichmentNotFoundError,
    EnrichmentValidationError,
    PhoneEnrichedEvent,
)
from app.services import ImportService, _message_body
from app.smartlead.client import SmartLeadClient, SmartLeadError
from app.speed_to_lead.notifications import SpeedToLeadNotifier
from app.speed_to_lead.repository import SpeedToLeadRepository
from app.speed_to_lead.schemas import SpeedToLeadOutcome
from app.utils import first_present, normalize_email, parse_datetime, to_iso, utc_now

if TYPE_CHECKING:
    from app.phone_enrichment.service import PhoneEnrichmentService
    from app.repositories import Repository

logger = logging.getLogger(__name__)

WEBHOOK_NAME = "Gloo speed to lead"
REPLY_EXCERPT_LENGTH = 280
CATEGORY_CACHE_SECONDS = 3600


class SpeedToLeadNotFoundError(Exception):
    pass


class SpeedToLeadValidationError(Exception):
    pass


@dataclass(frozen=True)
class SpeedToLeadResult:
    outcome: SpeedToLeadOutcome
    event: dict[str, Any] | None = None
    reason: str | None = None


class SpeedToLeadService:
    def __init__(
        self,
        repository: SpeedToLeadRepository,
        leads: Repository,
        smartlead: SmartLeadClient,
        phone_enrichment: PhoneEnrichmentService,
        *,
        webhook_url: str,
        event_type: str = "LEAD_CATEGORY_UPDATED",
        category_cache_seconds: int = CATEGORY_CACHE_SECONDS,
        notifier: SpeedToLeadNotifier | None = None,
    ) -> None:
        self._repository = repository
        self._leads = leads
        self._smartlead = smartlead
        self._phone_enrichment = phone_enrichment
        self._webhook_url = webhook_url
        self._event_type = event_type
        self._category_cache_seconds = category_cache_seconds
        self._category_cache: tuple[float, dict[int, str]] | None = None
        self._notifier = notifier

    # ------------------------------------------------------------------ opt-in

    async def configure_smartlead_campaign(
        self, campaign_id: int, *, enabled: bool, sdr_id: str | None
    ) -> dict[str, Any]:
        campaign = await self._repository.get_smartlead_campaign(campaign_id)
        if campaign is None:
            raise SpeedToLeadNotFoundError("SmartLead campaign is not configured")
        if enabled and sdr_id is None:
            raise SpeedToLeadValidationError(
                "sdr_id is required to enable speed to lead"
            )

        existing_webhook_id = campaign.get("smartlead_webhook_id")
        webhook_id: str | None
        if enabled:
            categories = self._positive_category_names(
                await self._smartlead.get_categories()
            )
            if not categories:
                raise SpeedToLeadValidationError(
                    "SmartLead has no positive categories to subscribe to"
                )
            saved = await self._smartlead.save_webhook(
                campaign_id,
                name=WEBHOOK_NAME,
                webhook_url=self._webhook_url,
                event_types=[self._event_type],
                categories=categories,
                webhook_id=(
                    str(existing_webhook_id)
                    if existing_webhook_id not in (None, "")
                    else None
                ),
            )
            webhook_id = str(saved["id"])
        else:
            if existing_webhook_id not in (None, ""):
                try:
                    await self._smartlead.delete_webhook(
                        campaign_id, str(existing_webhook_id)
                    )
                except SmartLeadError as exc:
                    if exc.status_code != 404:
                        raise
            webhook_id = None

        updated = await self._repository.update_smartlead_campaign(
            campaign_id,
            enabled=enabled,
            sdr_id=sdr_id,
            webhook_id=webhook_id,
        )
        if updated is None:
            raise SpeedToLeadNotFoundError("SmartLead campaign is not configured")
        return updated

    # -------------------------------------------------------------------- list

    async def list_events(
        self,
        *,
        limit: int,
        offset: int,
        include_handled: bool = False,
        visible_to_sdr_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        events, total = await self._repository.list_events(
            limit=limit,
            offset=offset,
            include_handled=include_handled,
            visible_to_sdr_id=visible_to_sdr_id,
        )
        events = [event for event in events if event.get("lead") is not None]
        leads_by_id: dict[str, dict[str, Any]] = {}
        for event in events:
            lead = event["lead"]
            leads_by_id.setdefault(str(lead["id"]), lead)
        await self._leads.decorate_leads(list(leads_by_id.values()))

        smartlead_ids = sorted(
            {
                int(event["smartlead_campaign_id"])
                for event in events
                if event.get("smartlead_campaign_id") is not None
            }
        )
        heyreach_ids = sorted(
            {
                int(event["heyreach_campaign_id"])
                for event in events
                if event.get("heyreach_campaign_id") is not None
            }
        )
        smartlead_names = {
            int(item["smartlead_campaign_id"]): str(item["name"])
            for item in (
                await self._leads.get_campaigns_by_ids(smartlead_ids)
                if smartlead_ids
                else []
            )
        }
        heyreach_names = {
            int(item["heyreach_campaign_id"]): str(item["name"])
            for item in (
                await self._leads.get_heyreach_campaigns_by_ids(heyreach_ids)
                if heyreach_ids
                else []
            )
        }
        runs = await self._repository.get_enrichment_runs(
            [
                str(event["enrichment_run_id"])
                for event in events
                if event.get("enrichment_run_id") is not None
            ]
        )

        items = []
        for event in events:
            if event.get("smartlead_campaign_id") is not None:
                campaign_id = int(event["smartlead_campaign_id"])
                campaign_name = smartlead_names.get(
                    campaign_id, f"SmartLead campaign {campaign_id}"
                )
            elif event.get("heyreach_campaign_id") is not None:
                campaign_id = int(event["heyreach_campaign_id"])
                campaign_name = heyreach_names.get(
                    campaign_id, f"HeyReach campaign {campaign_id}"
                )
            else:
                campaign_name = "Unknown campaign"
            run_id = event.get("enrichment_run_id")
            items.append(
                {
                    **event,
                    "campaign_name": campaign_name,
                    "lead": leads_by_id[str(event["lead"]["id"])],
                    "enrichment": runs.get(str(run_id)) if run_id else None,
                }
            )
        return items, total

    # ----------------------------------------------------------------- webhook

    async def process_smartlead_webhook(self, payload: dict[str, Any]) -> None:
        """Background-task boundary: never raises."""
        try:
            result = await self.handle_smartlead_category_update(payload)
        except Exception:
            logger.exception("Speed-to-lead SmartLead webhook processing failed")
            return
        logger.info(
            "Speed-to-lead SmartLead webhook handled",
            extra={
                "outcome": result.outcome,
                "reason": result.reason,
                "event_id": (result.event or {}).get("id"),
            },
        )

    async def handle_smartlead_category_update(
        self, payload: dict[str, Any]
    ) -> SpeedToLeadResult:
        event_type = str(payload.get("event_type") or "").strip().upper()
        if event_type and event_type != self._event_type.upper():
            return SpeedToLeadResult("ignored_event", reason=event_type)

        campaign_id = self._campaign_id(payload)
        if campaign_id is None:
            return SpeedToLeadResult("invalid_payload", reason="missing campaign_id")
        campaign = await self._repository.get_smartlead_campaign(campaign_id)
        if campaign is None or not campaign.get("speed_to_lead_enabled"):
            return SpeedToLeadResult("campaign_not_enabled")

        lead_data = payload.get("lead_data")
        if not isinstance(lead_data, dict):
            lead_data = {}
        category_id = self._category_id(payload, lead_data)
        category_name = self._category_name(payload, lead_data)
        if not await self._is_positive(category_id, lead_data):
            return SpeedToLeadResult("not_positive", reason=category_name)

        email = str(payload.get("lead_email") or lead_data.get("email") or "").strip()
        email_normalized = normalize_email(email)
        if not email_normalized or "@" not in email_normalized:
            return SpeedToLeadResult("invalid_payload", reason="missing lead email")

        inbound_messages = ImportService._inbound_messages(
            {
                "message_history": payload.get("history"),
                "last_message": payload.get("lastReply") or payload.get("last_reply"),
            }
        )
        received_times = [
            received_at
            for received_at in (
                self._message_received_at(message) for message in inbound_messages
            )
            if received_at is not None
        ]
        replied_at = self._replied_at(payload, received_times)
        dedupe_key = self._dedupe_key(
            campaign_id=campaign_id,
            email_normalized=email_normalized,
            category_id=category_id,
            replied_at=replied_at,
        )
        if await self._repository.get_event_by_dedupe_key(dedupe_key) is not None:
            return SpeedToLeadResult("duplicate")

        persisted = await self._persist_lead_conversation(
            payload,
            lead_data,
            campaign_id=campaign_id,
            email=email,
            email_normalized=email_normalized,
            category_id=category_id,
            category_name=category_name,
            inbound_messages=inbound_messages,
            received_times=received_times,
            replied_at=replied_at,
        )
        lead = persisted["lead"]
        conversation = persisted["conversation"]
        lead_id = str(lead["id"])

        sdr_id = campaign.get("speed_to_lead_sdr_id")
        if lead.get("assigned_sdr_id") is None and sdr_id:
            assigned = await self._repository.assign_lead_if_unassigned(
                lead_id, sdr_id=str(sdr_id)
            )
            if assigned:
                lead = {**lead, "assigned_sdr_id": str(sdr_id)}

        event = await self._repository.insert_event(
            {
                "platform": "smartlead",
                "lead_id": lead_id,
                "smartlead_campaign_id": campaign_id,
                "heyreach_campaign_id": None,
                "conversation_id": str(conversation["id"]),
                "category_id": category_id,
                "category_name": category_name,
                "reply_excerpt": self._reply_excerpt(inbound_messages, payload),
                "replied_at": to_iso(replied_at),
                "dedupe_key": dedupe_key,
                "notification_status": "pending",
            }
        )
        if event is None:
            return SpeedToLeadResult("duplicate")

        event = await self._notify_alert(event, lead=lead, campaign=campaign)
        event = await self._start_enrichment(event, lead_id)
        return SpeedToLeadResult("processed", event=event)

    async def handle_phone_enriched(self, phone_event: PhoneEnrichedEvent) -> None:
        """Thread the enriched phone under the alert for the matching event."""
        if self._notifier is None or not self._notifier.enabled:
            return
        event = await self._repository.get_event_by_enrichment_run(
            phone_event.run_id
        )
        if event is None or not event.get("slack_message_ts"):
            return
        try:
            await self._notifier.post_phone(
                thread_ts=str(event["slack_message_ts"]),
                phone=phone_event.phone,
                source=phone_event.source,
            )
        except Exception as exc:
            logger.exception(
                "Speed-to-lead phone notification failed",
                extra={"event_id": event.get("id")},
            )
            await self._repository.update_event(
                str(event["id"]), {"notification_error": str(exc)[:500]}
            )

    async def handle_enrichment_finished(self, run: dict[str, Any]) -> None:
        """Thread the enrichment outcome under the alert when no phone was found."""
        if self._notifier is None or not self._notifier.enabled:
            return
        run_id = run.get("id")
        if run_id is None:
            return
        event = await self._repository.get_event_by_enrichment_run(str(run_id))
        if event is None or not event.get("slack_message_ts"):
            return
        items = run.get("items") or []
        if any(item.get("status") == "enriched" for item in items):
            return  # the phone follow-up was already threaded by handle_phone_enriched
        try:
            await self._notifier.post_enrichment_summary(
                thread_ts=str(event["slack_message_ts"]), items=items
            )
        except Exception as exc:
            logger.exception(
                "Speed-to-lead enrichment summary notification failed",
                extra={"event_id": event.get("id")},
            )
            await self._repository.update_event(
                str(event["id"]), {"notification_error": str(exc)[:500]}
            )

    # ----------------------------------------------------------- notifications

    async def _notify_alert(
        self,
        event: dict[str, Any],
        *,
        lead: dict[str, Any],
        campaign: dict[str, Any],
    ) -> dict[str, Any]:
        event_id = str(event["id"])
        if self._notifier is None or not self._notifier.enabled:
            updated = await self._repository.update_event(
                event_id, {"notification_status": "skipped"}
            )
            return updated or {**event, "notification_status": "skipped"}

        sdr_label: str | None = None
        assigned_sdr_id = lead.get("assigned_sdr_id")
        if assigned_sdr_id:
            sdr_label = await self._repository.get_user_email(str(assigned_sdr_id))
            sdr_label = sdr_label or "Assigned SDR"
        try:
            ts = await self._notifier.post_alert(
                lead=lead,
                campaign_name=str(
                    campaign.get("name") or f"SmartLead campaign {campaign.get('smartlead_campaign_id')}"
                ),
                reply_excerpt=event.get("reply_excerpt"),
                sdr_label=sdr_label,
            )
        except Exception as exc:
            logger.exception(
                "Speed-to-lead Slack alert failed", extra={"event_id": event_id}
            )
            values = {
                "notification_status": "failed",
                "notification_error": str(exc)[:500],
            }
        else:
            values = {
                "notification_status": "sent",
                "notification_error": None,
                "slack_message_ts": ts,
            }
        updated = await self._repository.update_event(event_id, values)
        return updated or {**event, **values}

    # ----------------------------------------------------------------- helpers

    async def _persist_lead_conversation(
        self,
        payload: dict[str, Any],
        lead_data: dict[str, Any],
        *,
        campaign_id: int,
        email: str,
        email_normalized: str,
        category_id: int | None,
        category_name: str | None,
        inbound_messages: list[dict[str, Any]],
        received_times: list[datetime],
        replied_at: datetime,
    ) -> dict[str, Any]:
        lead_properties = {
            key: value
            for key, value in lead_data.items()
            if key not in {"category", "custom_fields"}
        }
        lead_properties.setdefault("email", email)
        if payload.get("lead_name") and not lead_properties.get("first_name"):
            lead_properties["name"] = payload["lead_name"]
        custom_properties = lead_data.get("custom_fields", {})
        if not isinstance(custom_properties, dict):
            custom_properties = {"value": custom_properties}

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

        map_id_value = (
            payload.get("campaign_lead_map_id")
            or payload.get("lead_map_id")
            or lead_data.get("campaign_lead_map_id")
        )
        map_id = str(map_id_value) if map_id_value not in (None, "") else ""
        if not map_id:
            existing = await self._repository.find_smartlead_conversation(
                campaign_id=campaign_id, email_normalized=email_normalized
            )
            if existing is not None:
                map_id = str(existing["smartlead_campaign_lead_map_id"])
        if not map_id:
            map_id = f"fallback:{campaign_id}:{email_normalized}"

        observed_at = max(received_times) if received_times else replied_at
        qualified_at = min(received_times) if received_times else replied_at
        lead_external_id = payload.get("lead_id") or lead_data.get("id")
        persisted = await self._leads.upsert_lead_conversation(
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
                "reply_type": "positive",
                "qualified_at": to_iso(qualified_at),
                "lead_properties": {
                    **lead_properties,
                    "_webhook_event": {
                        "event_type": payload.get("event_type"),
                        "campaign_name": payload.get("campaign_name"),
                    },
                },
                "custom_properties": custom_properties,
            },
        )
        conversation = persisted["conversation"]
        for message in inbound_messages:
            received_at = self._message_received_at(message)
            if received_at is None:
                continue
            message_id = message.get("id") or message.get("message_id")
            await self._leads.upsert_reply(
                {
                    "conversation_id": conversation["id"],
                    "smartlead_message_id": str(message_id) if message_id else None,
                    "dedupe_key": ImportService._reply_dedupe_key(
                        conversation_id=str(conversation["id"]),
                        message=message,
                        received_at=received_at,
                    ),
                    "subject": message.get("subject"),
                    "body": _message_body(message),
                    "sent_from": message.get("sent_from")
                    or message.get("from_email")
                    or message.get("from")
                    or payload.get("from"),
                    "sent_to": message.get("sent_to")
                    or message.get("to_email")
                    or message.get("to")
                    or payload.get("to"),
                    "received_at": to_iso(received_at),
                    "direction": "inbound",
                    "message_properties": message,
                }
            )
        return persisted

    async def _start_enrichment(
        self, event: dict[str, Any], lead_id: str
    ) -> dict[str, Any]:
        event_id = str(event["id"])
        try:
            run = await self._phone_enrichment.start(
                PhoneEnrichmentRequest(lead_ids=[UUID(lead_id)]),
                f"speed-to-lead:{event_id}",
                created_by=None,
            )
        except (
            EnrichmentConflictError,
            EnrichmentValidationError,
            EnrichmentNotFoundError,
        ) as exc:
            logger.warning(
                "Speed-to-lead enrichment could not start",
                extra={"event_id": event_id, "lead_id": lead_id, "error": str(exc)},
            )
            return event
        run_id = str(run["id"])
        updated = await self._repository.update_event(
            event_id, {"enrichment_run_id": run_id}
        )
        status = run.get("status")
        if status == "queued":
            await self._phone_enrichment.execute_background(run_id)
        elif status in {"succeeded", "partial", "failed"}:
            # Nothing was queued (e.g. the lead already has a phone), so the run
            # finished before we stored its id and the listener could not find us.
            await self.handle_enrichment_finished(run)
        return updated or {**event, "enrichment_run_id": run_id}

    async def _is_positive(
        self, category_id: int | None, lead_data: dict[str, Any]
    ) -> bool:
        sentiments = await self._category_sentiments()
        if category_id is not None and category_id in sentiments:
            return sentiments[category_id] == "positive"
        category = lead_data.get("category")
        if isinstance(category, dict):
            sentiment = str(category.get("sentiment_type") or "").casefold()
            return sentiment == "positive"
        return False

    async def _category_sentiments(self) -> dict[int, str]:
        now = time.monotonic()
        if (
            self._category_cache is not None
            and now - self._category_cache[0] < self._category_cache_seconds
        ):
            return self._category_cache[1]
        try:
            categories = await self._smartlead.get_categories()
        except SmartLeadError:
            logger.warning("Could not refresh SmartLead categories for speed-to-lead")
            return self._category_cache[1] if self._category_cache else {}
        sentiments: dict[int, str] = {}
        for category in categories:
            try:
                identifier = int(category["id"])
            except (KeyError, TypeError, ValueError):
                continue
            sentiments[identifier] = str(
                category.get("sentiment_type") or ""
            ).casefold()
        self._category_cache = (now, sentiments)
        return sentiments

    @staticmethod
    def _positive_category_names(categories: list[dict[str, Any]]) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for category in categories:
            if str(category.get("sentiment_type") or "").casefold() != "positive":
                continue
            name = str(category.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
        return names

    @staticmethod
    def _campaign_id(payload: dict[str, Any]) -> int | None:
        value = payload.get("campaign_id")
        if value is None and isinstance(payload.get("campaign"), dict):
            value = payload["campaign"].get("id")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _category_id(
        payload: dict[str, Any], lead_data: dict[str, Any]
    ) -> int | None:
        value = payload.get("lead_category_id") or payload.get("category_id")
        if value is None:
            category = lead_data.get("category")
            if isinstance(category, dict):
                value = category.get("id")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _category_name(
        payload: dict[str, Any], lead_data: dict[str, Any]
    ) -> str | None:
        category = payload.get("category")
        if isinstance(category, dict):
            category = category.get("name")
        if not category:
            nested = lead_data.get("category")
            if isinstance(nested, dict):
                category = nested.get("name")
        return str(category) if category else None

    @staticmethod
    def _message_received_at(message: dict[str, Any]) -> datetime | None:
        try:
            return ImportService._message_received_at(message)
        except ValueError:
            return None

    @staticmethod
    def _replied_at(
        payload: dict[str, Any], received_times: list[datetime]
    ) -> datetime:
        if received_times:
            return max(received_times)
        for key in ("time_replied", "event_timestamp", "timestamp"):
            value = payload.get(key)
            if value:
                try:
                    return parse_datetime(value)
                except ValueError:
                    continue
        return utc_now()

    @staticmethod
    def _reply_excerpt(
        inbound_messages: list[dict[str, Any]], payload: dict[str, Any]
    ) -> str | None:
        body = ""
        if inbound_messages:
            latest = max(
                inbound_messages,
                key=lambda message: SpeedToLeadService._message_received_at(message)
                or datetime.min.replace(tzinfo=utc_now().tzinfo),
            )
            body = _message_body(latest)
        if not body:
            body = str(payload.get("reply_body") or payload.get("preview_text") or "")
        # SmartLead sends HTML; Slack renders it literally. Strip tags and quoted
        # history before collapsing whitespace.
        text = " ".join(reply_to_text(body).split())
        if not text:
            return None
        if len(text) > REPLY_EXCERPT_LENGTH:
            return text[: REPLY_EXCERPT_LENGTH - 1].rstrip() + "…"
        return text

    @staticmethod
    def _dedupe_key(
        *,
        campaign_id: int,
        email_normalized: str,
        category_id: int | None,
        replied_at: datetime,
    ) -> str:
        material = "smartlead:{}:{}:{}:{}".format(
            campaign_id,
            email_normalized,
            category_id if category_id is not None else "",
            to_iso(replied_at),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
