alter table public.smartlead_campaigns
    add column reply_types text[] not null default array['positive']::text[];

alter table public.smartlead_campaigns
    add constraint smartlead_campaigns_reply_types_check check (
        cardinality(reply_types) > 0
        and reply_types <@ array['positive', 'ooo']::text[]
        and cardinality(reply_types) =
            (case when 'positive' = any(reply_types) then 1 else 0 end) +
            (case when 'ooo' = any(reply_types) then 1 else 0 end)
    );

alter table public.smartlead_conversations
    add column reply_type text default 'positive';

alter table public.smartlead_conversations
    add constraint smartlead_conversations_reply_type_check check (
        reply_type is null or reply_type in ('positive', 'ooo')
    );

create index smartlead_conversations_reply_type_lead_id_idx
    on public.smartlead_conversations (reply_type, lead_id)
    where reply_type is not null;
