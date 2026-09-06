alter table public.heyreach_conversations
    add column linkedin_sender_name text;

create or replace function public.upsert_heyreach_lead_conversation(
    p_lead jsonb,
    p_conversation jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
    stored_lead public.leads%rowtype;
    stored_conversation public.heyreach_conversations%rowtype;
    email_key text;
    linkedin_key text;
begin
    email_key := nullif(btrim(coalesce(p_lead ->> 'email_normalized', '')), '');
    linkedin_key := nullif(btrim(coalesce(p_lead ->> 'linkedin_profile_normalized', '')), '');

    if email_key is not null then
        select * into stored_lead
        from public.leads
        where email_normalized = email_key
        limit 1;
    end if;

    if stored_lead.id is null and linkedin_key is not null then
        select * into stored_lead
        from public.leads
        where linkedin_profile_normalized = linkedin_key
        limit 1;
    end if;

    if stored_lead.id is null then
        insert into public.leads (
            email,
            email_normalized,
            first_name,
            last_name,
            smartlead_phone_number,
            company_name,
            location,
            website,
            company_url,
            linkedin_profile,
            linkedin_profile_normalized,
            properties,
            custom_properties,
            source_observed_at,
            updated_at
        )
        values (
            nullif(p_lead ->> 'email', ''),
            email_key,
            p_lead ->> 'first_name',
            p_lead ->> 'last_name',
            p_lead ->> 'smartlead_phone_number',
            p_lead ->> 'company_name',
            p_lead ->> 'location',
            p_lead ->> 'website',
            p_lead ->> 'company_url',
            p_lead ->> 'linkedin_profile',
            linkedin_key,
            coalesce(p_lead -> 'properties', '{}'::jsonb),
            coalesce(p_lead -> 'custom_properties', '{}'::jsonb),
            (p_lead ->> 'source_observed_at')::timestamptz,
            now()
        )
        returning * into stored_lead;
    else
        update public.leads
        set
            email = coalesce(nullif(p_lead ->> 'email', ''), email),
            email_normalized = coalesce(email_key, email_normalized),
            first_name = coalesce(nullif(p_lead ->> 'first_name', ''), first_name),
            last_name = coalesce(nullif(p_lead ->> 'last_name', ''), last_name),
            smartlead_phone_number = coalesce(
                nullif(p_lead ->> 'smartlead_phone_number', ''),
                smartlead_phone_number
            ),
            company_name = coalesce(nullif(p_lead ->> 'company_name', ''), company_name),
            location = coalesce(nullif(p_lead ->> 'location', ''), location),
            website = coalesce(nullif(p_lead ->> 'website', ''), website),
            company_url = coalesce(nullif(p_lead ->> 'company_url', ''), company_url),
            linkedin_profile = coalesce(
                nullif(p_lead ->> 'linkedin_profile', ''),
                linkedin_profile
            ),
            linkedin_profile_normalized = coalesce(
                linkedin_key,
                linkedin_profile_normalized
            ),
            properties = coalesce(properties, '{}'::jsonb)
                || coalesce(p_lead -> 'properties', '{}'::jsonb),
            custom_properties = coalesce(custom_properties, '{}'::jsonb)
                || coalesce(p_lead -> 'custom_properties', '{}'::jsonb),
            source_observed_at = greatest(
                source_observed_at,
                (p_lead ->> 'source_observed_at')::timestamptz
            ),
            chat_refreshed_at = null,
            updated_at = now()
        where id = stored_lead.id
        returning * into stored_lead;
    end if;

    insert into public.heyreach_conversations (
        lead_id,
        heyreach_campaign_id,
        heyreach_conversation_id,
        heyreach_lead_id,
        linkedin_account_id,
        linkedin_sender_name,
        reply_type,
        qualified_at,
        lead_properties,
        custom_properties,
        updated_at
    )
    values (
        stored_lead.id,
        (p_conversation ->> 'heyreach_campaign_id')::bigint,
        p_conversation ->> 'heyreach_conversation_id',
        p_conversation ->> 'heyreach_lead_id',
        nullif(p_conversation ->> 'linkedin_account_id', '')::bigint,
        nullif(p_conversation ->> 'linkedin_sender_name', ''),
        coalesce(p_conversation ->> 'reply_type', 'positive'),
        (p_conversation ->> 'qualified_at')::timestamptz,
        coalesce(p_conversation -> 'lead_properties', '{}'::jsonb),
        coalesce(p_conversation -> 'custom_properties', '{}'::jsonb),
        now()
    )
    on conflict (heyreach_campaign_id, heyreach_conversation_id) do update
    set
        lead_id = excluded.lead_id,
        heyreach_lead_id = excluded.heyreach_lead_id,
        linkedin_account_id = excluded.linkedin_account_id,
        linkedin_sender_name = coalesce(
            excluded.linkedin_sender_name,
            public.heyreach_conversations.linkedin_sender_name
        ),
        reply_type = excluded.reply_type,
        qualified_at = excluded.qualified_at,
        lead_properties = excluded.lead_properties,
        custom_properties = excluded.custom_properties,
        updated_at = now()
    returning * into stored_conversation;

    update public.leads
    set
        chat_refreshed_at = null,
        updated_at = now()
    where id = stored_lead.id
    returning * into stored_lead;

    return jsonb_build_object(
        'lead', to_jsonb(stored_lead),
        'conversation', to_jsonb(stored_conversation)
    );
end;
$$;
