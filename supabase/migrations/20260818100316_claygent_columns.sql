alter table public.table_columns
    drop constraint if exists table_columns_type_check;

alter table public.table_columns
    add constraint table_columns_type_check
        check (type in ('text', 'boolean', 'claygent'));

alter table public.table_columns
    add column config jsonb
        check (config is null or jsonb_typeof(config) = 'object'),
    add column source_column_id uuid
        references public.table_columns(id) on delete cascade,
    add column source_field text
        check (source_field is null or char_length(btrim(source_field)) > 0);

alter table public.table_columns
    add constraint table_columns_source_pair_check
        check ((source_column_id is null) = (source_field is null));

alter table public.table_columns
    add constraint table_columns_claygent_config_check
        check (
            (type <> 'claygent' and config is null)
            or (
                type = 'claygent'
                and config is not null
                and source_column_id is null
            )
        );

alter table public.table_columns
    add constraint table_columns_source_type_check
        check (
            source_column_id is null
            or type in ('text', 'boolean')
        );

create index table_columns_source_column_id_idx
    on public.table_columns (source_column_id);

create table public.table_claygent_runs (
    id uuid primary key default gen_random_uuid(),
    table_id uuid not null references public.tables(id) on delete cascade,
    column_id uuid not null references public.table_columns(id) on delete cascade,
    created_by uuid not null,
    status text not null check (status in ('running', 'succeeded', 'partial', 'failed')),
    row_ids jsonb
        check (row_ids is null or jsonb_typeof(row_ids) = 'array'),
    overwrite boolean not null default false,
    total_count integer not null default 0 check (total_count >= 0),
    succeeded_count integer not null default 0 check (succeeded_count >= 0),
    failed_count integer not null default 0 check (failed_count >= 0),
    skipped_count integer not null default 0 check (skipped_count >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz
);

create table public.table_claygent_run_items (
    id uuid primary key default gen_random_uuid(),
    run_id uuid not null
        references public.table_claygent_runs(id) on delete cascade,
    row_id uuid not null references public.table_rows(id) on delete cascade,
    status text not null
        check (status in ('running', 'succeeded', 'failed', 'skipped')),
    error_message text,
    model_response jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (run_id, row_id)
);

create index table_claygent_runs_table_id_idx
    on public.table_claygent_runs (table_id);
create index table_claygent_runs_column_id_idx
    on public.table_claygent_runs (column_id);
create index table_claygent_run_items_run_id_idx
    on public.table_claygent_run_items (run_id);

alter table public.table_claygent_runs enable row level security;
alter table public.table_claygent_run_items enable row level security;

revoke all on table public.table_claygent_runs from public, anon, authenticated;
revoke all on table public.table_claygent_run_items from public, anon, authenticated;
revoke all on table public.table_claygent_runs from service_role;
revoke all on table public.table_claygent_run_items from service_role;

grant select, insert, update, delete on table public.table_claygent_runs to service_role;
grant select, insert, update, delete on table public.table_claygent_run_items to service_role;
