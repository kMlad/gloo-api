alter table public.smartlead_campaigns
    add column speed_to_lead_enabled boolean not null default false,
    add column speed_to_lead_sdr_id uuid references auth.users(id) on delete restrict,
    add column smartlead_webhook_id text;

alter table public.smartlead_campaigns
    add constraint smartlead_campaigns_speed_to_lead_sdr_check check (
        not speed_to_lead_enabled or speed_to_lead_sdr_id is not null
    );

create index smartlead_campaigns_speed_to_lead_sdr_idx
    on public.smartlead_campaigns (speed_to_lead_sdr_id)
    where speed_to_lead_sdr_id is not null;

create table public.speed_to_lead_events (
    id uuid primary key default gen_random_uuid(),
    platform text not null check (platform in ('smartlead', 'heyreach')),
    lead_id uuid not null references public.leads(id) on delete cascade,
    smartlead_campaign_id bigint
        references public.smartlead_campaigns(smartlead_campaign_id) on delete restrict,
    heyreach_campaign_id bigint
        references public.heyreach_campaigns(heyreach_campaign_id) on delete restrict,
    conversation_id uuid not null,
    category_id bigint,
    category_name text,
    reply_excerpt text,
    replied_at timestamptz not null,
    dedupe_key text not null unique,
    enrichment_run_id uuid
        references public.phone_enrichment_runs(id) on delete set null,
    notification_status text not null default 'pending' check (
        notification_status in ('pending', 'sent', 'failed', 'skipped')
    ),
    notification_error text,
    slack_message_ts text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint speed_to_lead_events_platform_campaign_check check (
        (
            platform = 'smartlead'
            and smartlead_campaign_id is not null
            and heyreach_campaign_id is null
        )
        or (
            platform = 'heyreach'
            and heyreach_campaign_id is not null
            and smartlead_campaign_id is null
        )
    )
);

create index speed_to_lead_events_lead_id_idx
    on public.speed_to_lead_events (lead_id);

create index speed_to_lead_events_replied_at_idx
    on public.speed_to_lead_events (replied_at desc, id);

alter table public.speed_to_lead_events enable row level security;

revoke all on table public.speed_to_lead_events from public, anon, authenticated;
revoke all on table public.speed_to_lead_events from service_role;

grant select, insert, update, delete on table public.speed_to_lead_events
    to service_role;
