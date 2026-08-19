alter table public.table_claygent_runs
    drop constraint if exists table_claygent_runs_status_check;

alter table public.table_claygent_runs
    add constraint table_claygent_runs_status_check
        check (status in ('queued', 'running', 'succeeded', 'partial', 'failed'));

alter table public.table_claygent_run_items
    drop constraint if exists table_claygent_run_items_status_check;

alter table public.table_claygent_run_items
    add constraint table_claygent_run_items_status_check
        check (status in ('queued', 'running', 'succeeded', 'failed', 'skipped'));
