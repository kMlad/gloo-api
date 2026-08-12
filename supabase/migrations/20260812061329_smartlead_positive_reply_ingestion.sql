create table public.smartlead_campaigns (
    smartlead_campaign_id bigint primary key check (smartlead_campaign_id > 0),
    name text not null,
    enabled boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.leads (
    id uuid primary key default gen_random_uuid(),
    email text not null,
    email_normalized text not null unique,
    first_name text,
    last_name text,
    smartlead_phone_number text,
    company_name text,
    location text,
    website text,
    company_url text,
    linkedin_profile text,
    properties jsonb not null default '{}'::jsonb
        check (jsonb_typeof(properties) = 'object'),
    custom_properties jsonb not null default '{}'::jsonb
        check (jsonb_typeof(custom_properties) = 'object'),
    enriched_phone_number text,
    phone_source text check (
        phone_source is null or phone_source in (
            'smartlead_signature',
            'leadmagic',
            'prospeo',
            'airscale',
            'fullenrich'
        )
    ),
    source_observed_at timestamptz not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (email_normalized = lower(btrim(email_normalized)))
);

create table public.smartlead_conversations (
    id uuid primary key default gen_random_uuid(),
    lead_id uuid not null references public.leads(id) on delete cascade,
    smartlead_campaign_id bigint not null
        references public.smartlead_campaigns(smartlead_campaign_id) on delete restrict,
    smartlead_campaign_lead_map_id text not null,
    smartlead_lead_id text,
    positive_category_id bigint,
    positive_category_name text,
    qualified_at timestamptz not null,
    lead_properties jsonb not null default '{}'::jsonb
        check (jsonb_typeof(lead_properties) = 'object'),
    custom_properties jsonb not null default '{}'::jsonb
        check (jsonb_typeof(custom_properties) = 'object'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (smartlead_campaign_id, smartlead_campaign_lead_map_id)
);

create table public.smartlead_replies (
    id uuid primary key default gen_random_uuid(),
    conversation_id uuid not null
        references public.smartlead_conversations(id) on delete cascade,
    smartlead_message_id text,
    dedupe_key text not null unique,
    subject text,
    body text not null default '',
    sent_from text,
    sent_to text,
    received_at timestamptz not null,
    message_properties jsonb not null default '{}'::jsonb
        check (jsonb_typeof(message_properties) = 'object'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.smartlead_import_runs (
    id uuid primary key default gen_random_uuid(),
    status text not null check (
        status in ('running', 'succeeded', 'partial', 'failed', 'rejected')
    ),
    campaign_ids bigint[] not null default '{}',
    reply_time_from timestamptz,
    reply_time_to timestamptz,
    max_conversations integer not null default 1000 check (max_conversations > 0),
    qualifying_conversation_count integer not null default 0 check (qualifying_conversation_count >= 0),
    leads_processed integer not null default 0 check (leads_processed >= 0),
    conversations_processed integer not null default 0 check (conversations_processed >= 0),
    replies_processed integer not null default 0 check (replies_processed >= 0),
    errors jsonb not null default '[]'::jsonb
        check (jsonb_typeof(errors) = 'array'),
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index leads_source_observed_at_idx
    on public.leads (source_observed_at desc, id);
create index smartlead_conversations_lead_id_idx
    on public.smartlead_conversations (lead_id);
create index smartlead_conversations_qualified_at_idx
    on public.smartlead_conversations (qualified_at desc);
create index smartlead_replies_conversation_id_idx
    on public.smartlead_replies (conversation_id);
create index smartlead_replies_received_at_idx
    on public.smartlead_replies (received_at desc);
create unique index smartlead_import_runs_one_running_idx
    on public.smartlead_import_runs ((1)) where status = 'running';

alter table public.smartlead_campaigns enable row level security;
alter table public.leads enable row level security;
alter table public.smartlead_conversations enable row level security;
alter table public.smartlead_replies enable row level security;
alter table public.smartlead_import_runs enable row level security;

revoke all on table public.smartlead_campaigns from public, anon, authenticated;
revoke all on table public.leads from public, anon, authenticated;
revoke all on table public.smartlead_conversations from public, anon, authenticated;
revoke all on table public.smartlead_replies from public, anon, authenticated;
revoke all on table public.smartlead_import_runs from public, anon, authenticated;

grant select, insert, update, delete on table public.smartlead_campaigns to service_role;
grant select, insert, update, delete on table public.leads to service_role;
grant select, insert, update, delete on table public.smartlead_conversations to service_role;
grant select, insert, update, delete on table public.smartlead_replies to service_role;
grant select, insert, update, delete on table public.smartlead_import_runs to service_role;
