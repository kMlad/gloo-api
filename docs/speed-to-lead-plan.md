# Speed-to-lead plan

Goal: when a SmartLead reply is categorised positive, auto-enrich the mobile phone, ping the
team in Slack, and surface the lead on a dedicated speed-to-lead screen in gloo-ui.
HeyReach follows right after SmartLead.

Repos: `gloo-api` (this repo, FastAPI + Supabase) and `../gloo-ui` (React 19, react-router,
TanStack Query).

## Decisions (agreed 2026-09-07)

- Trigger: SmartLead campaign webhook, event `LEAD_CATEGORY_UPDATED`. Positive means
  `sentiment_type == positive`, same as imports.
- SmartLead webhooks are unsigned. Auth = secret path token, same pattern as the FullEnrich
  webhook. Handler returns 204 immediately, processes in a background task, must be idempotent
  (SmartLead retries on non-200).
- Per-campaign opt-in, chosen in gloo. Opting in requires an SDR; otherwise 422.
- Auto-assign: unassigned lead gets the campaign SDR. Already-assigned leads untouched.
- Enrichment: reuse existing waterfall (`PhoneEnrichmentService.start` with `lead_ids`).
  Signature check first, then providers. Only `enriched_phone_number` is trusted; the
  SmartLead-supplied number is ignored.
- Notify: Slack, one channel. Alert on arrival, phone follow-up as a thread reply. Needs bot
  token + `chat.postMessage` (incoming webhooks cannot thread).
- Queue semantics: no acknowledge button. Screen lists events whose lead status is still
  `new`; changing status in the lead drawer clears the item.
- Event table is platform-agnostic so HeyReach reuses it.

## Phase 1: API ingest + auto-enrichment (SmartLead)

Migration
- `smartlead_campaigns`: add `speed_to_lead_enabled bool default false`,
  `speed_to_lead_sdr_id uuid references auth.users`, `smartlead_webhook_id text`.
  Check: enabled implies sdr_id not null.
- New `speed_to_lead_events`: id, `platform` (`smartlead`|`heyreach`), lead_id,
  `smartlead_campaign_id` nullable, `heyreach_campaign_id` nullable, conversation_id,
  category_id, category_name, reply_excerpt, replied_at, `dedupe_key` unique,
  enrichment_run_id, notification_status, notification_error, slack_message_ts,
  created_at, updated_at. RLS + service_role grants like other tables.

Env
- `SMARTLEAD_WEBHOOK_TOKEN` (SecretStr, min 32). Add to `.env.example` + README.

Code
- `app/speed_to_lead/` package: `repository.py`, `service.py`, `schemas.py`, `routes.py`.
- Route `POST /api/v1/smartlead/webhooks/{token}`: constant-time compare, 204, background task.
- `SpeedToLeadService.handle_smartlead_category_update(payload)`:
  1. Resolve campaign; skip unless `speed_to_lead_enabled`.
  2. Resolve reply type via cached `get_categories` sentiment map, fallback
     `lead_data.category.sentiment_type`. Skip unless positive.
  3. Dedupe key = sha256(campaign_id, lead email, category_id, reply time). Skip if exists.
  4. Upsert lead + conversation via `upsert_smartlead_lead_conversation` RPC, upsert inbound
     replies from `history` (mirror `ImportService._persist_item`).
  5. Auto-assign if lead unassigned and campaign has SDR.
  6. Insert event row.
  7. `PhoneEnrichmentService.start(lead_ids=[lead_id])` + `execute_background`. Store run id.
- Campaign opt-in endpoint `PATCH /api/v1/smartlead/campaigns/{id}/speed-to-lead`
  body `{enabled, sdr_id}`; admin or sales lead. Validate SDR with existing `_validate_sdr`
  logic (422 if missing/inactive). Enabling registers the SmartLead webhook
  (`POST /campaigns/{id}/webhooks`) and stores id; disabling deletes it.
- `SmartLeadClient`: add `save_webhook`, `delete_webhook`, `list_webhooks`.
- Wire service in `main.py` lifespan + `dependencies.py`.
- Verify the accepted event name against the live SmartLead API (docs disagree:
  `LEAD_REPLIED` vs `LEAD_CATEGORY_UPDATED`).

Tests
- Positive vs non-positive category, disabled campaign, duplicate delivery, auto-assign rules,
  enrichment kickoff, opt-in without SDR -> 422, webhook token rejection.

## Phase 2: Slack notifications

Env
- `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID` (optional; feature no-ops without them).
  Bot scope `chat:write`, invited to channel. `APP_BASE_URL` for deep links.

Code
- `app/notifications/slack.py`: httpx client, `post_message(channel, text, blocks,
  thread_ts=None) -> ts`.
- On event insert: post alert (name, company, campaign, reply excerpt, assigned SDR, link to
  `/speed-to-lead`). Store `slack_message_ts`, `notification_status`.
- On enrichment item `enriched` for that lead: post thread reply with phone + source.
  Hook point: `PhoneEnrichmentService._complete_with_phone` -> callback, or poll run
  completion from the speed-to-lead service.
- Slack failures logged on the event row; never block ingest.
- Run-finished hook (`PhoneEnrichmentService.add_run_finished_listener`): when no phone is
  found, thread a per-provider summary; flag providers that returned insufficient credits.
- Excerpts go through `reply_to_text` (HTML -> text, quoted history dropped).

Tests
- Message formatting, thread reply uses stored ts, missing config no-ops.

## Phase 3: Speed-to-lead API

- `GET /api/v1/speed-to-lead?limit&offset&include_handled=false`: events newest first,
  joined with lead summary (`LeadListItem` shape), assignment, enrichment run status.
  Default filter: lead status `new`. SDRs see only their assigned leads.
- Extend `CampaignResponse` with `speed_to_lead_enabled`, `speed_to_lead_sdr_id`.
- Add `speed_to_lead_at` (latest event time) to `LeadListItem` for badging.

## Phase 4: gloo-ui

- `src/lib/speed-to-lead.ts`: zod schemas + `listSpeedToLeadEvents`, query keys.
- `src/lib/smartlead.ts`: `updateSpeedToLead(campaignId, {enabled, sdr_id})`.
- Campaign detail drawer: "Speed to lead" toggle + SDR picker (reuse `listSdrs`). Disable
  toggle until an SDR is chosen; surface 422 message.
- Router: `/speed-to-lead` route; sidebar item with live unhandled count.
- Page `speed-to-lead-page.tsx`: columns time-since-reply, name, company, campaign, reply
  excerpt, enriched phone + enrichment status, assignee. Row click opens
  `LeadDetailDrawer`; status change invalidates the list. Poll 2s while any enrichment
  running, else 30s.

## Phase 5: HeyReach

- Same opt-in fields + `heyreach_webhook_id` on `heyreach_campaigns`.
- Route `POST /api/v1/heyreach/webhooks/{token}`, env `HEYREACH_WEBHOOK_TOKEN`.
- Handler maps Interested auto-tag -> positive event, reuses
  `upsert_heyreach_lead_conversation` RPC and `HeyReachImportService` helpers.
- Check whether HeyReach webhooks fire on tag change or only on message receipt (auto-tag
  lands ~15 min after the first reply); pick trigger accordingly.
- UI: same toggle in the HeyReach campaign drawer; list already platform-aware.

## Unresolved questions

None. All open points resolved on 2026-09-07 (see Decisions).
