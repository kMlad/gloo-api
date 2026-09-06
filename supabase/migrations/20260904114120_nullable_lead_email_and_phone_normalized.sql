alter table public.leads
    drop constraint if exists leads_email_normalized_check;

alter table public.leads
    alter column email drop not null,
    alter column email_normalized drop not null;

alter table public.leads
    add constraint leads_email_normalized_format_check check (
        (email is null and email_normalized is null)
        or (
            email is not null
            and email_normalized is not null
            and email_normalized = lower(btrim(email_normalized))
        )
    );

alter table public.leads
    add column phone_normalized text generated always as (
        nullif(
            regexp_replace(
                coalesce(smartlead_phone_number, enriched_phone_number, ''),
                '[^0-9]',
                '',
                'g'
            ),
            ''
        )
    ) stored;

alter table public.leads
    add constraint leads_email_or_phone_check check (
        email_normalized is not null or phone_normalized is not null
    );

create index leads_phone_normalized_idx
    on public.leads (phone_normalized)
    where phone_normalized is not null;
