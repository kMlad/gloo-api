import pytest

from app.speed_to_lead.notifications import SpeedToLeadNotifier, lead_display_name


class SlackStub:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def post_message(self, channel, text, *, blocks=None, thread_ts=None):
        self.messages.append(
            {
                "channel": channel,
                "text": text,
                "blocks": blocks,
                "thread_ts": thread_ts,
            }
        )
        return f"{len(self.messages)}.0"


def test_alert_formatting_includes_lead_campaign_sdr_link_and_excerpt() -> None:
    text, blocks = SpeedToLeadNotifier.format_alert(
        lead={
            "first_name": "Pat",
            "last_name": "Lee",
            "email": "pat@example.com",
            "company_name": "Acme & Co",
        },
        campaign_name="Q3 <Outbound>",
        reply_excerpt="Yes, let's talk.\nCall me > 5pm",
        sdr_label="sdr@gloo.example",
        screen_url="https://app.example.com/speed-to-lead",
    )

    assert text == "New positive reply: Pat Lee (Acme & Co) · Q3 <Outbound>"
    section = blocks[0]["text"]["text"]
    assert "<https://app.example.com/speed-to-lead|Open speed to lead>" in section
    assert "*Lead:* Pat Lee (Acme &amp; Co)" in section
    assert "*Email:* pat@example.com" in section
    assert "*Campaign:* Q3 &lt;Outbound&gt;" in section
    assert "*Assigned:* sdr@gloo.example" in section
    assert blocks[1]["text"]["text"] == "> Yes, let's talk.\n> Call me &gt; 5pm"


def test_alert_formatting_without_optional_fields() -> None:
    text, blocks = SpeedToLeadNotifier.format_alert(
        lead={"email": "pat@example.com"},
        campaign_name="Campaign",
        reply_excerpt=None,
        sdr_label=None,
        screen_url=None,
    )

    assert text == "New positive reply: pat@example.com · Campaign"
    section = blocks[0]["text"]["text"]
    assert section.startswith("*New positive reply*\n")
    assert "Open speed to lead" not in section
    assert "*Assigned:* Unassigned" in section
    assert "*Email:*" not in section
    assert len(blocks) == 1


def test_phone_formatting_labels_source() -> None:
    text, blocks = SpeedToLeadNotifier.format_phone(
        phone="+14155552671", source="smartlead_signature"
    )

    assert text == "Phone found: +14155552671 (via email signature)"
    assert "`+14155552671`" in blocks[0]["text"]["text"]
    assert "email signature" in blocks[0]["text"]["text"]


def test_lead_display_name_falls_back() -> None:
    assert lead_display_name({"first_name": " Pat ", "last_name": None}) == "Pat"
    assert lead_display_name({"email": "x@y.z"}) == "x@y.z"
    assert lead_display_name({}) == "Unknown lead"


def test_notifier_is_disabled_without_client_or_channel() -> None:
    assert SpeedToLeadNotifier(None, channel_id="C1").enabled is False
    assert SpeedToLeadNotifier(SlackStub(), channel_id=None).enabled is False
    assert SpeedToLeadNotifier(SlackStub(), channel_id="").enabled is False
    assert SpeedToLeadNotifier(SlackStub(), channel_id="C1").enabled is True


@pytest.mark.asyncio
async def test_notifier_posts_alert_and_threaded_phone() -> None:
    slack = SlackStub()
    notifier = SpeedToLeadNotifier(
        slack, channel_id="C1", app_base_url="https://app.example.com/"
    )

    ts = await notifier.post_alert(
        lead={"first_name": "Pat"},
        campaign_name="Campaign",
        reply_excerpt="Sure",
        sdr_label=None,
    )
    reply_ts = await notifier.post_phone(
        thread_ts=ts, phone="+14155552671", source="prospeo"
    )

    assert ts == "1.0"
    assert reply_ts == "2.0"
    assert slack.messages[0]["channel"] == "C1"
    assert slack.messages[0]["thread_ts"] is None
    assert "https://app.example.com/speed-to-lead" in (
        slack.messages[0]["blocks"][0]["text"]["text"]
    )
    assert slack.messages[1]["thread_ts"] == "1.0"
    assert slack.messages[1]["text"] == "Phone found: +14155552671 (via Prospeo)"


