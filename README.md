# Gloo API

FastAPI service for discovering SmartLead campaigns, importing positive and
out-of-office replies into Supabase, and enriching the imported leads with phone
numbers. Import and phone enrichment are separate, asynchronous runs.

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

SmartLead campaign configuration writes require the internal token:

```text
Authorization: Bearer <INTERNAL_API_TOKEN>
```

Campaign discovery, imports, import history, and phone enrichment accept that
internal token or a user JWT whose `app_metadata.role` is `admin` or
`sales_lead`. SDRs cannot call those routes.

User invite, lead, and table (workbook) endpoints require a Supabase user
access token:

```text
Authorization: Bearer <SUPABASE_ACCESS_TOKEN>
```

## SmartLead workflow

Discover SmartLead campaigns and see which ones have already supplied imported
leads:

```shell
curl http://127.0.0.1:8000/api/v1/smartlead/campaigns \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN"
```

The response is synchronized from SmartLead and includes campaign status, tags,
`ever_imported`, imported lead counts by reply type, and the most recent import
run. `enabled` remains available for legacy scheduled or all-enabled imports;
sales leads can explicitly import any campaign returned by discovery.

Queue a positive-reply import for selected campaigns:

```shell
curl -X POST http://127.0.0.1:8000/api/v1/smartlead/imports \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Idempotency-Key: smartlead-import-2026-08-31-01" \
  -H "Content-Type: application/json" \
  -d '{"campaign_ids": [12345, 67890], "reply_types": ["positive"]}'
```

Set `reply_types` to `["ooo"]` for only out-of-office replies or
`["positive", "ooo"]` for both. The filter belongs to the import request, so
campaign discovery/configuration and importing remain separate concerns. An
import may also specify timezone-aware `reply_time_from` and `reply_time_to`
values. Omitting `campaign_ids` imports all campaigns whose legacy `enabled`
flag is true.

The endpoint returns **202** with a durable run record in `queued` status; the
import then runs as an API background task. The idempotency key makes a
retry return the original run. Only one SmartLead import may be queued or
running at a time, and imports over `SMARTLEAD_IMPORT_LIMIT` are rejected before
reply histories are read. SmartLead calls are throttled within the API process
below its documented API-key limit.

Inspect import history, status, and the exact leads captured by a run:

```text
GET /api/v1/smartlead/imports?limit=25&offset=0
GET /api/v1/smartlead/imports/{run_id}
GET /api/v1/smartlead/imports/{run_id}/leads
```

Imports atomically persist each lead with its SmartLead conversation. To repair
legacy leads whose conversation row was not written, rerun the import with the
affected campaigns and reply-time range. The upserts are idempotent, and a
successful import invalidates the lead's chat cache so the next detail request
loads the complete SmartLead history.

For a historical repair, explicitly request every reply category that must be
retained, then run an unbounded import after deploying the migrations:

```shell
curl -X POST http://127.0.0.1:8000/api/v1/smartlead/imports \
  -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  -H "Idempotency-Key: smartlead-historical-repair-01" \
  -H "Content-Type: application/json" \
  -d '{"reply_types": ["positive", "ooo"]}'
```

Date filters apply to SmartLead reply timestamps, not local lead creation dates,
so a creation-date cutoff is not sufficient to repair an older reply batch.

List imported leads with `GET /api/v1/leads` (user access token). Filters include
`campaign_id`, `import_run_id`, `status`, singular `reply_type`, and repeated
`reply_types`, for example
`?reply_types=positive&reply_types=ooo&campaign_id=12345`. Every list item
includes `source_campaigns` so the UI can show where and why the lead qualified.
Retrieve complete canonical, campaign-specific, custom-property, and full
SmartLead chat history (inbound and outbound) with
`GET /api/v1/leads/{lead_id}`. Cached threads are refreshed from SmartLead when
older than `SMARTLEAD_CHAT_REFRESH_TTL_SECONDS` (default 1 hour).

## Lead assignment

Admins and sales leads can find assignment candidates by combining the existing
SmartLead filters with `assignment_status=unassigned`, for example:

```text
GET /api/v1/leads?campaign_id=12345&reply_type=positive&assignment_status=unassigned
```

The UI confirms a concrete snapshot by sending up to 100 exact lead IDs. Leads
that were assigned by another manager after the list was loaded are skipped
rather than overwritten:

```shell
curl -X POST http://127.0.0.1:8000/api/v1/leads/assignments \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"lead_ids": ["00000000-0000-0000-0000-000000000000"], "sdr_id": "11111111-1111-1111-1111-111111111111"}'
```

Use `GET /api/v1/users/sdrs` to populate the assignee picker. Explicit recovery
operations are `PUT /api/v1/leads/{lead_id}/assignment` with an `sdr_id` to
replace the owner and `DELETE /api/v1/leads/{lead_id}/assignment` to unassign.
Only admins and sales leads may use these operations.

