create table public.perplexity_usage (
    id uuid primary key default gen_random_uuid(),
    operation text not null check (operation in ('expand', 'research')),
    model text not null,
    table_id uuid references public.tables(id) on delete set null,
    column_id uuid references public.table_columns(id) on delete set null,
    run_id uuid references public.table_sheriff_runs(id) on delete set null,
    run_item_id uuid references public.table_sheriff_run_items(id) on delete set null,
    perplexity_response_id text,
    input_tokens integer,
    output_tokens integer,
    total_tokens integer,
    input_cost numeric(12, 8),
    output_cost numeric(12, 8),
    tool_calls_cost numeric(12, 8),
    cache_creation_cost numeric(12, 8),
    cache_read_cost numeric(12, 8),
    model_cost numeric(12, 8),
    total_cost numeric(12, 8),
    tool_calls_details jsonb
        check (tool_calls_details is null or jsonb_typeof(tool_calls_details) = 'object'),
    usage_raw jsonb
        check (usage_raw is null or jsonb_typeof(usage_raw) = 'object'),
    created_at timestamptz not null default now()
);

create index perplexity_usage_created_at_idx
    on public.perplexity_usage (created_at desc, id);
create index perplexity_usage_run_id_idx
    on public.perplexity_usage (run_id);
create index perplexity_usage_table_id_created_at_idx
    on public.perplexity_usage (table_id, created_at desc);

alter table public.perplexity_usage enable row level security;

revoke all on table public.perplexity_usage from public, anon, authenticated;
revoke all on table public.perplexity_usage from service_role;

grant select, insert, update, delete on table public.perplexity_usage to service_role;
