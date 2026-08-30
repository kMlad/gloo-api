alter table public.leads
    add column chat_refreshed_at timestamptz;

alter table public.smartlead_replies
    add column direction text not null default 'inbound';

alter table public.smartlead_replies
    add constraint smartlead_replies_direction_check check (
        direction in ('inbound', 'outbound')
    );
