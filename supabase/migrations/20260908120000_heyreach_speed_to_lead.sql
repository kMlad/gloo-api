alter table public.heyreach_campaigns
    add column speed_to_lead_enabled boolean not null default false,
    add column speed_to_lead_sdr_id uuid references auth.users(id) on delete restrict,
    add column heyreach_webhook_id text;

alter table public.heyreach_campaigns
    add constraint heyreach_campaigns_speed_to_lead_sdr_check check (
        not speed_to_lead_enabled or speed_to_lead_sdr_id is not null
    );

create index heyreach_campaigns_speed_to_lead_sdr_idx
    on public.heyreach_campaigns (speed_to_lead_sdr_id)
    where speed_to_lead_sdr_id is not null;
