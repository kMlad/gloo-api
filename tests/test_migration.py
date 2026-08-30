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
        assert (
            f"revoke all on table public.{table} from public, anon, authenticated"
            in migration
        )
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
        assert (
            f"revoke all on table public.{table} from public, anon, authenticated"
            in migration
        )
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


def test_smartlead_lead_conversation_upsert_is_atomic_and_private() -> None:
    migration = next(
        Path("supabase/migrations").glob(
            "*_atomic_smartlead_lead_conversation.sql"
        )
    ).read_text()

    assert (
        "create or replace function public.upsert_smartlead_lead_conversation"
        in migration
    )
    assert "security invoker" in migration
    assert "insert into public.leads" in migration
    assert "insert into public.smartlead_conversations" in migration
    assert "on conflict (email_normalized)" in migration
    assert (
        "on conflict (smartlead_campaign_id, smartlead_campaign_lead_map_id)"
        in migration
    )
    assert "revoke execute on function" in migration
    assert "from public, anon, authenticated" in migration
    assert "grant execute on function" in migration
    assert "to service_role" in migration


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
        "status in ('queued', 'running', 'succeeded', 'partial', 'failed')" in migration
    )
    assert (
        "status in ('queued', 'running', 'succeeded', 'failed', 'skipped')" in migration
    )


def test_sheriff_rename_migration_updates_type_and_tables() -> None:
    migration = next(
        Path("supabase/migrations").glob("*_rename_claygent_to_sheriff.sql")
    ).read_text()
    assert "set type = 'sheriff'" in migration
    assert "where type = 'claygent'" in migration
    assert "check (type in ('text', 'boolean', 'sheriff'))" in migration
    assert "table_columns_sheriff_config_check" in migration
    assert "rename to table_sheriff_runs" in migration
    assert "rename to table_sheriff_run_items" in migration
    assert "table_sheriff_runs_table_id_idx" in migration
    assert "replace(rec.conname, 'claygent', 'sheriff')" in migration


def test_perplexity_usage_migration_is_private_and_split() -> None:
    migration = next(
        Path("supabase/migrations").glob("*_perplexity_usage.sql")
    ).read_text()
    assert "create table public.perplexity_usage" in migration
    assert (
        "operation text not null check (operation in ('expand', 'research'))"
        in migration
    )
    assert "model_cost numeric(12, 8)" in migration
    assert "tool_calls_cost numeric(12, 8)" in migration
    assert "total_cost numeric(12, 8)" in migration
    assert "on delete set null" in migration
    assert "perplexity_usage_created_at_idx" in migration
    assert "perplexity_usage_run_id_idx" in migration
    assert "perplexity_usage_table_id_created_at_idx" in migration
    assert "alter table public.perplexity_usage enable row level security" in migration
    assert (
        "revoke all on table public.perplexity_usage from public, anon, authenticated"
        in migration
    )
    assert "revoke all on table public.perplexity_usage from service_role" in migration
    assert (
        "grant select, insert, update, delete on table public.perplexity_usage to service_role"
        in migration
    )
    assert "to anon" not in migration
    assert "to authenticated" not in migration


def test_email_enrichment_columns_migration_is_private_and_typed() -> None:
    migration = next(
        Path("supabase/migrations").glob("*_email_enrichment_columns.sql")
    ).read_text()
    assert (
        "check (type in ('text', 'boolean', 'sheriff', 'email_enrichment'))"
        in migration
    )
    assert "table_columns_computed_config_check" in migration
    assert "type in ('sheriff', 'email_enrichment')" in migration
    tables = [
        "table_email_enrichment_runs",
        "table_email_enrichment_run_items",
        "table_email_enrichment_attempts",
    ]
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
    assert "not_found_count integer not null default 0" in migration
    assert "millionverifier" in migration
    assert "skipped_cached" in migration
    assert "to anon" not in migration
    assert "to authenticated" not in migration


def test_email_validation_columns_migration_is_private_and_typed() -> None:
    migration = next(
        Path("supabase/migrations").glob("*_email_validation_columns.sql")
    ).read_text()
    assert (
        "check (type in ('text', 'boolean', 'sheriff', 'email_enrichment', "
        "'email_validation'))" in migration
    )
    assert "table_columns_computed_config_check" in migration
    assert "type in ('sheriff', 'email_enrichment', 'email_validation')" in migration
    tables = [
        "table_email_validation_runs",
        "table_email_validation_run_items",
    ]
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


def test_lead_chat_history_migration_is_additive_and_constrained() -> None:
    migration = next(
        Path("supabase/migrations").glob("*_lead_chat_history.sql")
    ).read_text()

    assert "add column chat_refreshed_at timestamptz" in migration
    assert "add column direction text not null default 'inbound'" in migration
    assert "direction in ('inbound', 'outbound')" in migration
    assert "drop column" not in migration
