import pytest

from app.tables.csv_import import (
    CsvImportError,
    parse_csv,
    table_name_from_filename,
)


def test_parse_csv_reads_headers_and_rows() -> None:
    parsed = parse_csv(b"Company,Active\nAcme,true\nGlobex,\n")
    assert parsed.headers == ["Company", "Active"]
    assert parsed.rows == [["Acme", "true"], ["Globex", ""]]


def test_parse_csv_strips_bom_and_header_whitespace() -> None:
    parsed = parse_csv("\ufeff Company , Name \n Acme , Pat \n".encode())
    assert parsed.headers == ["Company", "Name"]
    assert parsed.rows == [["Acme", "Pat"]]


def test_parse_csv_skips_blank_rows() -> None:
    parsed = parse_csv(b"Name\nPat\n\n  \nLee\n")
    assert parsed.rows == [["Pat"], ["Lee"]]


def test_parse_csv_rejects_empty_and_missing_headers() -> None:
    with pytest.raises(CsvImportError, match="empty"):
        parse_csv(b"")
    with pytest.raises(CsvImportError, match="header"):
        parse_csv(b"\n\n")
    with pytest.raises(CsvImportError, match="empty"):
        parse_csv(b"Name,\nPat,1\n")


def test_parse_csv_rejects_duplicate_headers() -> None:
    with pytest.raises(CsvImportError, match="duplicates"):
        parse_csv(b"Name,Name\nPat,Other\n")


def test_parse_csv_enforces_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tables import csv_import

    monkeypatch.setattr(csv_import, "MAX_FILE_BYTES", 8)
    with pytest.raises(CsvImportError, match="5 MB"):
        csv_import.parse_csv(b"Name,TooLong")

    monkeypatch.setattr(csv_import, "MAX_FILE_BYTES", 5 * 1024 * 1024)
    monkeypatch.setattr(csv_import, "MAX_COLUMNS", 2)
    with pytest.raises(CsvImportError, match="columns"):
        csv_import.parse_csv(b"A,B,C\n1,2,3\n")

    monkeypatch.setattr(csv_import, "MAX_COLUMNS", 50)
    monkeypatch.setattr(csv_import, "MAX_ROWS", 1)
    with pytest.raises(CsvImportError, match="data rows"):
        csv_import.parse_csv(b"Name\nPat\nLee\n")


def test_parse_csv_rejects_non_utf8() -> None:
    with pytest.raises(CsvImportError, match="UTF-8"):
        parse_csv(b"\xff\xfeName\n")


def test_table_name_from_filename() -> None:
    assert table_name_from_filename("leads.csv") == "leads"
    assert table_name_from_filename("/tmp/Outbound Aug.csv") == "Outbound Aug"
    assert table_name_from_filename("") == "Untitled"
    assert table_name_from_filename(None) == "Untitled"
    assert table_name_from_filename(".csv") == "Untitled"
