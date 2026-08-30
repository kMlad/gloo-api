alter table public.leads
    add column status text not null default 'new',
    add column notes text;

alter table public.leads
    add constraint leads_status_check check (
        status in (
            'new',
            'attempted',
            'needs_follow_up',
            'meeting_booked',
            'not_interested',
            'do_not_contact'
        )
    );

create index leads_status_source_observed_at_idx
    on public.leads (status, source_observed_at desc, id);
