update public.sdr_settings
    set timezone = 'Europe/Skopje',
        updated_at = now()
    where timezone is distinct from 'Europe/Skopje';

alter table public.sdr_settings
    drop constraint sdr_settings_timezone_check;

alter table public.sdr_settings
    add constraint sdr_settings_timezone_check check (timezone = 'Europe/Skopje');
