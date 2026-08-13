create table public.phone_enrichment_runs (
    id uuid primary key default gen_random_uuid(),
    idempotency_key text not null unique check (char_length(idempotency_key) between 8 and 128),
    request_fingerprint text not null,
    selection_mode text not null check (selection_mode in ('selected', 'eligible')),
    requested_lead_ids uuid[] not null default '{}',
    requested_limit integer not null check (requested_limit between 1 and 100),
    status text not null check (
        status in ('running', 'waiting', 'succeeded', 'partial', 'failed')
    ),
    leads_selected integer not null default 0 check (leads_selected >= 0),
    leads_enriched integer not null default 0 check (leads_enriched >= 0),
    leads_not_found integer not null default 0 check (leads_not_found >= 0),
    leads_skipped integer not null default 0 check (leads_skipped >= 0),
    leads_failed integer not null default 0 check (leads_failed >= 0),
    fullenrich_job_id text,
    errors jsonb not null default '[]'::jsonb
        check (jsonb_typeof(errors) = 'array'),
    last_reconciled_at timestamptz,
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.phone_enrichment_items (
    id uuid primary key default gen_random_uuid(),
    run_id uuid not null references public.phone_enrichment_runs(id) on delete cascade,
    lead_id uuid not null references public.leads(id) on delete cascade,
    status text not null check (
        status in (
            'running', 'waiting', 'enriched', 'not_found',
            'skipped_existing', 'skipped_active', 'failed'
        )
    ),
    final_phone_number text,
    final_source text check (
        final_source is null or final_source in (
            'smartlead_signature', 'leadmagic', 'prospeo', 'airscale', 'fullenrich'
        )
    ),
    had_provider_error boolean not null default false,
    error_message text,
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (run_id, lead_id)
);

create table public.phone_enrichment_attempts (
    id uuid primary key default gen_random_uuid(),
    run_id uuid not null references public.phone_enrichment_runs(id) on delete cascade,
    item_id uuid not null references public.phone_enrichment_items(id) on delete cascade,
    lead_id uuid not null references public.leads(id) on delete cascade,
    provider text not null check (
        provider in (
            'smartlead_signature', 'leadmagic', 'prospeo', 'airscale', 'fullenrich'
        )
    ),
    sequence smallint not null check (sequence between 1 and 5),
    status text not null check (
        status in (
            'pending', 'in_progress', 'waiting', 'found', 'not_found',
            'skipped_no_input', 'rate_limited', 'timed_out', 'failed'
        )
    ),
    request_payload jsonb not null default '{}'::jsonb
        check (jsonb_typeof(request_payload) = 'object'),
    response_payload jsonb,
    response_headers jsonb not null default '{}'::jsonb
        check (jsonb_typeof(response_headers) = 'object'),
    http_status integer check (http_status is null or http_status between 100 and 599),
    external_request_id text,
    phone_candidate text,
    error_code text,
    error_message text,
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (run_id, lead_id, provider)
);

create index phone_enrichment_runs_started_at_idx
    on public.phone_enrichment_runs (started_at desc, id);
create index phone_enrichment_items_run_id_idx
    on public.phone_enrichment_items (run_id, created_at, id);
create index phone_enrichment_items_lead_id_idx
    on public.phone_enrichment_items (lead_id);
create unique index phone_enrichment_items_one_active_per_lead_idx
    on public.phone_enrichment_items (lead_id)
    where status in ('running', 'waiting');
create index phone_enrichment_attempts_run_id_idx
    on public.phone_enrichment_attempts (run_id, sequence, created_at, id);
create index phone_enrichment_attempts_item_id_idx
    on public.phone_enrichment_attempts (item_id, sequence);
create index phone_enrichment_attempts_external_request_id_idx
    on public.phone_enrichment_attempts (external_request_id)
    where external_request_id is not null;

alter table public.phone_enrichment_runs enable row level security;
alter table public.phone_enrichment_items enable row level security;
alter table public.phone_enrichment_attempts enable row level security;

revoke all on table public.phone_enrichment_runs from public, anon, authenticated;
revoke all on table public.phone_enrichment_items from public, anon, authenticated;
revoke all on table public.phone_enrichment_attempts from public, anon, authenticated;
revoke all on table public.phone_enrichment_runs from service_role;
revoke all on table public.phone_enrichment_items from service_role;
revoke all on table public.phone_enrichment_attempts from service_role;

grant select, insert, update, delete on table public.phone_enrichment_runs to service_role;
grant select, insert, update, delete on table public.phone_enrichment_items to service_role;
grant select, insert, update, delete on table public.phone_enrichment_attempts to service_role;
