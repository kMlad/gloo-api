create or replace function public.upsert_smartlead_lead_conversation(
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
    stored_conversation public.smartlead_conversations%rowtype;
begin
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
        properties,
        custom_properties,
        source_observed_at,
        updated_at
    )
    values (
        p_lead ->> 'email',
        p_lead ->> 'email_normalized',
        p_lead ->> 'first_name',
        p_lead ->> 'last_name',
        p_lead ->> 'smartlead_phone_number',
        p_lead ->> 'company_name',
        p_lead ->> 'location',
        p_lead ->> 'website',
        p_lead ->> 'company_url',
        p_lead ->> 'linkedin_profile',
        coalesce(p_lead -> 'properties', '{}'::jsonb),
        coalesce(p_lead -> 'custom_properties', '{}'::jsonb),
        (p_lead ->> 'source_observed_at')::timestamptz,
        now()
    )
    on conflict (email_normalized) do update
    set
        email = excluded.email,
        first_name = excluded.first_name,
        last_name = excluded.last_name,
        smartlead_phone_number = excluded.smartlead_phone_number,
        company_name = excluded.company_name,
        location = excluded.location,
        website = excluded.website,
        company_url = excluded.company_url,
        linkedin_profile = excluded.linkedin_profile,
        properties = excluded.properties,
        custom_properties = excluded.custom_properties,
        source_observed_at = excluded.source_observed_at,
        updated_at = now()
    returning * into stored_lead;

    insert into public.smartlead_conversations (
        lead_id,
        smartlead_campaign_id,
        smartlead_campaign_lead_map_id,
        smartlead_lead_id,
        positive_category_id,
        positive_category_name,
        reply_type,
        qualified_at,
        lead_properties,
        custom_properties,
        updated_at
    )
    values (
        stored_lead.id,
        (p_conversation ->> 'smartlead_campaign_id')::bigint,
        p_conversation ->> 'smartlead_campaign_lead_map_id',
        p_conversation ->> 'smartlead_lead_id',
        (p_conversation ->> 'positive_category_id')::bigint,
        p_conversation ->> 'positive_category_name',
        p_conversation ->> 'reply_type',
        (p_conversation ->> 'qualified_at')::timestamptz,
        coalesce(p_conversation -> 'lead_properties', '{}'::jsonb),
        coalesce(p_conversation -> 'custom_properties', '{}'::jsonb),
        now()
    )
    on conflict (smartlead_campaign_id, smartlead_campaign_lead_map_id) do update
    set
        lead_id = excluded.lead_id,
        smartlead_lead_id = excluded.smartlead_lead_id,
        positive_category_id = excluded.positive_category_id,
        positive_category_name = excluded.positive_category_name,
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

revoke execute on function public.upsert_smartlead_lead_conversation(jsonb, jsonb)
from public, anon, authenticated;
grant execute on function public.upsert_smartlead_lead_conversation(jsonb, jsonb)
to service_role;
