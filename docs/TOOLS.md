# Tools Framework & the Offline-Planning Tool

How the tool system is organized, the unified tool contract, and everything
operational about the offline-planning tool (Weverse notices + Ticketmaster
events). Booking.com was cancelled from the project scope: it is not
planned, implemented, or referenced anywhere.

## 1. Architecture

```
index.html  <-->  app.py (FastAPI /chat, LiteLLM harness, run_agent loop)
                        |
                        v
              tools/  (auto-discovering registry)
              ├── __init__.py            # scans tools/*/ for SCHEMAS + HANDLERS
              ├── offline_planning/      # this slice's registered tools
              │   ├── __init__.py        # SCHEMAS + HANDLERS (two tools)
              │   ├── tool.py            # plan_offline_attendance: args + envelope
              │   ├── read_tool.py       # read_weverse_notice: full text on demand
              │   ├── pipeline.py        # notices -> judge -> conditional TM -> merge
              │   ├── judge.py           # tiered (title-first) notice classifier
              │   └── README.md          # full component/input/output documentation
              └── integrations/          # data-source adapters, NOT registered
                  ├── weverse/           # signed gateway client + notice search/read
                  └── ticketmaster/      # Discovery API client + event shaping
```

`app.py` only ever imports `from tools import TOOLS, run_tool`. The registry
imports every `tools/<name>/` subpackage and registers the ones exposing
`SCHEMAS` + `HANDLERS`; `tools/integrations/` defines neither, so adapters
stay internal. **Adding a tool = creating one folder; adding a data source =
creating one adapter folder. No shared-file edits.**

## 2. Tool contract (unified input/output)

A tool package exposes two attributes in its `__init__.py`:

```python
SCHEMAS: list[dict]             # OpenAI-style function schemas the model sees
HANDLERS: dict[str, callable]   # {"tool_name": handler_function}
```

Handler rules:

1. Arguments: plain JSON-compatible values only (str/int/bool/None/lists/dicts).
2. Return: a JSON string via `json.dumps(...)` - never raise through the loop.
3. Success envelope: `{"ok": true, ...}`; failure envelope:
   `{"ok": false, "error": "<stable_code>", "message": "<short, safe, actionable>"}`
   plus `"source": "<integration>"` when a specific adapter failed.
4. Configuration via environment variables only (local `.env`, git-ignored).
5. Trace safety: `app.safe_trace_result()` whitelists the scalar summary keys
   (`ok`, `error`, `scanned_count`, `matched_count`, `notice_matched`,
   `truncated`, `notice_id`, `text_chars`) shown in the chat UI. Notice bodies
   and excerpts exist only so the model can audit a label, so they are
   deliberately excluded from the whitelist and stay in the model context.

Stable error codes: `missing_credentials`, `authentication_failed`,
`community_not_joined`, `ambiguous_artist`, `rate_limited`,
`upstream_schema_changed`, `timeout`, `unexpected_upstream_error`,
`notice_not_found`, plus harness-level `unknown_tool` / `bad_arguments`.

## 3. The offline-planning tools

Full component/input/output documentation lives in
[`tools/offline_planning/README.md`](../tools/offline_planning/README.md).
Summary:

```
plan_offline_attendance(artist, city?, country_code?, notice_query?)
read_weverse_notice(artist, notice_id, max_chars?)
```

`plan_offline_attendance` performs the whole pipeline in one call:

1. **Notices** - `integrations/weverse` fetches the artist's official Weverse
   notices (last 365 days, dynamic slug resolution, no community join needed).
   Every in-window notice reaches the judge, up to 120.
2. **Judge** - `judge.py` labels each notice `ticketed_event` / `popup` /
   `fan_event` / `merchandise` / `online_event` / `announcement` with a
   tiered bilingual hint scan (title first, body only when the title is
   silent) and decides `should_search` (ticketed only).
3. **Ticketmaster** - `integrations/ticketmaster` searches Discovery once per
   unique `(keyword, city, country_code)` for ticketed notices, keeping only
   events whose headliner actually is the artist.
4. **Merge** - one envelope: trace-safe summary keys plus `notices[]`, where
   each notice carries its own evidence (`event_type`, `matched_signal`,
   `matched_field`, `excerpt`), and `ticketmaster_events[]`.

