create table public.smartlead_import_run_scopes (
    run_id uuid not null
        references public.smartlead_import_runs(id) on delete cascade,
    smartlead_campaign_id bigint not null
        references public.smartlead_campaigns(smartlead_campaign_id)
        on delete restrict,
    reply_type text not null check (reply_type in ('positive', 'ooo')),
    status text not null check (
        status in ('queued', 'running', 'succeeded', 'partial', 'failed', 'rejected')
    ),
    primary key (run_id, smartlead_campaign_id, reply_type)
);

create index smartlead_import_run_scopes_campaign_reply_idx
    on public.smartlead_import_run_scopes (smartlead_campaign_id, reply_type);

insert into public.smartlead_import_run_scopes (
    run_id,
    smartlead_campaign_id,
    reply_type,
    status
)
select
    run.id,
    campaign_id,
    reply_type,
    run.status
from public.smartlead_import_runs as run
cross join unnest(run.campaign_ids) as campaign_id
cross join unnest(run.reply_types) as reply_type
on conflict do nothing;

create unique index smartlead_import_run_scopes_one_active_idx
    on public.smartlead_import_run_scopes (smartlead_campaign_id, reply_type)
    where status in ('queued', 'running');

create or replace function public.sync_smartlead_import_run_scopes()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
    if tg_op = 'INSERT' then
        insert into public.smartlead_import_run_scopes (
            run_id,
            smartlead_campaign_id,
            reply_type,
            status
        )
        select
            new.id,
            campaign_id,
            reply_type,
            new.status
        from unnest(new.campaign_ids) as campaign_id
        cross join unnest(new.reply_types) as reply_type;
        return new;
    end if;

    if new.status is distinct from old.status then
        update public.smartlead_import_run_scopes
        set status = new.status
        where run_id = new.id;
    end if;
    return new;
end;
$$;

create trigger smartlead_import_runs_sync_scopes
    after insert or update of status on public.smartlead_import_runs
    for each row
    execute function public.sync_smartlead_import_run_scopes();

drop index if exists public.smartlead_import_runs_one_active_idx;

create or replace function public.latest_smartlead_imports(p_campaign_ids bigint[])
returns table (
    smartlead_campaign_id bigint,
    reply_type text,
    run jsonb
)
language sql
stable
security invoker
set search_path = ''
as $$
    select distinct on (scope.smartlead_campaign_id, scope.reply_type)
        scope.smartlead_campaign_id,
        scope.reply_type,
        to_jsonb(run.*) as run
    from public.smartlead_import_run_scopes as scope
    inner join public.smartlead_import_runs as run on run.id = scope.run_id
    where scope.smartlead_campaign_id = any(p_campaign_ids)
    order by
        scope.smartlead_campaign_id,
        scope.reply_type,
        run.created_at desc,
        run.id desc;
$$;

alter table public.smartlead_import_run_scopes enable row level security;
revoke all on table public.smartlead_import_run_scopes
    from public, anon, authenticated;
revoke all on table public.smartlead_import_run_scopes from service_role;
grant select, insert, update, delete on table public.smartlead_import_run_scopes
    to service_role;

revoke execute on function public.sync_smartlead_import_run_scopes()
    from public, anon, authenticated;
grant execute on function public.sync_smartlead_import_run_scopes() to service_role;

revoke execute on function public.latest_smartlead_imports(bigint[])
    from public, anon, authenticated;
grant execute on function public.latest_smartlead_imports(bigint[]) to service_role;

create unique index phone_enrichment_runs_one_active_per_import_idx
    on public.phone_enrichment_runs (source_import_run_id)
    where source_import_run_id is not null
        and status in ('queued', 'running', 'waiting');
