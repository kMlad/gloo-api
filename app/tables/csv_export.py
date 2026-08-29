import csv
import io
import json
import re
from typing import Any
from urllib.parse import quote

_UNSAFE_FILENAME = re.compile(r'[/\\"]+')


def format_csv_cell(column_type: str, value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        if column_type == "email_enrichment":
            email = value.get("email")
            if isinstance(email, str) and email.strip():
                return email.strip()
        if column_type == "email_validation":
            result = value.get("result")
            if isinstance(result, str) and result:
                return result
        status = value.get("status")
        if isinstance(status, str) and status:
            return status
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render_csv(headers: list[str], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    if headers:
        writer.writerow(headers)
        writer.writerows(rows)
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def csv_filename(table_name: str) -> str:
    stem = _UNSAFE_FILENAME.sub("-", table_name.strip()).strip(" .-") or "table"
    if not stem.lower().endswith(".csv"):
        stem = f"{stem}.csv"
    return stem


def content_disposition_attachment(filename: str) -> str:
    ascii_name = (
        filename.encode("ascii", "ignore").decode("ascii").replace('"', "").strip()
        or "table.csv"
    )
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
