import html
import re
from html.parser import HTMLParser
from typing import Any

import phonenumbers
from phonenumbers import PhoneNumberFormat, NumberParseException

_PHONE_CANDIDATE = re.compile(
    r"(?<![\w])(?:\+|00)[ \t]*\d(?:[\d \t()./\-]{5,}\d)"
    r"(?:[ \t]*(?:x|ext\.?|extension)[ \t]*\d{1,6})?",
    re.IGNORECASE,
)
_QUOTED_HISTORY = [
    re.compile(r"^\s*On\s+.+\bwrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*_{5,}\s*$"),
]


class _ReplyHTMLParser(HTMLParser):
    _ignored_tags = {"blockquote", "script", "style"}
    _line_break_tags = {"br", "div", "p", "li", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self._ignored_tags:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and tag in self._line_break_tags:
            self.parts.append("\n")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if self._ignored_depth == 0 and tag.casefold() in self._line_break_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._ignored_tags and self._ignored_depth:
            self._ignored_depth -= 1
        elif self._ignored_depth == 0 and tag in self._line_break_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)


def reply_to_text(body: str) -> str:
    if re.search(r"<\s*(?:html|body|div|p|br|table|blockquote)\b", body, re.I):
        parser = _ReplyHTMLParser()
        parser.feed(body)
        parser.close()
        text = "".join(parser.parts)
    else:
        text = html.unescape(body)

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    for index, line in enumerate(lines):
        if any(pattern.match(line) for pattern in _QUOTED_HISTORY):
            break
        if line.lstrip().startswith(">"):
            break
        if re.match(r"^\s*From:\s+", line, re.IGNORECASE):
            following = "\n".join(lines[index + 1 : index + 5])
            if re.search(r"^\s*(?:Sent|Date|To|Subject):", following, re.I | re.M):
                break
        kept.append(line)
    return "\n".join(kept)


def normalize_phone(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.startswith("00"):
        candidate = "+" + candidate[2:].lstrip()
    if not candidate.startswith("+"):
        return None
    try:
        parsed = phonenumbers.parse(candidate, None)
    except NumberParseException:
        return None
    if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(
        parsed
    ):
        return None
    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


def extract_phone_from_replies(replies: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    ordered = sorted(
        replies,
        key=lambda reply: str(reply.get("received_at") or ""),
        reverse=True,
    )
    for reply in ordered:
        body = reply.get("body")
        if not isinstance(body, str) or not body.strip():
            continue
        text = reply_to_text(body)
        for match in _PHONE_CANDIDATE.finditer(text):
            raw_candidate = match.group(0)
            phone = normalize_phone(raw_candidate)
            if phone is not None:
                return phone, raw_candidate
    return None, None
