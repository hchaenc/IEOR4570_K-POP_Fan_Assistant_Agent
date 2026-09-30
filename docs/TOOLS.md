# Tools Framework

How the tool system is organized, the unified tool contract, and everything
operational about the two families: original tools (one per team member) and
common tools (shared API access). Booking.com was cancelled from the project
scope: it is not planned, implemented, or referenced anywhere.

## 1. Architecture

```
index.html  <-->  app.py (FastAPI /chat, LiteLLM harness, run_agent loop)
                        |
                        v
              tools/__init__.py   auto-discovery, no shared-file edits
              │                   scans BOTH of:
              ├── originals/      one package per team member's primary tool
              │   ├── weverse/        search_weverse_notices, read_weverse_notice
              │   ├── stage_videos/   placeholder, registers nothing
              │   └── song_lyrics/    placeholder, registers nothing
              └── common/         shared services and tools
                  ├── ticketmaster.py  search_ticketmaster_events
                  └── classify.py      notice event-type helper (not a tool)
```

A module or package is model-callable exactly when it exposes `SCHEMAS` (list
of OpenAI-style function schemas) and `HANDLERS` (tool name -> callable).
`classify.py` exposes neither, so it stays internal. **Adding a tool means
adding one file or folder** - nobody edits the registry, and original tools
never import each other.

The division of labour is deliberate: an original tool returns annotated data
from its own source and performs **no cross-source orchestration**. Deciding
that a ticketed notice deserves a Ticketmaster lookup is the model's job inside
its tool loop. Shared API access lives in `common/` so several original tools
can reuse one client, cache and error mapping.

## 2. Tool contract

Handler rules:

1. Arguments: plain JSON-compatible values only (str/int/bool/None/lists/dicts).
2. Return: a JSON string via `json.dumps(...)` - never raise through the loop.
3. Success envelope: `{"ok": true, ...}`; failure envelope:
   `{"ok": false, "error": "<stable_code>", "message": "<short, safe, actionable>"}`
   plus `"source": "<integration>"` when a specific adapter failed.
4. Configuration via environment variables only (local `.env`, git-ignored).
5. Trace safety: `app.safe_trace_result()` whitelists the scalar summary keys
   (`ok`, `error`, `scanned_count`, `matched_count`, `notice_count`,
   `truncated`, `notice_id`, `text_chars`) for the collapsed trace header. The
   full envelope is also sent to the browser under `result` so a grader can
   expand exactly what the model was told; `text` and `excerpt` are never in
   the summary.

Stable error codes: `missing_credentials`, `authentication_failed`,
`community_not_joined`, `ambiguous_artist`, `rate_limited`,
`upstream_schema_changed`, `timeout`, `notice_not_found`,
`unexpected_upstream_error`, plus harness-level `unknown_tool` / `bad_arguments`.

## 3. The tools

### `search_weverse_notices(artist, query?, limit?)` - original

One signed request fetches up to 300 recent notices, keeps the last 365 days,
deduplicates by id, strips HTML to text and returns up to `limit` (default 120,
which covers the whole window for most artists) newest-first:

```json
{
  "ok": true, "artist": "aespa", "community_id": 125,
  "cutoff": "2025-09-30T00:00:00+00:00",
  "scanned_count": 300, "matched_count": 86, "notice_count": 86, "truncated": false,
  "notices": [
    {
      "notice_id": "36316",
      "title": "[NOTICE] 2026-27 aespa LIVE TOUR Announcement",
      "published_at": "2026-04-21T04:00:00+00:00",
      "url": "https://weverse.io/aespa/notice/36316",
      "event_type": "ticketed_event",
      "matched_signal": "tour",
      "matched_field": "title",
      "ticket_relevant": true,
      "excerpt": "Hello. We are aespa. 2026-27 aespa LIVE TOUR ..."
    }
  ]
}
```

Every notice carries its own evidence: `matched_signal` (the word that fired),
`matched_field` (`title`, `body`, or null) and `excerpt`. The full body does not
appear here - that is what `read_weverse_notice` is for.

Artist names are resolved to a Weverse `urlPath` by trying the hyphenated slug
then the despaced one: Weverse files MONSTA X as `monstax`, so a single guess
would 404. The `artist` field echoes what the user typed, because the model
reuses it as the Ticketmaster keyword and `MONSTAX` matches nothing there.

### `read_weverse_notice(artist, notice_id, max_chars?)` - original

Returns one notice's full text (default 1500 chars, capped at 5000) plus
`text_chars` and `truncated`, so the model can quote application windows,
membership rules or prices that never fit in an excerpt. It reuses the list
request, because `tabContent` already returns complete bodies - there is no
per-notice endpoint to discover, and no cache to invalidate.

