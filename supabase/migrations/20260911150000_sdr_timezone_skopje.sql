alter table public.sdr_settings
    alter column timezone set default 'Europe/Skopje';

update public.sdr_settings
    set timezone = 'Europe/Skopje',
        updated_at = now()
    where timezone = 'Europe/Paris';
