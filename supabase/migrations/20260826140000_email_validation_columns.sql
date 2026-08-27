alter table public.table_columns
    drop constraint if exists table_columns_type_check;

alter table public.table_columns
    drop constraint if exists table_columns_computed_config_check;

alter table public.table_columns
    add constraint table_columns_type_check
        check (type in ('text', 'boolean', 'sheriff', 'email_enrichment', 'email_validation'));

alter table public.table_columns
    add constraint table_columns_computed_config_check
        check (
            (type not in ('sheriff', 'email_enrichment', 'email_validation') and config is null)
            or (
                type in ('sheriff', 'email_enrichment', 'email_validation')
                and config is not null
                and source_column_id is null
            )
        );

create table public.table_email_validation_runs (
    id uuid primary key default gen_random_uuid(),
    table_id uuid not null references public.tables(id) on delete cascade,
    column_id uuid not null references public.table_columns(id) on delete cascade,
    created_by uuid not null,
    status text not null check (
        status in ('queued', 'running', 'succeeded', 'partial', 'failed')
    ),
    row_ids jsonb
        check (row_ids is null or jsonb_typeof(row_ids) = 'array'),
    overwrite boolean not null default false,
    total_count integer not null default 0 check (total_count >= 0),
    succeeded_count integer not null default 0 check (succeeded_count >= 0),
    failed_count integer not null default 0 check (failed_count >= 0),
    skipped_count integer not null default 0 check (skipped_count >= 0),
    not_found_count integer not null default 0 check (not_found_count >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz
);

create table public.table_email_validation_run_items (
    id uuid primary key default gen_random_uuid(),
    run_id uuid not null
        references public.table_email_validation_runs(id) on delete cascade,
    row_id uuid not null references public.table_rows(id) on delete cascade,
    status text not null check (
        status in (
            'queued', 'running', 'succeeded', 'not_found', 'failed', 'skipped'
        )
    ),
    error_message text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (run_id, row_id)
);

create index table_email_validation_runs_table_id_idx
    on public.table_email_validation_runs (table_id);
create index table_email_validation_runs_column_id_idx
    on public.table_email_validation_runs (column_id);
create index table_email_validation_run_items_run_id_idx
    on public.table_email_validation_run_items (run_id);

alter table public.table_email_validation_runs enable row level security;
alter table public.table_email_validation_run_items enable row level security;

revoke all on table public.table_email_validation_runs from public, anon, authenticated;
revoke all on table public.table_email_validation_run_items from public, anon, authenticated;
revoke all on table public.table_email_validation_runs from service_role;
revoke all on table public.table_email_validation_run_items from service_role;

grant select, insert, update, delete on table public.table_email_validation_runs to service_role;
grant select, insert, update, delete on table public.table_email_validation_run_items to service_role;