`read_weverse_notice` is the escalation path. The course guidance for
search-style tools is "return titles + summaries to start... combine with a
'read page' tool for deeper dives", so the planning envelope ships a
signal-centered 180-character excerpt per notice and this tool returns the
whole body (default 1500 chars, capped at 5000) when a question turns on
detail the excerpt cannot hold. It reuses the list request, because the
`tabContent` endpoint already returns complete bodies - there is no
per-notice endpoint to discover.

Why the classifier stays in code: it decides how Ticketmaster quota is spent,
and it costs zero model tokens either way. Dumping all 86 aespa bodies into the
context instead would cost ~42,000 tokens per turn, re-sent on every later
turn of the session, and would still leave the quota decision unexplained.

Known limits encoded in the system prompt and the tool docs: notices cover
365 days; Ticketmaster publishes no price range for most K-pop events (and no
seat inventory at all) so the tool reports on-sale times, presale windows and
ticket limits instead; a community with no NOTICE tab answers HTTP 404 and is
reported as such rather than as a transient failure.

## 4. Data-source operations

### Weverse (tools/integrations/weverse)

The original `MujyKun/Weverse` library is dead: its login endpoint rejects
everything with `-10004` and its content host no longer resolves in DNS. The
adapter implements the current contract instead: reads go through the Naver
gateway with a Naver-style HMAC-SHA1 request signature, auth uses bearer
tokens obtained once by a human.

Token lifecycle and auto-refresh:

| Token | Lifetime | Notes |
|---|---|---|
| `WEVERSE_ACCESS_TOKEN` | ~3 days | auto-refreshed on 401 |
| `WEVERSE_REFRESH_TOKEN` | ~90 days | rotates on every refresh; newest value persisted back to `.env` |

- Cold start with only a refresh token: exchanged for a fresh pair first.
- Any 401: one automatic refresh + retry inside the same call.
- When the whole token family is revoked (e.g. refreshed too often in
  probes, error `-10019`): re-login in a browser and copy the fresh
  `we2_access_token` / `we2_refresh_token` / `we2_device_id` cookies into
  `.env`. `bootstrap_login.py` (repo root) is the alternative OTP login
  flow: `--request-otp` then `--verify-code <code>`.

### Ticketmaster (tools/integrations/ticketmaster)

Discovery API v2 with a free key (`TICKETMASTER_API_KEY`, 5000
requests/day, in-process response cache). Events are searched with
`classificationName=Music` in Discovery's own relevance order (date sorting
buries the artist's real tour under title matches), and `extract_events`
keeps only events whose published `attractions` match the keyword. It shapes
name, dates, timezone, venue, headliners, public on-sale time, presale
windows, ticket limit and the official purchase URL, skipping malformed
entries and tolerating the missing `priceRanges` that most K-pop events
report.

## 5. Chatbot integration

- The harness (`app.py`, unchanged course pattern) passes `TOOLS` to the
  model and executes requests through `run_tool`. The system prompt tells
  the model that `event_type` is a heuristic pre-label it may override using
  the excerpt, to call `read_weverse_notice` rather than guess when an answer
  turns on detail the excerpt cannot hold (at most three notices per answer,
  never quote a notice it did not read), to treat `matched_count: 0` as an
  honest "not on Ticketmaster", to state a price only when one appears in text
  it actually read, and to attribute every fact to its source URL.
- Suggested demo questions (all verified against the live sources):
  - "What aespa tour dates are on sale in the US, and when do tickets open?"
  - "What exactly does the aespa presale notice say about membership
    requirements?" (drives `plan_offline_attendance` then `read_weverse_notice`)
  - "Search YOASOBI notices from the last year for pop-up events."
  - "I want to see NCT TEN live - help me plan."
  - Note: MONSTA X and ATEEZ are not usable demos - their Weverse communities
    expose no NOTICE feed, so the tool correctly returns `community_not_joined`.
- Deploy note (Cloud Run): inject `WEVERSE_ACCESS_TOKEN`,
  `WEVERSE_REFRESH_TOKEN`, `TICKETMASTER_API_KEY` via `--set-env-vars` or
  Secret Manager; never bake secrets into the image. The token persistence
  writes only to the local `.env` and degrades gracefully on read-only
  filesystems.

## 6. Testing

```powershell
python -m pytest -q                                           # mocked only, no network
python -m pytest -m "live_weverse or live_ticketmaster" -q -s # both real sources
```

`tests/test_live_e2e.py::test_live_full_chain_single_tool_call` stubs the
LLM and drives `app.run_agent` through the real registry and both live
sources, so the entire production path is verified except the model vendor
call itself.