SDRs can list, retrieve, and update only leads assigned to their own Auth user
ID. Other and unassigned leads return no list rows and `404` from detail/update
routes. SDR updates remain limited to the existing `status` and `notes` fields.

## Phone enrichment

Queue phone enrichment for the exact lead snapshot from a completed SmartLead
import (internal token or an admin / sales-lead JWT):

```shell
curl -X POST http://127.0.0.1:8000/api/v1/phone-enrichments \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Idempotency-Key: phone-run-2026-08-12-01" \
  -H "Content-Type: application/json" \
  -d '{"source_import_run_id": "00000000-0000-0000-0000-000000000000"}'
```

This is a separate **202** queued run; importing never starts enrichment
automatically. Alternatively, send `lead_ids` (up to 100), or omit both selectors
to enrich the newest eligible leads (`limit` defaults to 25 and is capped at
100). `lead_ids` and `source_import_run_id` are mutually exclusive.

The service checks inbound reply signatures first, then LeadMagic, Prospeo,
AirScale, and FullEnrich, stopping at the first valid E.164 number. FullEnrich
runs may remain in `waiting` until its authenticated webhook arrives. Inspect a
run with `GET /api/v1/phone-enrichments/{run_id}` or use
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
create, import, and edit them. Column types are `text`, `boolean`,
`sheriff`, `email_enrichment`, or `email_validation`. Empty `text`/`boolean`
columns start blank; missing cells are returned as `null`. Sheriff columns are
a reusable research prompt that writes typed fields back into auto-created
child columns. Email enrichment columns run a provider waterfall, validate
candidates with MillionVerifier, and write a work email into an auto-created
child column. Email validation columns check an existing text column with
MillionVerifier and write a boolean into an auto-created child column.

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
`PUT /api/v1/tables/{table_id}/filters`. Each clause after the first may set
`logic` to `and` (default) or `or` to chain it with the previous result.
Clauses are evaluated left to right, so `contains "Yes" OR contains "Unclear"
AND active is true` keeps rows that match Yes or Unclear, then restricts to
active rows. The first clause's `logic` is ignored. Rename or hide a column with
`PATCH /api/v1/tables/{table_id}/columns/{column_id}`. Persist column order with
`PUT /api/v1/tables/{table_id}/columns/order` and a complete `column_ids` list.
Download the current view as CSV (saved filters, current column order, hidden
columns omitted). The file is UTF-8 with a BOM so Excel opens it. Optional
`sort_column_id` and `sort_direction` (`asc` or `desc`) match a client-side
sort; the default is row order:

```text
GET /api/v1/tables/{table_id}/export
GET /api/v1/tables/{table_id}/export?sort_column_id=...&sort_direction=desc
```

Append an empty column with `POST /api/v1/tables/{table_id}/columns`. Create,
patch, and delete rows under `/api/v1/tables/{table_id}/rows`. Sheriff, email
enrichment, and email validation columns are not allowed on table create or CSV
import — add them after input columns exist.

Optional prompt helper (nothing is persisted):

```text
POST /api/v1/tables/{table_id}/sheriff/prompts/expand
{"goal": "Find the CEO of {{Company}}", "column_ids": []}
```

Create a sheriff column with a user prompt and output fields (`text` or
`boolean`, max 10). Expanding the prompt is optional; if `enhanced_prompt` is
omitted, runs interpolate `user_prompt`. Optional `web_search` (default
`true`), `web_search_limit` (1–20 web_search calls per row; omit for the
default 5-step loop), and `model` (OpenAI models only, default
`openai/gpt-5.4-mini`) are stored on the column and used for runs. List
allowed models with `GET /api/v1/tables/{table_id}/sheriff/options`. The
response is the full table, including auto-created child columns
(`first_name` → `First name`).

```shell
curl -X POST http://127.0.0.1:8000/api/v1/tables/$TABLE_ID/columns \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "CEO",
    "type": "sheriff",
    "sheriff": {
      "user_prompt": "Find the CEO of {{Company}}",
      "web_search": true,
      "web_search_limit": 2,
      "model": "openai/gpt-5.4-mini",
      "outputs": [
        {"key": "first_name", "type": "text"},
        {"key": "last_name", "type": "text"}
      ]
    }
  }'
```

Run one row, selected rows, or the whole table (omit `row_ids`). Returns
**202** with the run and items in `queued` status. Parent
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

