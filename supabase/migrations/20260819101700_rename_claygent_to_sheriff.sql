alter table public.table_columns
    drop constraint if exists table_columns_type_check;

alter table public.table_columns
    drop constraint if exists table_columns_claygent_config_check;

update public.table_columns
    set type = 'sheriff'
    where type = 'claygent';

alter table public.table_columns
    add constraint table_columns_type_check
        check (type in ('text', 'boolean', 'sheriff'));

alter table public.table_columns
    add constraint table_columns_sheriff_config_check
        check (
            (type <> 'sheriff' and config is null)
            or (
                type = 'sheriff'
                and config is not null
                and source_column_id is null
            )
        );

alter table public.table_claygent_runs rename to table_sheriff_runs;
alter table public.table_claygent_run_items rename to table_sheriff_run_items;

alter index if exists public.table_claygent_runs_table_id_idx
    rename to table_sheriff_runs_table_id_idx;
alter index if exists public.table_claygent_runs_column_id_idx
    rename to table_sheriff_runs_column_id_idx;
alter index if exists public.table_claygent_run_items_run_id_idx
    rename to table_sheriff_run_items_run_id_idx;

do $$
declare
    rec record;
begin
    for rec in
        select c.relname as table_name, con.conname
        from pg_constraint con
        join pg_class c on c.oid = con.conrelid
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public'
          and con.conname like '%claygent%'
    loop
        execute format(
            'alter table public.%I rename constraint %I to %I',
            rec.table_name,
            rec.conname,
            replace(rec.conname, 'claygent', 'sheriff')
        );
    end loop;
end $$;
