alter table public.leads
    add column linkedin_profile_normalized text;

alter table public.leads
    drop constraint if exists leads_email_or_phone_check;

alter table public.leads
    add constraint leads_email_phone_or_linkedin_check check (
        email_normalized is not null
        or phone_normalized is not null
        or linkedin_profile_normalized is not null
    );

create unique index leads_linkedin_profile_normalized_idx
    on public.leads (linkedin_profile_normalized)
    where linkedin_profile_normalized is not null;

alter table public.phone_enrichment_runs
    drop constraint if exists phone_enrichment_runs_source_import_run_id_fkey;

create table public.heyreach_campaigns (
    heyreach_campaign_id bigint primary key check (heyreach_campaign_id > 0),
    name text not null,
    enabled boolean not null default true,
    status text,
    last_synced_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.heyreach_conversations (
    id uuid primary key default gen_random_uuid(),
    lead_id uuid not null references public.leads(id) on delete cascade,
    heyreach_campaign_id bigint not null
        references public.heyreach_campaigns(heyreach_campaign_id) on delete restrict,
    heyreach_conversation_id text not null,
    heyreach_lead_id text,
    linkedin_account_id bigint,
    reply_type text not null default 'positive'
        check (reply_type in ('positive', 'ooo')),
    qualified_at timestamptz not null,
    lead_properties jsonb not null default '{}'::jsonb
        check (jsonb_typeof(lead_properties) = 'object'),
    custom_properties jsonb not null default '{}'::jsonb
        check (jsonb_typeof(custom_properties) = 'object'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (heyreach_campaign_id, heyreach_conversation_id)
);

create table public.heyreach_replies (
    id uuid primary key default gen_random_uuid(),
    conversation_id uuid not null
        references public.heyreach_conversations(id) on delete cascade,
    heyreach_message_id text,
    dedupe_key text not null unique,
    subject text,
    body text not null default '',
    sent_from text,
    sent_to text,
    received_at timestamptz not null,
    direction text not null default 'inbound'
        check (direction in ('inbound', 'outbound')),
    message_properties jsonb not null default '{}'::jsonb
        check (jsonb_typeof(message_properties) = 'object'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.heyreach_import_runs (
    id uuid primary key default gen_random_uuid(),
    status text not null check (
        status in ('queued', 'running', 'succeeded', 'partial', 'failed', 'rejected')
    ),
    campaign_ids bigint[] not null default '{}',
    reply_time_from timestamptz,
    reply_time_to timestamptz,
    requested_by uuid,
    idempotency_key text,
    max_conversations integer not null default 1000 check (max_conversations > 0),
    qualifying_conversation_count integer not null default 0
        check (qualifying_conversation_count >= 0),
    leads_processed integer not null default 0 check (leads_processed >= 0),
    conversations_processed integer not null default 0
        check (conversations_processed >= 0),
    replies_processed integer not null default 0 check (replies_processed >= 0),
    errors jsonb not null default '[]'::jsonb
        check (jsonb_typeof(errors) = 'array'),
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint heyreach_import_runs_idempotency_key_check check (
        idempotency_key is null
        or char_length(idempotency_key) between 8 and 128
    )
);

create unique index heyreach_import_runs_idempotency_key_idx
    on public.heyreach_import_runs (idempotency_key)
    where idempotency_key is not null;

create table public.heyreach_import_run_items (
    id uuid primary key default gen_random_uuid(),
    run_id uuid not null
        references public.heyreach_import_runs(id) on delete cascade,
    lead_id uuid not null references public.leads(id) on delete cascade,
    conversation_id uuid not null
        references public.heyreach_conversations(id) on delete cascade,
    heyreach_campaign_id bigint not null
        references public.heyreach_campaigns(heyreach_campaign_id)
        on delete restrict,
    created_at timestamptz not null default now(),
    unique (run_id, conversation_id)
);

create table public.heyreach_import_run_scopes (
    run_id uuid not null
        references public.heyreach_import_runs(id) on delete cascade,
    heyreach_campaign_id bigint not null
        references public.heyreach_campaigns(heyreach_campaign_id)
        on delete restrict,
    status text not null check (
        status in ('queued', 'running', 'succeeded', 'partial', 'failed', 'rejected')
    ),
    primary key (run_id, heyreach_campaign_id)
);

create unique index heyreach_import_run_scopes_one_active_idx
    on public.heyreach_import_run_scopes (heyreach_campaign_id)
    where status in ('queued', 'running');

create index heyreach_conversations_lead_id_idx
    on public.heyreach_conversations (lead_id);
create index heyreach_conversations_qualified_at_idx
    on public.heyreach_conversations (qualified_at desc);
create index heyreach_replies_conversation_id_idx
    on public.heyreach_replies (conversation_id);
create index heyreach_replies_received_at_idx
    on public.heyreach_replies (received_at desc);
create index heyreach_import_run_items_run_lead_idx
    on public.heyreach_import_run_items (run_id, lead_id);
create index heyreach_import_run_items_campaign_idx
    on public.heyreach_import_run_items (heyreach_campaign_id, created_at desc);
create index heyreach_import_run_scopes_campaign_idx
    on public.heyreach_import_run_scopes (heyreach_campaign_id);

create or replace function public.sync_heyreach_import_run_scopes()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
    if tg_op = 'INSERT' then
        insert into public.heyreach_import_run_scopes (
            run_id,
            heyreach_campaign_id,
            status
        )
        select
            new.id,
            campaign_id,
            new.status
        from unnest(new.campaign_ids) as campaign_id;
        return new;
    end if;

    if new.status is distinct from old.status then
        update public.heyreach_import_run_scopes
        set status = new.status
        where run_id = new.id;
    end if;
    return new;
end;
$$;

create trigger heyreach_import_runs_sync_scopes
    after insert or update of status on public.heyreach_import_runs
    for each row
    execute function public.sync_heyreach_import_run_scopes();

create or replace function public.latest_heyreach_imports(p_campaign_ids bigint[])
returns table (
    heyreach_campaign_id bigint,
    run jsonb
)
language sql
stable
security invoker
set search_path = ''
as $$
    select distinct on (scope.heyreach_campaign_id)
        scope.heyreach_campaign_id,
        to_jsonb(run.*) as run
    from public.heyreach_import_run_scopes as scope
    inner join public.heyreach_import_runs as run on run.id = scope.run_id
    where scope.heyreach_campaign_id = any(p_campaign_ids)
    order by
        scope.heyreach_campaign_id,
        run.created_at desc,
        run.id desc;
$$;

create or replace function public.upsert_heyreach_lead_conversation(
    p_lead jsonb,
    p_conversation jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
    stored_lead public.leads%rowtype;
    stored_conversation public.heyreach_conversations%rowtype;
    email_key text;
    linkedin_key text;
begin
    email_key := nullif(btrim(coalesce(p_lead ->> 'email_normalized', '')), '');
    linkedin_key := nullif(btrim(coalesce(p_lead ->> 'linkedin_profile_normalized', '')), '');

    if email_key is not null then
        select * into stored_lead
        from public.leads
        where email_normalized = email_key
        limit 1;
    end if;

    if stored_lead.id is null and linkedin_key is not null then
        select * into stored_lead
        from public.leads
        where linkedin_profile_normalized = linkedin_key
        limit 1;
    end if;

    if stored_lead.id is null then
        insert into public.leads (
            email,
            email_normalized,
            first_name,
            last_name,
            smartlead_phone_number,
            company_name,
            location,
            website,
            company_url,
            linkedin_profile,
            linkedin_profile_normalized,
            properties,
            custom_properties,
            source_observed_at,
            updated_at
        )
        values (
            nullif(p_lead ->> 'email', ''),
            email_key,
            p_lead ->> 'first_name',
            p_lead ->> 'last_name',
            p_lead ->> 'smartlead_phone_number',
            p_lead ->> 'company_name',
            p_lead ->> 'location',
            p_lead ->> 'website',
            p_lead ->> 'company_url',
            p_lead ->> 'linkedin_profile',
            linkedin_key,
            coalesce(p_lead -> 'properties', '{}'::jsonb),
            coalesce(p_lead -> 'custom_properties', '{}'::jsonb),
            (p_lead ->> 'source_observed_at')::timestamptz,
            now()
        )
        returning * into stored_lead;
    else
        update public.leads
        set
            email = coalesce(nullif(p_lead ->> 'email', ''), email),
            email_normalized = coalesce(email_key, email_normalized),
            first_name = coalesce(nullif(p_lead ->> 'first_name', ''), first_name),
            last_name = coalesce(nullif(p_lead ->> 'last_name', ''), last_name),
            smartlead_phone_number = coalesce(
                nullif(p_lead ->> 'smartlead_phone_number', ''),
                smartlead_phone_number
            ),
            company_name = coalesce(nullif(p_lead ->> 'company_name', ''), company_name),
            location = coalesce(nullif(p_lead ->> 'location', ''), location),
            website = coalesce(nullif(p_lead ->> 'website', ''), website),
            company_url = coalesce(nullif(p_lead ->> 'company_url', ''), company_url),
            linkedin_profile = coalesce(
                nullif(p_lead ->> 'linkedin_profile', ''),
                linkedin_profile
            ),
            linkedin_profile_normalized = coalesce(
                linkedin_key,
                linkedin_profile_normalized
            ),
            properties = coalesce(properties, '{}'::jsonb)
                || coalesce(p_lead -> 'properties', '{}'::jsonb),
            custom_properties = coalesce(custom_properties, '{}'::jsonb)
                || coalesce(p_lead -> 'custom_properties', '{}'::jsonb),
            source_observed_at = greatest(
                source_observed_at,
                (p_lead ->> 'source_observed_at')::timestamptz
            ),
            chat_refreshed_at = null,
            updated_at = now()
        where id = stored_lead.id
        returning * into stored_lead;
    end if;

    insert into public.heyreach_conversations (
        lead_id,
        heyreach_campaign_id,
        heyreach_conversation_id,
        heyreach_lead_id,
        linkedin_account_id,
        reply_type,
        qualified_at,
        lead_properties,
        custom_properties,
        updated_at
    )
    values (
        stored_lead.id,
        (p_conversation ->> 'heyreach_campaign_id')::bigint,
        p_conversation ->> 'heyreach_conversation_id',
        p_conversation ->> 'heyreach_lead_id',
        nullif(p_conversation ->> 'linkedin_account_id', '')::bigint,
        coalesce(p_conversation ->> 'reply_type', 'positive'),
        (p_conversation ->> 'qualified_at')::timestamptz,
        coalesce(p_conversation -> 'lead_properties', '{}'::jsonb),
        coalesce(p_conversation -> 'custom_properties', '{}'::jsonb),
        now()
    )
    on conflict (heyreach_campaign_id, heyreach_conversation_id) do update
    set
        lead_id = excluded.lead_id,
        heyreach_lead_id = excluded.heyreach_lead_id,
        linkedin_account_id = excluded.linkedin_account_id,
        reply_type = excluded.reply_type,
        qualified_at = excluded.qualified_at,
        lead_properties = excluded.lead_properties,
        custom_properties = excluded.custom_properties,
        updated_at = now()
    returning * into stored_conversation;

    update public.leads
    set
        chat_refreshed_at = null,
        updated_at = now()
    where id = stored_lead.id
    returning * into stored_lead;

    return jsonb_build_object(
        'lead', to_jsonb(stored_lead),
        'conversation', to_jsonb(stored_conversation)
    );
end;
$$;

alter table public.heyreach_campaigns enable row level security;
alter table public.heyreach_conversations enable row level security;
alter table public.heyreach_replies enable row level security;
alter table public.heyreach_import_runs enable row level security;
alter table public.heyreach_import_run_items enable row level security;
alter table public.heyreach_import_run_scopes enable row level security;

revoke all on table public.heyreach_campaigns
    from public, anon, authenticated;
revoke all on table public.heyreach_conversations
    from public, anon, authenticated;
revoke all on table public.heyreach_replies
    from public, anon, authenticated;
revoke all on table public.heyreach_import_runs
    from public, anon, authenticated;
revoke all on table public.heyreach_import_run_items
    from public, anon, authenticated;
revoke all on table public.heyreach_import_run_scopes
    from public, anon, authenticated;

revoke all on table public.heyreach_campaigns from service_role;
revoke all on table public.heyreach_conversations from service_role;
revoke all on table public.heyreach_replies from service_role;
revoke all on table public.heyreach_import_runs from service_role;
revoke all on table public.heyreach_import_run_items from service_role;
revoke all on table public.heyreach_import_run_scopes from service_role;

grant select, insert, update, delete on table public.heyreach_campaigns
    to service_role;
grant select, insert, update, delete on table public.heyreach_conversations
    to service_role;
grant select, insert, update, delete on table public.heyreach_replies
    to service_role;
grant select, insert, update, delete on table public.heyreach_import_runs
    to service_role;
grant select, insert, update, delete on table public.heyreach_import_run_items
    to service_role;
grant select, insert, update, delete on table public.heyreach_import_run_scopes
    to service_role;

revoke execute on function public.sync_heyreach_import_run_scopes()
    from public, anon, authenticated;
grant execute on function public.sync_heyreach_import_run_scopes() to service_role;

revoke execute on function public.latest_heyreach_imports(bigint[])
    from public, anon, authenticated;
grant execute on function public.latest_heyreach_imports(bigint[]) to service_role;

revoke execute on function public.upsert_heyreach_lead_conversation(jsonb, jsonb)
    from public, anon, authenticated;
grant execute on function public.upsert_heyreach_lead_conversation(jsonb, jsonb)
    to service_role;
