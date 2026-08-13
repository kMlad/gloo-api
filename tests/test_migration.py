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