@pytest.mark.asyncio
async def test_unconfigured_notifier_refuses_to_post() -> None:
    notifier = SpeedToLeadNotifier(None, channel_id=None)
    with pytest.raises(RuntimeError):
        await notifier.post_alert(
            lead={}, campaign_name="c", reply_excerpt=None, sdr_label=None
        )


def _attempt(provider: str, sequence: int, status: str, **extra) -> dict:
    return {"provider": provider, "sequence": sequence, "status": status, **extra}


def test_enrichment_summary_lists_provider_outcomes_and_credit_warning() -> None:
    items = [
        {
            "status": "failed",
            "attempts": [
                _attempt("fullenrich", 5, "skipped_no_input"),
                _attempt("smartlead_signature", 1, "not_found"),
                _attempt("leadmagic", 2, "not_found"),
                _attempt(
                    "prospeo",
                    3,
                    "failed",
                    error_code="insufficient_credits",
                    error_message="Provider account has insufficient credits",
                ),
                _attempt(
                    "airscale",
                    4,
                    "failed",
                    error_code="insufficient_credits",
                    error_message="Provider account has insufficient credits",
                ),
            ],
        }
    ]

    formatted = SpeedToLeadNotifier.format_enrichment_summary(items=items)

    assert formatted is not None
    text, blocks = formatted
    assert text == (
        "No phone found. email signature: not found; LeadMagic: not found; "
        "Prospeo: out of credits; AirScale: out of credits; "
        "FullEnrich: skipped (needs a LinkedIn URL, or full name + company). "
        "Out of credits: Prospeo, AirScale"
    )
    section = blocks[0]["text"]["text"]
    assert section.startswith(":mag: *No phone found.*")
    assert "• *Prospeo:* out of credits" in section
    assert ":warning: Out of credits: Prospeo, AirScale" in section
    # Attempts are rendered in waterfall order regardless of input order.
    assert section.index("*email signature:*") < section.index("*FullEnrich:*")


def test_enrichment_summary_describes_other_failures() -> None:
    items = [
        {
            "status": "not_found",
            "attempts": [
                _attempt("leadmagic", 2, "rate_limited"),
                _attempt("prospeo", 3, "timed_out"),
                _attempt(
                    "airscale", 4, "failed", error_message="Provider <rejected> it"
                ),
            ],
        }
    ]

    formatted = SpeedToLeadNotifier.format_enrichment_summary(items=items)

    assert formatted is not None
    text, blocks = formatted
    assert "LeadMagic: rate limited" in text
    assert "Prospeo: timed out" in text
    assert "AirScale: Provider <rejected> it" in text
    assert "Provider &lt;rejected&gt; it" in blocks[0]["text"]["text"]
    assert "Out of credits" not in text


def test_enrichment_summary_is_silent_when_phone_was_found_or_no_items() -> None:
    assert SpeedToLeadNotifier.format_enrichment_summary(items=[]) is None
    assert (
        SpeedToLeadNotifier.format_enrichment_summary(
            items=[{"status": "enriched", "attempts": []}]
        )
        is None
    )


def test_enrichment_summary_explains_skipped_items() -> None:
    existing = SpeedToLeadNotifier.format_enrichment_summary(
        items=[{"status": "skipped_existing", "attempts": []}]
    )
    active = SpeedToLeadNotifier.format_enrichment_summary(
        items=[{"status": "skipped_active", "attempts": []}]
    )

    assert existing is not None
    assert existing[0] == "Phone already on file; enrichment skipped."
    assert active is not None
    assert "already running" in active[0]


@pytest.mark.asyncio
async def test_notifier_threads_enrichment_summary() -> None:
    slack = SlackStub()
    notifier = SpeedToLeadNotifier(slack, channel_id="C1")

    ts = await notifier.post_enrichment_summary(
        thread_ts="1.0",
        items=[{"status": "not_found", "attempts": [_attempt("leadmagic", 2, "not_found")]}],
    )
    silent = await notifier.post_enrichment_summary(thread_ts="1.0", items=[])

    assert ts == "1.0"
    assert silent is None
    assert len(slack.messages) == 1
    assert slack.messages[0]["thread_ts"] == "1.0"
    assert slack.messages[0]["text"] == "No phone found. LeadMagic: not found"
