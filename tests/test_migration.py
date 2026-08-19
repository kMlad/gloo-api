from pathlib import Path


def test_migration_enables_rls_and_only_grants_service_role() -> None:
    migration = next(
        Path("supabase/migrations").glob("*_smartlead_positive_reply_ingestion.sql")
    ).read_text()
    tables = [
        "smartlead_campaigns",
        "leads",
        "smartlead_conversations",
        "smartlead_replies",
        "smartlead_import_runs",
    ]

    for table in tables:
        assert f"alter table public.{table} enable row level security" in migration
        assert f"revoke all on table public.{table} from public, anon, authenticated" in migration
        assert (
            f"grant select, insert, update, delete on table public.{table} to service_role"
            in migration
        )

    assert "to anon" not in migration
    assert "to authenticated" not in migration


def test_phone_enrichment_migration_is_private_and_idempotent() -> None:
    migration = next(
        Path("supabase/migrations").glob("*_phone_enrichment_waterfall.sql")
    ).read_text()
    tables = [
        "phone_enrichment_runs",
        "phone_enrichment_items",
        "phone_enrichment_attempts",
    ]
    for table in tables:
        assert f"alter table public.{table} enable row level security" in migration
        assert f"revoke all on table public.{table} from public, anon, authenticated" in migration
        assert f"revoke all on table public.{table} from service_role" in migration
        assert (
            f"grant select, insert, update, delete on table public.{table} to service_role"
            in migration
        )
    assert "idempotency_key text not null unique" in migration
    assert "phone_enrichment_items_one_active_per_lead_idx" in migration


def test_campaign_reply_types_migration_is_additive_and_constrained() -> None:
    migration = next(
        Path("supabase/migrations").glob("*_campaign_reply_types.sql")
    ).read_text()

    assert "add column reply_types text[] not null default" in migration
    assert "cardinality(reply_types) > 0" in migration
    assert "array['positive', 'ooo']::text[]" in migration
    assert "add column reply_type text default 'positive'" in migration
    assert "reply_type is null or reply_type in ('positive', 'ooo')" in migration
    assert "smartlead_conversations_reply_type_lead_id_idx" in migration
    assert "drop column positive_category" not in migration


def test_workbook_tables_migration_is_private_and_typed() -> None:
    migration = next(
        Path("supabase/migrations").glob("*_workbook_tables.sql")
    ).read_text()
    tables = ["tables", "table_columns", "table_rows"]
    for table in tables:
        assert f"alter table public.{table} enable row level security" in migration
        assert (
            f"revoke all on table public.{table} from public, anon, authenticated"
            in migration
        )
        assert f"revoke all on table public.{table} from service_role" in migration
        assert (
            f"grant select, insert, update, delete on table public.{table} to service_role"
            in migration
        )
    assert "to anon" not in migration
    assert "to authenticated" not in migration
    assert "type text not null check (type in ('text', 'boolean'))" in migration
    assert "unique (table_id, name)" in migration
    assert "unique (table_id, position)" in migration
    assert "table_rows_values_gin_idx" in migration
    assert "using gin (values)" in migration


def test_claygent_columns_migration_widens_type_and_stays_private() -> None:
    migration = next(
        Path("supabase/migrations").glob("*_claygent_columns.sql")
    ).read_text()
    assert "check (type in ('text', 'boolean', 'claygent'))" in migration
    assert "drop constraint if exists table_columns_type_check" in migration
    assert "add column config jsonb" in migration
    assert "add column source_column_id uuid" in migration
    assert "on delete cascade" in migration
    tables = ["table_claygent_runs", "table_claygent_run_items"]
    for table in tables:
        assert f"alter table public.{table} enable row level security" in migration
        assert (
            f"revoke all on table public.{table} from public, anon, authenticated"
            in migration
        )
        assert f"revoke all on table public.{table} from service_role" in migration
        assert (
            f"grant select, insert, update, delete on table public.{table} to service_role"
            in migration
        )
    assert "to anon" not in migration
    assert "to authenticated" not in migration
    assert "status in ('running', 'succeeded', 'partial', 'failed')" in migration


def test_claygent_queued_status_migration_widens_run_checks() -> None:
    migration = next(
        Path("supabase/migrations").glob("*_claygent_queued_status.sql")
    ).read_text()
    assert "queued" in migration
    assert "table_claygent_runs_status_check" in migration
    assert "table_claygent_run_items_status_check" in migration
    assert (
        "status in ('queued', 'running', 'succeeded', 'partial', 'failed')"
        in migration
    )
    assert (
        "status in ('queued', 'running', 'succeeded', 'failed', 'skipped')"
        in migration
    )
