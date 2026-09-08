# MVP production readiness — 8 September 2026

**Verdict: close, but do not deploy the current revision as-is.** The API passes
its automated checks and runs against the local database. Production is behind
the code in both schema and configuration. A small workbook authorization fix
was made during this review. No production configuration, data, or deployment
was changed.

Scope: this API repository, its existing uncommitted changes, the linked hosted
Supabase project, and read-only checks of the deployed API. This is not a full
browser/frontend acceptance test or a live paid-enrichment test.

## Launch blockers

1. **Production database is seven migrations behind.** Hosted migration history
   ends at `20260831070344_lead_assignments`. Apply the following existing
   migrations, in order, before releasing this API:

   - `20260904114120_nullable_lead_email_and_phone_normalized.sql`
   - `20260906132400_campaign_import_scopes.sql`
   - `20260906160000_heyreach_campaign_imports.sql`
   - `20260906183000_heyreach_linkedin_sender_name.sql`
   - `20260906193000_heyreach_auto_tag_reply_types.sql`
   - `20260907120000_speed_to_lead.sql`
   - `20260908120000_heyreach_speed_to_lead.sql`

   This affects ordinary operation: current campaign discovery calls the new
   `latest_smartlead_imports` RPC, and HeyReach and speed-to-lead require tables
   that do not exist in production. All 23 migrations executed successfully in
   a fresh temporary PostgreSQL database with a minimal `auth.users` fixture.
   This validates SQL ordering and execution, not an upgrade against a copy of
   production data. The temporary database was removed afterward.

2. **The checked-in-workspace `.env.prod` file cannot start the current app.**
   Settings validation reports these required values missing:
   `HEYREACH_API_KEY`, `SMARTLEAD_WEBHOOK_TOKEN`, `HEYREACH_WEBHOOK_TOKEN`.
   Configure them in the actual deployment environment; both webhook tokens
   require at least 32 characters. This finding is about the local production
   env file: the deployment platform's injected environment was not inspected.
   Preserve the production API/frontend URLs and CORS settings; `.env.local`
   contains localhost URLs and a development tunnel.

3. **Ship the workbook authorization fix.** Previously `/api/v1/tables` required
   authentication but no app role. The hosted Auth settings report
   `disable_signup: false`. A signed-in account without an assigned role could
   therefore enter the shared workbook API, including write/research routes.
   The API now requires `admin`, `sales_lead`, or `sdr` from `app_metadata`, using
   the same dependency as lead access. Regression tests cover read, delete,
   and run requests, reject a user-supplied metadata role, and retain access for
   all three permitted roles. All three existing hosted accounts have valid
   app roles. The fix is local and still needs deployment.

   Turn off hosted **Allow new users to sign up** to match the documented
   invite-only product. Keep the Email provider enabled. Supabase documents
   the signup setting in its [Auth configuration guide](https://supabase.com/docs/guides/auth/general-configuration).

4. **Release the current API version after the schema/configuration updates.**
   The deployed OpenAPI document lacks 12 current paths, including every
   HeyReach route, speed-to-lead routes, and lead CSV imports. The existing
   running service is an older revision, so its successful health response
   does not verify this release candidate.

## Acceptable only with MVP operating limits

- **Background work is not durable.** Imports, enrichment, and research run in
  the API process. A forced restart can leave a run `queued` or `running`;
  import scopes and active enrichment items can then block later attempts.
  There is no startup recovery loop. Phone reconciliation only handles runs
  waiting on FullEnrich, not interrupted synchronous work. For a supervised
  pilot, run one always-on API instance, avoid deploys during active jobs, and
  inspect unfinished runs after interruptions. Durable recovery is needed
  before unattended operation becomes a requirement.
- **Speed-to-lead delivery can be lost.** Webhook routes return 204 before
  processing/persistence; worker failures are logged and swallowed. Once an
  event is inserted, duplicate handling can also prevent a retry from finishing
  failed downstream work. Treat automatic handoff as best effort for the pilot
  and reconcile with manual imports. Do not promise guaranteed immediate
  processing until events are persisted before acknowledgment and retryable.
- **The existing health endpoint is only a process/configuration signal.**
  `app/main.py` checks whether the Supabase client exists; it performs no query.
  It returned 200 even while the schema required by this revision was missing.
  Use real database-backed API reads as release checks.
- **Slack requires production setup if alerts are part of launch.** `.env.prod`
  has no `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`, or `APP_BASE_URL`. Notifications
  are silently disabled without the bot and channel. Configure these and verify
  campaign webhook registration after deployment if that feature is required.
- **Keep batches modest until hosted row limits are verified.** Several run-item
  reads do not paginate. README requires hosted Max rows to be 10,000; local
  configuration has that value, but the hosted API limit was not established in
  this review. Do not assume 10,000-row runs are validated by the unit tests.

## Verification completed

- Full test suite: **419 passed**, including six new authorization cases.
- Both required Ruff commands pass; `git diff --check` passes.
- Final Docker image builds with locked dependencies; startup, a real database
  query, and shutdown pass against local Supabase. No dotenv files were bundled
  in the inspected image.
- Current app with real local Supabase returns 200 for SmartLead and HeyReach
  import history. Unauthenticated lead, workbook, SDR-list, and speed-to-lead
  requests return 401.
- All 23 migrations execute in a fresh isolated PostgreSQL database.
- Deployed CORS preflight accepts the production frontend origin.
- Hosted public tables have RLS enabled and no direct table grants to `anon`
  or `authenticated`. No active import/enrichment/research jobs were found in
  the queried hosted job tables at review time.

## Final release acceptance

After the migration/configuration/API updates, verify one invited user's login,
one campaign import per required provider, lead assignment and SDR visibility,
a small workbook CSV import, and one real enrichment. If speed-to-lead/Slack is
required, verify a genuine provider event produces the assigned lead and alert.
These external side effects were not triggered during this review. Frontend
interaction, email delivery, provider credits, actual enrichment callbacks,
and restart recovery remain outside the completed end-to-end verification.
