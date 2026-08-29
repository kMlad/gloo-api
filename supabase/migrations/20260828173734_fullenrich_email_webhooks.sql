alter table public.table_email_enrichment_runs
    drop constraint if exists table_email_enrichment_runs_status_check;

alter table public.table_email_enrichment_runs
    add constraint table_email_enrichment_runs_status_check
        check (status in ('queued', 'running', 'waiting', 'succeeded', 'partial', 'failed'));

alter table public.table_email_enrichment_run_items
    add column column_id uuid references public.table_columns(id) on delete cascade;

update public.table_email_enrichment_run_items item
set column_id = run.column_id
from public.table_email_enrichment_runs run
where run.id = item.run_id;

alter table public.table_email_enrichment_run_items
    alter column column_id set not null;

alter table public.table_email_enrichment_run_items
    drop constraint if exists table_email_enrichment_run_items_status_check;

alter table public.table_email_enrichment_run_items
    add constraint table_email_enrichment_run_items_status_check
        check (
            status in (
                'queued', 'running', 'waiting', 'succeeded', 'not_found',
                'failed', 'skipped'
            )
        );

alter table public.table_email_enrichment_attempts
    drop constraint if exists table_email_enrichment_attempts_status_check;

alter table public.table_email_enrichment_attempts
    add constraint table_email_enrichment_attempts_status_check
        check (
            status in (
                'in_progress', 'waiting', 'found', 'not_found', 'valid', 'invalid',
                'skipped_no_input', 'skipped_not_configured', 'skipped_cached',
                'rate_limited', 'timed_out', 'failed'
            )
        );

with ranked_active_items as (
    select
        id,
        row_number() over (
            partition by column_id, row_id
            order by created_at, id
        ) as position
    from public.table_email_enrichment_run_items
    where status in ('queued', 'running', 'waiting')
)
update public.table_email_enrichment_run_items item
set
    status = 'skipped',
    error_message = 'Superseded by another active email enrichment',
    updated_at = now()
from ranked_active_items ranked
where item.id = ranked.id
  and ranked.position > 1;

create unique index table_email_enrichment_items_one_active_per_column_row_idx
    on public.table_email_enrichment_run_items (column_id, row_id)
    where status in ('queued', 'running', 'waiting');

create index table_email_enrichment_attempts_external_request_id_idx
    on public.table_email_enrichment_attempts (external_request_id)
    where external_request_id is not null;
