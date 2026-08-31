alter table public.leads
    add column assigned_sdr_id uuid references auth.users(id) on delete restrict,
    add column assigned_by uuid references auth.users(id) on delete set null,
    add column assigned_at timestamptz;

alter table public.leads
    add constraint leads_assignment_fields_check check (
        (assigned_sdr_id is null and assigned_at is null)
        or (assigned_sdr_id is not null and assigned_at is not null)
    );

create index leads_assigned_sdr_source_observed_at_idx
    on public.leads (assigned_sdr_id, source_observed_at desc, id);

create index leads_assigned_by_idx
    on public.leads (assigned_by)
    where assigned_by is not null;