Progressive disclosure is the point: a compact digest first, the raw body only
when the answer needs it (course slides p.29: "return titles + summaries to
start... combine with a 'read page' tool for deeper dives"). Dumping all 86
aespa bodies instead would cost ~42,000 tokens on every turn of the session.

### `search_ticketmaster_events(keyword, city?, country_code?)` - common

Discovery API v2, one request per call. Returns `matched_count` and `events[]`
with name, date, time, timezone, status, venue, city, country, `attractions`
(the headliners), `public_on_sale_at`, `presale_windows`, `ticket_limit`,
`please_note` and the purchase `url`.

## 4. Data-source operations

### Weverse (`tools/originals/weverse/`)

The original `MujyKun/Weverse` library is dead: its login endpoint replies
`-10004` to everything and its content host no longer resolves in DNS.
`gateway.py` implements the current contract instead - reads go through the
Naver gateway with an HMAC-SHA1 request signature over
`(short_path + "?" + sorted_query)[:255] + wmsgpad`, with `wmd` travelling as an
ordinary parameter so the wire order equals the signed order.

| Token | Lifetime | Notes |
|---|---|---|
| `WEVERSE_ACCESS_TOKEN` | ~3 days | auto-refreshed on 401 |
| `WEVERSE_REFRESH_TOKEN` | ~90 days | rotates on every refresh; newest value persisted back to `.env` |

- Cold start with only a refresh token: exchanged for a fresh pair first.
- Any 401: one automatic refresh + retry inside the same call.
- HTTP 404 on the NOTICE tab is permanent, not transient: some communities
  (MONSTA X, ATEEZ) expose no notice feed at all, and the message says so
  instead of inviting a retry.
- Whole token family revoked (error `-10019`): re-login in a browser and copy
  the fresh `we2_access_token` / `we2_refresh_token` / `we2_device_id` cookies
  into `.env`. `bootstrap_login.py` is the alternative OTP flow.
- `get_notice_service()` shares one client per process so the rotated token is
  reused instead of paying a refresh cycle per call.

### Ticketmaster (`tools/common/ticketmaster.py`)

Free key (`TICKETMASTER_API_KEY`, 5000 requests/day, in-process response
cache). Three measured constraints shape the tool:

- **`priceRanges` is absent for K-pop**, in the US and in Europe alike, so
  prices are reported as null and the prompt forbids estimating them. On-sale
  times, presale windows and ticket limits are always present.
- **Keyword search also matches event titles.** For the artist TEN,
  date-sorted Discovery returned "Mt. Joy 2026: Celebrating 10 Years" and
  "Chance The Rapper - Coloring Book 10 Year Anniversary" as the first ten
  hits. Results are therefore filtered against `attractions`, and single-word
  keywords must match an attraction exactly.
- **Coverage is territorial.** Verified for aespa: US, CA, GB, DE, NL, IT, ES,
  SE return events; KR, JP, HK, TW, SG, MY, PH, ID, AU return nothing, because
  Ticketmaster does not sell in those markets (Korea uses Interpark / Yes24 /
  Melon Ticket, Japan uses e+ / Lawson). An empty result is an honest answer,
  and the notice names the real channel.

### Event classification (`tools/common/classify.py`)

Deterministic, tiered, bilingual. The title decides; the body is consulted only
when the title carries no signal, and with a deliberately narrower hint list.
Six labels in priority order: `online_event`, `popup`, `fan_event`,
`merchandise`, `ticketed_event`, else `announcement`. Only `ticketed_event`
sets `ticket_relevant`.

Why tiering rather than one scan: bodies are long and full of boilerplate. A
BTS legal-action notice mentions "ticket"; a Korean pre-recording notice
recites ticket and reservation wording; a tour merchandise notice contains
"tour". Scanning title and body together turned all of them into ticketed
events, and titles alone missed notices whose type appears only in the body.
`생방송` is a body-tier hint only because pre-recording notices are caught by
their titles first.

Live effect on aespa's 86 in-window notices: 45 labels from the title, 6 from
the body, 35 announcements, 10 ticketed. Before tiering, 18 were labeled
ticketed (11 wasted Ticketmaster searches). Re-check the mix against a live
artist before trusting any hint-list change.

## 5. Chatbot integration

`app.py` passes `TOOLS` to the model and executes requests through `run_tool`.
The system prompt tells the model to start from the notices, treat `event_type`
as a heuristic pre-label it may override using the excerpt, call
`search_ticketmaster_events` only for `ticket_relevant` notices, read a notice
in full rather than guess when detail exceeds the excerpt, state a price only
when one appears in text it actually read, and attribute every fact to its
source URL.

- Demo questions (all verified against the live sources):
  - "What aespa US tour dates are on sale, and when do tickets open?"
  - "What exactly does the aespa presale notice say about who can join?"
    (drives a search, then `read_weverse_notice`)
  - "Search YOASOBI notices from the last year for pop-up events."
  - "I want to see NCT TEN live - help me plan."
- Deploy note (Cloud Run): inject `WEVERSE_ACCESS_TOKEN`,
  `WEVERSE_REFRESH_TOKEN`, `TICKETMASTER_API_KEY` and `GOOGLE_CLOUD_PROJECT`
  via `--set-env-vars` or Secret Manager; never bake secrets into the image.
  The refresh-token persistence writes only to the local `.env` and degrades
  gracefully on a read-only filesystem.
- `/bench` is a local tool test page that runs `run_tool` directly with no
  model in the loop. It stays behind `KPOP_DEBUG_UI=1` because an open
  tool-execution endpoint would let any visitor spend the Ticketmaster quota.

## 6. Testing

```powershell
python -m pytest -q                                            # mocked suite
python -m pytest -m "live_weverse or live_ticketmaster" -q -s  # real sources
```

`tests/test_classify.py` covers the classifier, `tests/test_weverse_tool.py`
the original tool's annotation and envelopes, `tests/test_weverse_notice.py`
the gateway and service, `tests/test_ticketmaster.py` the common tool, and
`tests/test_live_e2e.py` the live paths - including a stubbed-LLM test that
drives `run_agent` through search -> ticketmaster -> read against both real
APIs, which is what proves the model really can compose the split tools.
