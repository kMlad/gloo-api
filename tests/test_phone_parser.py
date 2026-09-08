from app.phone_enrichment.parser import (
    extract_phone_from_replies,
    normalize_phone,
    reply_to_text,
)


def test_extracts_first_valid_international_phone_from_newest_reply() -> None:
    phone, raw = extract_phone_from_replies(
        [
            {
                "id": "old",
                "received_at": "2026-08-01T10:00:00Z",
                "body": "Old signature +44 20 7946 0958",
            },
            {
                "id": "new",
                "received_at": "2026-08-02T10:00:00Z",
                "body": "Thanks,\nPat\n+1 (415) 555-2671",
            },
        ]
    )

    assert phone == "+14155552671"
    assert raw == "+1 (415) 555-2671"


def test_html_and_quoted_history_are_removed_before_matching() -> None:
    body = """
    <div>Call me on 00 44 20 7946 0958</div>
    <blockquote>Old sender: +1 415 555 2671</blockquote>
    """
    assert "+1 415" not in reply_to_text(body)
    phone, _ = extract_phone_from_replies(
        [{"received_at": "2026-08-01T10:00:00Z", "body": body}]
    )
    assert phone == "+442079460958"


def test_national_and_invalid_numbers_are_rejected() -> None:
    assert normalize_phone("415 555 2671") is None
    assert normalize_phone("+1 123") is None
    phone, _ = extract_phone_from_replies(
        [
            {
                "received_at": "2026-08-01T10:00:00Z",
                "body": "Local: 415 555 2671\nOn Tue, Pat wrote:\n+1 415 555 2671",
            }
        ]
    )
    assert phone is None


def test_plain_text_outlook_and_prefixed_quotes_are_removed() -> None:
    outlook = "Reply only\n\nFrom: Old Sender\nSent: Monday\nTo: Pat\n+1 415 555 2671"
    prefixed = "Reply only\n> Old signature +44 20 7946 0958"
    assert "+1 415" not in reply_to_text(outlook)
    assert "+44 20" not in reply_to_text(prefixed)


def test_reply_to_text_drops_wrapped_gmail_attribution() -> None:
    body = (
        "Sure, call me on +1 415 555 2671\n"
        "Best,\n"
        "Pat\n"
        "\n"
        "On Mon, Sep 7, 2026 at 9:13 PM Bisera Loteska\n"
        "<bisera@example.com> wrote:\n"
        "> Hi Pat, my number is +44 20 7946 0958\n"
    )

    text = reply_to_text(body)

    assert "Sure, call me on +1 415 555 2671" in text
    assert "wrote:" not in text
    assert "7946" not in text


def test_reply_to_text_keeps_ordinary_sentences_starting_with_on() -> None:
    body = "On Monday we can talk.\nOn balance I am interested.\nBest, Pat"

    assert reply_to_text(body) == body


def test_reply_to_text_drops_truncated_gmail_attribution() -> None:
    # SmartLead's message-history API returns a truncated plain-text preview that
    # can end before "wrote:".
    body = (
        "Best,\xa0\n\nKiril\n\nOn Mon, Sep 7, 2026 at 9:13 PM Bisera Loteska\n"
        "<bisera@example.com [bisera@example.com]"
    )

    assert reply_to_text(body) == "Best,\xa0\n\nKiril\n"
