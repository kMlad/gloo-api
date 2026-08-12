# Gloo API

FastAPI service for importing positively categorized SmartLead replies into
Supabase. Imports are manual and restricted to an API-managed campaign allowlist.

## Configuration

Copy `.env.example` to `.env.local` and provide:

- `SUPABASE_URL` and a backend-only `SUPABASE_SECRET_KEY`
- `SMARTLEAD_API_KEY`
- a long random `INTERNAL_API_TOKEN`

Never expose the Supabase secret key or internal token in a browser client.

## Local setup

```shell
uv sync --group dev
supabase start
supabase db reset
uv run fastapi dev
```

All integration and lead endpoints require:

```text
Authorization: Bearer <INTERNAL_API_TOKEN>
```

## Workflow

Add an allowed campaign:

```shell
curl -X POST http://127.0.0.1:8000/api/v1/smartlead/campaigns \
  -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"smartlead_campaign_id": 12345, "enabled": true}'
```

Import all enabled campaigns:

```shell
curl -X POST http://127.0.0.1:8000/api/v1/smartlead/imports \
  -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

An import may instead specify `campaign_ids`, `reply_time_from`, and
`reply_time_to`. Timestamps must be timezone-aware ISO 8601 values. Imports over
the configured conversation limit are rejected before reply histories are read.

List imported leads with `GET /api/v1/leads`; retrieve complete canonical,
campaign-specific, custom-property, and reply data with
`GET /api/v1/leads/{lead_id}`.

V1 does not parse signature phone numbers or call enrichment providers.
