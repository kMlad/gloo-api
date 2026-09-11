create table public.sdr_settings (
    user_id uuid primary key references auth.users(id) on delete cascade,
    slack_channel_id text,
    timezone text not null default 'Europe/Skopje',
    work_days smallint[] not null default '{1,2,3,4,5}',
    work_start time not null default '09:00',
    work_end time not null default '18:00',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint sdr_settings_slack_channel_id_check check (
        slack_channel_id is null or slack_channel_id ~ '^[CGD][A-Z0-9]+$'
    ),
    constraint sdr_settings_timezone_check check (timezone <> ''),
    constraint sdr_settings_work_days_check check (
        cardinality(work_days) > 0
        and work_days <@ '{1,2,3,4,5,6,7}'::smallint[]
    ),
    constraint sdr_settings_work_window_check check (work_start < work_end)
);

alter table public.sdr_settings enable row level security;

revoke all on table public.sdr_settings from public, anon, authenticated;
revoke all on table public.sdr_settings from service_role;

grant select, insert, update, delete on table public.sdr_settings
    to service_role;

alter table public.speed_to_lead_events
    add column slack_channel_id text,
    add column enrichment_skipped_reason text;

alter table public.speed_to_lead_events
    add constraint speed_to_lead_events_enrichment_skipped_reason_check check (
        enrichment_skipped_reason is null
        or enrichment_skipped_reason in ('outside_working_hours')
    );
