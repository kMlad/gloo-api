create table public.tables (
    id uuid primary key default gen_random_uuid(),
    name text not null check (char_length(btrim(name)) > 0),
    created_by uuid not null,
    filters jsonb not null default '[]'::jsonb
        check (jsonb_typeof(filters) = 'array'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.table_columns (
    id uuid primary key default gen_random_uuid(),
    table_id uuid not null references public.tables(id) on delete cascade,
    name text not null check (char_length(btrim(name)) > 0),
    type text not null check (type in ('text', 'boolean')),
    position integer not null check (position >= 0),
    hidden boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (table_id, name),
    unique (table_id, position)
);

create table public.table_rows (
    id uuid primary key default gen_random_uuid(),
    table_id uuid not null references public.tables(id) on delete cascade,
    position integer not null check (position >= 0),
    values jsonb not null default '{}'::jsonb
        check (jsonb_typeof(values) = 'object'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (table_id, position)
);

create index table_columns_table_id_idx
    on public.table_columns (table_id);
create index table_rows_table_id_idx
    on public.table_rows (table_id);
create index table_rows_values_gin_idx
    on public.table_rows using gin (values);

alter table public.tables enable row level security;
alter table public.table_columns enable row level security;
alter table public.table_rows enable row level security;

revoke all on table public.tables from public, anon, authenticated;
revoke all on table public.table_columns from public, anon, authenticated;
revoke all on table public.table_rows from public, anon, authenticated;
revoke all on table public.tables from service_role;
revoke all on table public.table_columns from service_role;
revoke all on table public.table_rows from service_role;

grant select, insert, update, delete on table public.tables to service_role;
grant select, insert, update, delete on table public.table_columns to service_role;
grant select, insert, update, delete on table public.table_rows to service_role;
