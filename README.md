# Gloo API

FastAPI service for importing positively categorized SmartLead replies into
Supabase. Imports are manual and restricted to an API-managed campaign allowlist.

## Configuration

Copy `.env.example` to `.env.local` and provide:

- `SUPABASE_URL` and a backend-only `SUPABASE_SECRET_KEY`
- `SMARTLEAD_API_KEY`
- `LEADMAGIC_API_KEY`, `PROSPEO_API_KEY`, `AIRSCALE_API_KEY`, and
  `FULLENRICH_API_KEY`
- a long random `INTERNAL_API_TOKEN`
- the externally reachable `PUBLIC_API_BASE_URL` and a separate long random
  `FULLENRICH_WEBHOOK_TOKEN`
- optionally `INVITE_REDIRECT_URL` (must be in the Auth redirect allow-list)

Never expose the Supabase secret key or internal token in a browser client.

Local Auth is invite-only (`enable_signup = false` in `supabase/config.toml`).
On a hosted project, disable public signup the same way: Dashboard → Auth →
Providers → Email. Admin invites still create users when signup is disabled.

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

User invite endpoints instead require a Supabase user access token:

```text
Authorization: Bearer <SUPABASE_ACCESS_TOKEN>
```

## Workflow

Add an allowed campaign:

```shell
curl -X POST http://127.0.0.1:8000/api/v1/smartlead/campaigns \
  -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"smartlead_campaign_id": 12345, "enabled": true, "reply_types": ["positive", "ooo"]}'
```

Import all enabled campaigns:

```shell
curl -X POST http://127.0.0.1:8000/api/v1/smartlead/imports \
  -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Campaign `reply_types` may be `["positive"]`, `["ooo"]`, or both and can be
changed with `PATCH /api/v1/smartlead/campaigns/{campaign_id}`. Existing and
new campaigns default to positive replies only. An import may instead specify
`campaign_ids`, `reply_time_from`, and
`reply_time_to`. Timestamps must be timezone-aware ISO 8601 values. Imports over
the configured conversation limit are rejected before reply histories are read.

List imported leads with `GET /api/v1/leads`, or use
`GET /api/v1/leads?reply_type=ooo` to select currently OOO leads for phone
enrichment. Retrieve complete canonical,
campaign-specific, custom-property, and reply data with
`GET /api/v1/leads/{lead_id}`.

## Phone enrichment

Enrich selected leads:

```shell
curl -X POST http://127.0.0.1:8000/api/v1/phone-enrichments \
  -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  -H "Idempotency-Key: phone-run-2026-08-12-01" \
  -H "Content-Type: application/json" \
  -d '{"lead_ids": ["00000000-0000-0000-0000-000000000000"]}'
```

Omit `lead_ids` to enrich the newest eligible leads; `limit` defaults to 25
and is capped at 100. The service checks inbound reply signatures first, then
LeadMagic, Prospeo, AirScale, and FullEnrich, stopping at the first valid E.164
number. FullEnrich runs may remain in `waiting` until its authenticated webhook
arrives. Inspect a run with `GET /api/v1/phone-enrichments/{run_id}` or use
`POST /api/v1/phone-enrichments/{run_id}/reconcile` after five minutes if a
callback needs reconciliation.

Provider requests, responses, statuses, and safe errors are retained with each
run. API keys and authorization headers are never written to audit records.

## User invites

Admins and sales leads invite teammates with a user JWT (not the internal token).
Roles are stored in Auth `app_metadata.role`. Admins may invite `admin`,
`sales_lead`, and `sdr`. Sales leads may invite `sdr` only.

```shell
curl -X POST http://127.0.0.1:8000/api/v1/users/invites \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email": "teammate@example.com", "role": "sdr"}'
```

Local invite emails are captured by Inbucket at `http://127.0.0.1:54324`. Hosted
projects need SMTP configured. After the invite email, the user sets a password
via Supabase's invite confirmation flow.
