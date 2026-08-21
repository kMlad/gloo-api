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

Local Auth is invite-only (`[auth] enable_signup = false` in
`supabase/config.toml`). Keep `[auth.email] enable_signup = true` so invited
users can still sign in; that flag enables the email provider, not public
signup. On a hosted project, leave the Email provider enabled and turn off
**Allow new users to sign up**. Admin invites still create users when signup
is disabled.

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

User invite endpoints and table (workbook) endpoints require a Supabase user
access token:

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

## Tables

Workbook-style tables are independent of CRM leads. Any signed-in user can
create, import, and edit them. Column types are `text`, `boolean`, or
`sheriff`. Empty `text`/`boolean` columns start blank; missing cells are
returned as `null`. Sheriff columns are a reusable research prompt that writes
typed fields back into auto-created child columns.

Create an empty table:

```shell
curl -X POST http://127.0.0.1:8000/api/v1/tables \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Outbound Aug", "columns": [{"name": "Company", "type": "text"}]}'
```

Import a CSV as a **new** table (headers become text columns; optional `name`
overrides the filename):

```shell
curl -X POST http://127.0.0.1:8000/api/v1/tables/imports \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -F "file=@leads.csv" \
  -F "name=Outbound Aug"
```

CSV files must be UTF-8, comma-separated, at most 5 MB, 50 columns, and 10,000
data rows. Duplicate or empty headers are rejected.

List tables with `GET /api/v1/tables` (`row_count` is an exact Postgres count).
Load schema, saved filters, and column order with `GET /api/v1/tables/{table_id}`.
Rows are paged separately; `total` is the exact matching count, independent of
PostgREST's payload cap:

```text
GET /api/v1/tables/{table_id}/rows?limit=100&offset=0
```

`limit` is 1–200 (default 100). The UI can request later windows as the user
scrolls. Local `[api] max_rows` in `supabase/config.toml` is 10,000 to match CSV
import; restart local Supabase after changing it (`supabase stop && supabase start`).
Hosted projects have a separate **Max rows** setting under Project Settings → API
— set that to 10,000 as well, or production stays capped at the default 1,000.

Saved filters are applied on row reads. Replace them with
`PUT /api/v1/tables/{table_id}/filters`. Rename or hide a column with
`PATCH /api/v1/tables/{table_id}/columns/{column_id}`. Persist column order with
`PUT /api/v1/tables/{table_id}/columns/order` and a complete `column_ids` list.
Append an empty column with `POST /api/v1/tables/{table_id}/columns`. Create,
patch, and delete rows under `/api/v1/tables/{table_id}/rows`. Sheriff columns
are not allowed on table create or CSV import — add them after input columns
exist.

Optional prompt helper (nothing is persisted):

```text
POST /api/v1/tables/{table_id}/sheriff/prompts/expand
{"goal": "Find the CEO of {{Company}}", "column_ids": []}
```

Create a sheriff column with a user prompt and output fields (`text` or
`boolean`, max 10). Expanding the prompt is optional; if `enhanced_prompt` is
omitted, runs interpolate `user_prompt`. The response is the full table,
including auto-created child columns (`first_name` → `First name`).

```shell
curl -X POST http://127.0.0.1:8000/api/v1/tables/$TABLE_ID/columns \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "CEO",
    "type": "sheriff",
    "sheriff": {
      "user_prompt": "Find the CEO of {{Company}}",
      "outputs": [
        {"key": "first_name", "type": "text"},
        {"key": "last_name", "type": "text"}
      ]
    }
  }'
```

Run one row, selected rows, or the whole table (omit `row_ids`). Max 100 rows
per run. Returns **202** with the run and items in `queued` status. Parent
cells start as `queued`, then flip to `running` when a worker picks them up
(`SHERIFF_CONCURRENCY`, default 3). Poll
`GET /api/v1/tables/{table_id}/columns/{column_id}/runs/{run_id}`.

```text
POST /api/v1/tables/{table_id}/columns/{column_id}/runs
{"row_ids": ["..."], "overwrite": false}
```

`overwrite: false` (default) skips rows whose sheriff cell `status` is
`succeeded`. Parent cells are computed JSON and cannot be patched; child cells
can. A succeeded cell looks like:

```json
{
  "status": "succeeded",
  "confidence": "high",
  "confidence_reason": "LinkedIn SERP headline still lists Acme",
  "sources": [{"url": "https://...", "title": "..."}],
  "output": {"first_name": "Ada", "last_name": "Lovelace"},
  "error": null
}
```

Sheriff uses the Perplexity Agent API (`web_search` only) with
`PERPLEXITY_API_KEY`. Perplexity Pro/Max app plans do not include API access.
Expand/run without a key returns 503. Optional tuning: `SHERIFF_MODEL`
(default `openai/gpt-5.4-mini`), `SHERIFF_TIMEOUT_SECONDS`,
`SHERIFF_CONCURRENCY`.


