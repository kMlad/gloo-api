create extension if not exists pg_trgm with schema extensions;

create index leads_location_idx
    on public.leads (location)
    where location is not null and btrim(location) <> '';

create index leads_location_trgm_idx
    on public.leads
    using gin (location extensions.gin_trgm_ops)
    where location is not null;

create or replace function public.list_lead_locations(
    p_query text default null,
    p_assigned_sdr_id uuid default null,
    p_limit integer default 50,
    p_offset integer default 0
)
returns jsonb
language sql
stable
security invoker
set search_path = ''
as $$
    with filtered as (
        select
            location,
            count(*)::bigint as lead_count
        from public.leads
        where location is not null
            and btrim(location) <> ''
            and (
                p_assigned_sdr_id is null
                or assigned_sdr_id = p_assigned_sdr_id
            )
            and (
                p_query is null
                or btrim(p_query) = ''
                or location ilike
                    '%' || replace(
                        replace(
                            replace(btrim(p_query), '\', '\\'),
                            '%',
                            '\%'
                        ),
                        '_',
                        '\_'
                    ) || '%' escape '\'
            )
        group by location
    )
    select jsonb_build_object(
        'items', coalesce(
            (
                select jsonb_agg(
                    jsonb_build_object(
                        'location', page.location,
                        'lead_count', page.lead_count
                    )
                    order by page.location
                )
                from (
                    select location, lead_count
                    from filtered
                    order by location
                    limit greatest(1, least(coalesce(p_limit, 50), 500))
                    offset greatest(coalesce(p_offset, 0), 0)
                ) as page
            ),
            '[]'::jsonb
        ),
        'total', (select count(*) from filtered)
    );
$$;

revoke execute on function public.list_lead_locations(text, uuid, integer, integer)
    from public, anon, authenticated;
grant execute on function public.list_lead_locations(text, uuid, integer, integer)
    to service_role;
