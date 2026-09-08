from typing import Any

from app.notifications.slack import SlackClient
from app.phone_enrichment.providers.base import INSUFFICIENT_CREDITS, PROVIDER_LABELS
from app.phone_enrichment.service import describe_provider_failure

SOURCE_LABELS = PROVIDER_LABELS
MISSING_INPUT_HINTS = {
    "smartlead_signature": "no reply text",
    "leadmagic": "needs an email or LinkedIn URL",
    "prospeo": "needs a name, email, or LinkedIn URL",
    "airscale": "needs a LinkedIn URL",
    "fullenrich": "needs a LinkedIn URL, or full name + company",
}


def escape_mrkdwn(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def lead_display_name(lead: dict[str, Any]) -> str:
    name = " ".join(
        str(part).strip()
        for part in (lead.get("first_name"), lead.get("last_name"))
        if part not in (None, "")
    ).strip()
    if name:
        return name
    email = str(lead.get("email") or "").strip()
    return email or "Unknown lead"


class SpeedToLeadNotifier:
    """Formats and posts speed-to-lead Slack messages. No-ops when unconfigured."""

    def __init__(
        self,
        slack: SlackClient | None,
        *,
        channel_id: str | None,
        app_base_url: str | None = None,
    ) -> None:
        self._slack = slack
        self._channel_id = channel_id or None
        self._app_base_url = app_base_url.rstrip("/") if app_base_url else None

    @property
    def enabled(self) -> bool:
        return self._slack is not None and self._channel_id is not None

    @property
    def screen_url(self) -> str | None:
        if self._app_base_url is None:
            return None
        return f"{self._app_base_url}/speed-to-lead"

    async def post_alert(
        self,
        *,
        lead: dict[str, Any],
        campaign_name: str,
        reply_excerpt: str | None,
        sdr_label: str | None,
    ) -> str:
        if self._slack is None or self._channel_id is None:
            raise RuntimeError("Slack notifications are not configured")
        text, blocks = self.format_alert(
            lead=lead,
            campaign_name=campaign_name,
            reply_excerpt=reply_excerpt,
            sdr_label=sdr_label,
            screen_url=self.screen_url,
        )
        return await self._slack.post_message(self._channel_id, text, blocks=blocks)

    async def post_phone(self, *, thread_ts: str, phone: str, source: str) -> str:
        if self._slack is None or self._channel_id is None:
            raise RuntimeError("Slack notifications are not configured")
        text, blocks = self.format_phone(phone=phone, source=source)
        return await self._slack.post_message(
            self._channel_id, text, blocks=blocks, thread_ts=thread_ts
        )

    @staticmethod
    def format_alert(
        *,
        lead: dict[str, Any],
        campaign_name: str,
        reply_excerpt: str | None,
        sdr_label: str | None,
        screen_url: str | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        name = lead_display_name(lead)
        company = str(lead.get("company_name") or "").strip()
        who = f"{name} ({company})" if company else name
        text = f"New positive reply: {who} · {campaign_name}"

        title = "*New positive reply*"
        if screen_url:
            title += f" · <{screen_url}|Open speed to lead>"
        lines = [
            title,
            f"*Lead:* {escape_mrkdwn(who)}",
            f"*Campaign:* {escape_mrkdwn(campaign_name)}",
            f"*Assigned:* {escape_mrkdwn(sdr_label) if sdr_label else 'Unassigned'}",
        ]
        email = str(lead.get("email") or "").strip()
        if email and email != name:
            lines.insert(2, f"*Email:* {escape_mrkdwn(email)}")
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}
        ]
        if reply_excerpt:
            quoted = "\n".join(
                f"> {escape_mrkdwn(line)}" for line in reply_excerpt.splitlines() or [""]
            )
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": quoted}}
            )
        return text, blocks

    async def post_enrichment_summary(
        self, *, thread_ts: str, items: list[dict[str, Any]]
    ) -> str | None:
        if self._slack is None or self._channel_id is None:
            raise RuntimeError("Slack notifications are not configured")
        formatted = self.format_enrichment_summary(items=items)
        if formatted is None:
            return None
        text, blocks = formatted
        return await self._slack.post_message(
            self._channel_id, text, blocks=blocks, thread_ts=thread_ts
        )

    @staticmethod
    def format_enrichment_summary(
        *, items: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]] | None:
        """Thread reply for a finished run that did not yield a phone.

        Returns ``None`` when there is nothing useful to say (phone found, or the
        run has no items).
        """
        if not items:
            return None
        item = items[0]
        status = str(item.get("status") or "")
        if status == "enriched":
            return None
        if status == "skipped_existing":
            text = "Phone already on file; enrichment skipped."
            return text, [_section(f":telephone_receiver: {escape_mrkdwn(text)}")]
        if status == "skipped_active":
            text = "Another phone enrichment is already running for this lead."
            return text, [_section(f":hourglass: {escape_mrkdwn(text)}")]

        outcomes: list[tuple[str, str]] = []
        out_of_credits: list[str] = []
        for attempt in sorted(
            item.get("attempts") or [], key=lambda entry: int(entry.get("sequence") or 0)
        ):
            provider = str(attempt.get("provider") or "")
            label = PROVIDER_LABELS.get(provider, provider or "provider")
            outcome = _attempt_outcome(provider, attempt)
            if attempt.get("error_code") == INSUFFICIENT_CREDITS:
                out_of_credits.append(label)
            outcomes.append((label, outcome))

        headline = "No phone found."
        text = headline
        if outcomes:
            text += " " + "; ".join(f"{label}: {outcome}" for label, outcome in outcomes)
        lines = [f":mag: *{headline}*"]
        lines.extend(
            f"• *{escape_mrkdwn(label)}:* {escape_mrkdwn(outcome)}"
            for label, outcome in outcomes
        )
        if out_of_credits:
            lines.append(
                ":warning: Out of credits: " + ", ".join(map(escape_mrkdwn, out_of_credits))
            )
            text += ". Out of credits: " + ", ".join(out_of_credits)
        return text, [_section("\n".join(lines))]

    @staticmethod
    def format_phone(
        *, phone: str, source: str
    ) -> tuple[str, list[dict[str, Any]]]:
        source_label = SOURCE_LABELS.get(source, source)
        text = f"Phone found: {phone} (via {source_label})"
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":telephone_receiver: *Phone found:* `{escape_mrkdwn(phone)}`"
                        f" _(via {escape_mrkdwn(source_label)})_"
                    ),
                },
            }
        ]
        return text, blocks


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _attempt_outcome(provider: str, attempt: dict[str, Any]) -> str:
    status = str(attempt.get("status") or "")
    if status == "found":
        return "found"
    if status == "not_found":
        return "not found"
    if status == "skipped_no_input":
        hint = MISSING_INPUT_HINTS.get(provider)
        return f"skipped ({hint})" if hint else "skipped"
    if status in {"pending", "waiting", "in_progress"}:
        return "still pending"
    if attempt.get("error_code") == INSUFFICIENT_CREDITS:
        return "out of credits"
    reason = describe_provider_failure(
        status, attempt.get("error_code"), attempt.get("error_message")
    )
    return reason[:120]
