alter table public.smartlead_campaigns
    add column status text,
    add column tags jsonb not null default '[]'::jsonb
        check (jsonb_typeof(tags) = 'array'),
    add column last_synced_at timestamptz;

alter table public.smartlead_import_runs
    add column reply_types text[] not null default array['positive']::text[],
    add column requested_by uuid,
    add column idempotency_key text,
    add column resolved_categories jsonb not null default '{}'::jsonb
        check (jsonb_typeof(resolved_categories) = 'object');

alter table public.smartlead_import_runs
    drop constraint if exists smartlead_import_runs_status_check;

alter table public.smartlead_import_runs
    add constraint smartlead_import_runs_status_check check (
        status in ('queued', 'running', 'succeeded', 'partial', 'failed', 'rejected')
    ),
    add constraint smartlead_import_runs_reply_types_check check (
        cardinality(reply_types) > 0
        and reply_types <@ array['positive', 'ooo']::text[]
        and cardinality(reply_types) =
            (case when 'positive' = any(reply_types) then 1 else 0 end) +
            (case when 'ooo' = any(reply_types) then 1 else 0 end)
    ),
    add constraint smartlead_import_runs_idempotency_key_check check (
        idempotency_key is null
        or char_length(idempotency_key) between 8 and 128
    );

create unique index smartlead_import_runs_idempotency_key_idx
    on public.smartlead_import_runs (idempotency_key)
    where idempotency_key is not null;

drop index if exists public.smartlead_import_runs_one_running_idx;

create unique index smartlead_import_runs_one_active_idx
    on public.smartlead_import_runs ((1))
    where status in ('queued', 'running');

create table public.smartlead_import_run_items (
    id uuid primary key default gen_random_uuid(),
    run_id uuid not null
        references public.smartlead_import_runs(id) on delete cascade,
    lead_id uuid not null references public.leads(id) on delete cascade,
    conversation_id uuid not null
        references public.smartlead_conversations(id) on delete cascade,
    smartlead_campaign_id bigint not null
        references public.smartlead_campaigns(smartlead_campaign_id)
        on delete restrict,
    reply_type text not null check (reply_type in ('positive', 'ooo')),
    created_at timestamptz not null default now(),
    unique (run_id, conversation_id)
);

create index smartlead_import_run_items_run_lead_idx
    on public.smartlead_import_run_items (run_id, lead_id);
create index smartlead_import_run_items_campaign_idx
    on public.smartlead_import_run_items (smartlead_campaign_id, created_at desc);

alter table public.smartlead_import_run_items enable row level security;
revoke all on table public.smartlead_import_run_items
    from public, anon, authenticated;
revoke all on table public.smartlead_import_run_items from service_role;
grant select, insert, update, delete on table public.smartlead_import_run_items
    to service_role;

alter table public.phone_enrichment_runs
    add column source_import_run_id uuid
        references public.smartlead_import_runs(id) on delete set null,
    add column created_by uuid;

alter table public.phone_enrichment_runs
    drop constraint if exists phone_enrichment_runs_selection_mode_check,
    drop constraint if exists phone_enrichment_runs_status_check,
    drop constraint if exists phone_enrichment_runs_requested_limit_check;

alter table public.phone_enrichment_runs
    add constraint phone_enrichment_runs_selection_mode_check check (
        selection_mode in ('selected', 'eligible', 'import_run')
    ),
    add constraint phone_enrichment_runs_status_check check (
        status in ('queued', 'running', 'waiting', 'succeeded', 'partial', 'failed')
    ),
    add constraint phone_enrichment_runs_requested_limit_check check (
        requested_limit between 1 and 10000
    );

alter table public.phone_enrichment_items
    drop constraint if exists phone_enrichment_items_status_check;

alter table public.phone_enrichment_items
    add constraint phone_enrichment_items_status_check check (
        status in (
            'queued', 'running', 'waiting', 'enriched', 'not_found',
            'skipped_existing', 'skipped_active', 'failed'
        )
    );

drop index if exists public.phone_enrichment_items_one_active_per_lead_idx;

create unique index phone_enrichment_items_one_active_per_lead_idx
    on public.phone_enrichment_items (lead_id)
    where status in ('queued', 'running', 'waiting');

create index phone_enrichment_runs_source_import_run_idx
    on public.phone_enrichment_runs (source_import_run_id)
    where source_import_run_id is not null;
