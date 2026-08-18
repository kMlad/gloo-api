import csv
import io
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_COLUMNS = 50
MAX_ROWS = 10_000
DEFAULT_TABLE_NAME = "Untitled"


class CsvImportError(Exception):
    pass


@dataclass(frozen=True)
class ParsedCsv:
    headers: list[str]
    rows: list[list[str]]


def table_name_from_filename(filename: str | None) -> str:
    if not filename:
        return DEFAULT_TABLE_NAME
    stem = PurePosixPath(filename).name
    if stem.lower().endswith(".csv"):
        stem = stem[: -len(".csv")]
    name = stem.strip()
    return name or DEFAULT_TABLE_NAME


def parse_csv(content: bytes) -> ParsedCsv:
    if len(content) > MAX_FILE_BYTES:
        raise CsvImportError("CSV file must be at most 5 MB")
    if not content:
        raise CsvImportError("CSV file is empty")

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CsvImportError("CSV file must be UTF-8 encoded") from error

    reader = csv.reader(io.StringIO(text))
    try:
        raw_headers = next(reader)
    except StopIteration as error:
        raise CsvImportError("CSV file must include a header row") from error

    headers = [header.strip() for header in raw_headers]
    if not headers or all(header == "" for header in headers):
        raise CsvImportError("CSV file must include a header row")
    if any(header == "" for header in headers):
        raise CsvImportError("CSV headers must not be empty")
    if len(headers) > MAX_COLUMNS:
        raise CsvImportError(f"CSV may contain at most {MAX_COLUMNS} columns")
    if len(set(headers)) != len(headers):
        raise CsvImportError("CSV headers must not contain duplicates")

    rows: list[list[str]] = []
    for raw_row in reader:
        cells = [
            raw_row[index].strip() if index < len(raw_row) else ""
            for index in range(len(headers))
        ]
        if all(cell == "" for cell in cells):
            continue
        rows.append(cells)
        if len(rows) > MAX_ROWS:
            raise CsvImportError(f"CSV may contain at most {MAX_ROWS} data rows")

    return ParsedCsv(headers=headers, rows=rows)
