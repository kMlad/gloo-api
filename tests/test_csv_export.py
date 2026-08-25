import csv
import io

from app.tables.csv_export import (
    content_disposition_attachment,
    csv_filename,
    format_csv_cell,
    render_csv,
)


def _read(content: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))


def test_format_csv_cell_handles_primitives_and_computed_values() -> None:
    assert format_csv_cell("text", None) == ""
    assert format_csv_cell("text", "") == ""
    assert format_csv_cell("boolean", True) == "true"
    assert format_csv_cell("boolean", False) == "false"
    assert format_csv_cell("text", "Acme") == "Acme"
    assert format_csv_cell("sheriff", {"status": "succeeded", "output": {"x": 1}}) == (
        "succeeded"
    )
    assert format_csv_cell(
        "email_enrichment",
        {"status": "succeeded", "email": "pat@acme.com"},
    ) == "pat@acme.com"
    assert format_csv_cell("email_enrichment", {"status": "not_found"}) == "not_found"
    assert format_csv_cell("text", ["a", "b"]) == '["a", "b"]'


def test_render_csv_includes_bom_and_quotes_commas() -> None:
    content = render_csv(["Company", "Active"], [["Acme, Inc", "true"], ["Globex", ""]])
    assert content.startswith("\ufeff".encode("utf-8"))
    assert _read(content) == [
        ["Company", "Active"],
        ["Acme, Inc", "true"],
        ["Globex", ""],
    ]


def test_render_csv_with_no_visible_columns_is_empty() -> None:
    assert render_csv([], [[]]) == "\ufeff".encode("utf-8")


def test_csv_filename_and_content_disposition() -> None:
    assert csv_filename("Outbound Aug") == "Outbound Aug.csv"
    assert csv_filename('  bad/"name".csv  ') == "bad-name-.csv"
    assert csv_filename("   ") == "table.csv"
    header = content_disposition_attachment("Café.csv")
    assert 'filename="Caf.csv"' in header
    assert "filename*=UTF-8''" in header
    assert "Caf%C3%A9.csv" in header