Sheriff uses the Perplexity Agent API with `PERPLEXITY_API_KEY`. Runs use the
column's `model` and `web_search` flag (`web_search` adds Perplexity
`web_search` and `fetch_url`). If the prompt includes a URL, the agent fetches
that page first and searches only if the page is not enough. Optional
`web_search_limit` caps how many times `web_search` may be called; `max_steps`
is set to `limit + 2` so the model still has a fetch turn and a final answer
turn. Perplexity Pro/Max app plans do not include API access.
Expand/run without a key returns 503. Optional tuning: `SHERIFF_MODEL`
(default `openai/gpt-5.4-mini`, used for prompt expand), `SHERIFF_TIMEOUT_SECONDS`,
`SHERIFF_CONCURRENCY`, `SHERIFF_SEARCH_CONTEXT_SIZE` (`low` / `medium` /
`high`, default `medium`).

Create an email enrichment column after mapping first name, last name, LinkedIn
URL, company name, and company domain/website columns. Providers default to
Icypeas → Kitt → LeadMagic → Prospeo → FullEnrich; omit or reorder them as
needed. The only validator is MillionVerifier. By default only `ok` counts as
valid. Set `accept_catchall: true` to also accept `catch_all` after the
waterfall finishes looking for an `ok` (the first catch-all is used as a
fallback). `PATCH` can turn the flag on and will reclassify existing
`not_found` rows from stored MillionVerifier results; it cannot turn the flag
off. Catch-all and other results are cached on the row so the same address is
not re-verified later in the waterfall.

```shell
curl -X POST http://127.0.0.1:8000/api/v1/tables/$TABLE_ID/columns \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Work email",
    "type": "email_enrichment",
    "email_enrichment": {
      "providers": ["icypeas", "kitt", "leadmagic", "prospeo", "fullenrich"],
      "validator": "millionverifier",
      "accept_catchall": false,
      "first_name_column_id": "...",
      "last_name_column_id": "...",
      "linkedin_column_id": "...",
      "company_name_column_id": "...",
      "company_domain_column_id": "..."
    }
  }'
```

Runs use the same endpoint as sheriff:
`POST /api/v1/tables/{table_id}/columns/{column_id}/runs`. A row is skipped when
any mapped input is blank. `overwrite: false` skips rows whose parent cell
`status` is `succeeded`. A succeeded cell includes the waterfall `steps` the UI
can render, for example `Icypeas: found test@gmail.com (invalid)`. Catch-all
fallbacks store `validation_result: "catch_all"` instead of `"ok"`:

```json
{
  "status": "succeeded",
  "email": "test1@gmail.com",
  "provider": "prospeo",
  "validator": "millionverifier",
  "validation_result": "ok",
  "rejected_emails": ["test@gmail.com"],
  "steps": [
    {
      "provider": "icypeas",
      "status": "found",
      "emails": [{"email": "test@gmail.com", "validation": "invalid"}]
    },
    {
      "provider": "kitt",
      "status": "found",
      "emails": [{"email": "test@gmail.com", "validation": "skipped"}]
    },
    {
      "provider": "prospeo",
      "status": "found",
      "emails": [{"email": "test1@gmail.com", "validation": "valid"}]
    }
  ],
  "error": null
}
```

Email enrichment reuses `LEADMAGIC_API_KEY`, `PROSPEO_API_KEY`, and
`FULLENRICH_API_KEY`. Optional keys: `ICYPEAS_API_KEY`, `KITT_API_KEY`,
`MILLIONVERIFIER_API_KEY`. Runs without MillionVerifier return 503. Selected
providers without a key are skipped. Optional tuning:
`EMAIL_ENRICHMENT_CONCURRENCY` (default 3),
`EMAIL_ENRICHMENT_FULLENRICH_POLL_SECONDS` (default 90).

Create an email validation column after mapping a text column that already
holds addresses. The response is the full table, including an auto-created
boolean child column (`valid` → `Valid`). The only validator is
MillionVerifier. By default only `ok` counts as valid. Set
`accept_catchall: true` to also treat `catch_all` as valid. `PATCH` can change
the mapped column or the flag; changing `accept_catchall` reclassifies existing
succeeded cells and the boolean child from the stored result without calling
MillionVerifier again.

```shell
curl -X POST http://127.0.0.1:8000/api/v1/tables/$TABLE_ID/columns \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Email valid",
    "type": "email_validation",
    "email_validation": {
      "email_column_id": "...",
      "validator": "millionverifier",
      "accept_catchall": false
    }
  }'
```

Runs use the same endpoint as sheriff and email enrichment:
`POST /api/v1/tables/{table_id}/columns/{column_id}/runs`. A row is skipped when
the mapped email is blank or not a valid address. `overwrite: false` skips rows
whose parent cell `status` is `succeeded`. Parent cells are computed JSON and
cannot be patched; the boolean child can. A succeeded cell looks like:

```json
{
  "status": "succeeded",
  "email": "ada@acme.com",
  "validator": "millionverifier",
  "result": "ok",
  "valid": true,
  "error": null
}
```

Runs without MillionVerifier return 503. Concurrency uses
`EMAIL_ENRICHMENT_CONCURRENCY`.
