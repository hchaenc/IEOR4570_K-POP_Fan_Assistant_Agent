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
              │   ├── weverse/          weverse_notices (digest + full text)
              │   ├── merch_appraisal/  appraise_kpop_merch (eBay price + scam check)
              │   └── venue_survival/   venue_survival_kit (OpenStreetMap)
              └── common/         shared services and tools
                  ├── ticketmaster.py  search_ticketmaster_events
                  ├── ebay.py          eBay Browse client (not a tool)
                  └── classify.py      notice event-type helper (not a tool)
```

A module or package is model-callable exactly when it exposes `SCHEMAS` (list
of OpenAI-style function schemas) and `HANDLERS` (tool name -> callable).
`classify.py` and `ebay.py` expose neither, so they stay internal. **Adding a tool means
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
   `mode`, `truncated`, `notice_id`, `text_chars`) for the collapsed trace
   header. The
   full envelope is also sent to the browser under `result` so a grader can
   expand exactly what the model was told; `text` and `excerpt` are never in
   the summary.

Stable error codes: `missing_credentials`, `authentication_failed`,
`community_not_joined`, `ambiguous_artist`, `rate_limited`,
`upstream_schema_changed`, `timeout`, `notice_not_found`,
`unexpected_upstream_error`, `no_results`, `too_few_comparables`,
`venue_not_found`, plus harness-level `unknown_tool` / `bad_arguments` (the two
tools below also return `bad_arguments` for a query or venue under 3 characters).

## 3. The tools

### `weverse_notices(artist, query?, limit?, notice_id?, max_chars?)` - original

One tool, two modes, selected by whether `notice_id` is present, and every
response names which mode it came back as in `mode`.

**Digest mode** (`mode: "digest"`). One signed request fetches up to 300 recent
notices, keeps the last 365 days, deduplicates by id, strips HTML to text and
returns up to `limit` (default 120, which covers the whole window for most
artists) newest-first:

```json
{
  "ok": true, "mode": "digest", "artist": "aespa", "community_id": 125,
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
appear here - that is what full-text mode is for.

Artist names are resolved to a Weverse `urlPath` by trying the hyphenated slug
then the despaced one: Weverse files MONSTA X as `monstax`, so a single guess
would 404. The `artist` field echoes what the user typed, because the model
reuses it as the Ticketmaster keyword and `MONSTAX` matches nothing there.

**Full-text mode** (`mode: "full_text"`, pass `notice_id`).

Returns one notice's full text (default 1500 chars, capped at 5000) plus
`text_chars` and `truncated`, so the model can quote application windows,
membership rules or prices that never fit in an excerpt. It reuses the list
request, because `tabContent` already returns complete bodies - there is no
per-notice endpoint to discover, and no cache to invalidate.

Progressive disclosure is the point: a compact digest first, the raw body only
when the answer needs it (course slides p.29: "return titles + summaries to
start... combine with a 'read page' tool for deeper dives"). The slides suggest
a second tool for that dive; this project keeps it as a mode of the one tool so
each member presents exactly one original tool, and `mode` makes the two
response shapes explicit rather than implied. Dumping all 86 aespa bodies
instead would cost ~42,000 tokens on every turn of the session.

### `appraise_kpop_merch(query, target_price?)` - original

What a merch item is listed for on eBay US, and which listings look risky. The
listings come from `tools/common/ebay.py`; the tool's own work is everything
after that:

1. Drop unofficial goods (`fanmade`, `lomo`, `reprint`, `replica`, ...) and
   bundles or pick-your-member listings, counting each under `excluded`.
2. Sort every title into a category (`photocard`, `album`, `lightstick`,
   `seasons_greetings`, `doll`, `concert_merch`) and a condition (`sealed` /
   `opened` / `opened_no_photocard` for albums, `working` / `not_working` for
   lightsticks, else `new` / `used`), and keep only the query's category and
   condition. When the query names no condition, the most common one is used.
3. Add shipping to the price, leave signed items and weak sellers (under 97%
   positive or under 10 ratings) out of the price, and drop outliers beyond
   1.5 x IQR.
4. Flag anything under 40% of the typical price as a possible fake.

```json
{
  "ok": true, "query": "IVE Wonyoung photocard", "source": "ebay",
  "note": "Prices are current asking prices on eBay US, shipping included, in USD. Not sold prices.",
  "category": "photocard", "condition": "new", "listings_compared": 7,
  "typical_price_usd": 21.0, "typical_range_usd": [16.88, 28.0],
  "excluded": {"unofficial": 2, "bundle_or_multi_choice": 1, "other_category": 0},
  "best_listings": [{"title": "...", "total_usd": 16.5, "ships_from": "US", "seller_feedback_pct": 100.0, "url": "..."}],
  "flagged_listings": [{"title": "...", "total_usd": 5.0, "why": ["price is far below the typical price: possible fake or scam"], "url": "..."}],
  "target_price_usd": 30, "verdict": "overpriced"
}
```

`verdict` appears only with `target_price`: `good_deal` at or under 85% of the
typical price, `fair` up to 115%, else `overpriced`. Fewer than 3 comparable
listings returns `too_few_comparables` (with the `excluded` counts) instead of
a price built on one or two listings, and the message tells the model how to
broaden the query.

### `venue_survival_kit(venue, radius_m?)` - original

What is around a venue, for fans queueing outside. One Nominatim lookup turns
the venue name into coordinates, one Overpass query lists what is mapped
within `radius_m` (default 500, clamped to 200-1500). Returns `nearby` with up
to 3 places per category (`convenience_store`, `station`, `toilets`, `cafe`,
`fast_food`, `pharmacy`), each with `meters`, `walk_min` (80 m/min) and, where
mapped, `opening_hours` / `open_24h` / `paid`; `tips` built from the gaps (no
toilet within 300 m, no 24-hour store, a quieter second station for after the
show); `coordinates`; and a `map_url`.

The `note` field says that an empty category means nothing is mapped there, not
that nothing exists, so the model does not tell a fan there is no toilet.

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

### eBay (`tools/common/ebay.py`)

Browse API `item_summary/search` with an application token (client-credentials
grant), so there is no user login. `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` come
from the Production keyset on developer.ebay.com; `EBAY_ENV=sandbox` switches
host. The token (about 2 hours) and responses are cached in the shared client.

- **Asking prices, not sold prices.** Browse only returns live listings; sold
  data is a restricted API. Every result says so in `note`.
- **eBay US, USD only.** Listings in other currencies are dropped rather than
  converted.
- **No keys, no numbers.** Without keys the tool returns `missing_credentials`;
  it never falls back to sample data, so the model cannot quote a fake price.
- It exposes no `SCHEMAS` / `HANDLERS`: the model cannot search eBay directly,
  only through `appraise_kpop_merch`.

### OpenStreetMap (`tools/originals/venue_survival/osm.py`)

Nominatim and Overpass are free and need no key. Both ask for a descriptive
User-Agent and light use, so results are cached per venue and radius for the
life of the process. Overpass is often busy: a second mirror is tried before
the tool gives up. Coverage depends on volunteers, which is why the result
carries the "not mapped is not the same as not there" note.

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
    (drives a digest call, then the same tool again with a `notice_id`)
  - "Search YOASOBI notices from the last year for pop-up events."
  - "I want to see NCT TEN live - help me plan."
  - "Someone is selling an IVE Wonyoung LOVE DIVE photocard for $30 - is that fair?"
  - "I'm queueing overnight at UBS Arena New York - what's around?"
- Running locally: `uv sync --group dev` then `uv run app.py`
  (`--port` / `--host` to move off 8000). The app boots with no `.env` at all;
  the tools then return `missing_credentials` naming what to add.
- Which project pays for the model: resolution order is
  `--gcp-project` > `GOOGLE_CLOUD_PROJECT` > `gcp-project.txt` (searched in the
  repo, then in its parent directory, like `password.txt`). Keep your own
  project ID in that text file rather than in `.env`, so nobody commits it and
  nobody runs up somebody else's bill - gcloud charges the quota project, not
  the account that logged in.
- Deploy note (Cloud Run): inject `WEVERSE_ACCESS_TOKEN`,
  `WEVERSE_REFRESH_TOKEN`, `TICKETMASTER_API_KEY`, `EBAY_CLIENT_ID`,
  `EBAY_CLIENT_SECRET` and `GOOGLE_CLOUD_PROJECT`
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
covers the original tool's two modes, annotation and envelopes, `tests/test_weverse_notice.py`
the gateway and service, `tests/test_ticketmaster.py` the common tool, and
`tests/test_ebay.py` the eBay client, `tests/test_merch_appraisal.py` and
`tests/test_venue_survival.py` those two original tools, and
`tests/test_live_e2e.py` the live paths - including a stubbed-LLM test that
drives `run_agent` through digest -> ticketmaster -> full-text read against
both real APIs, which is what proves the model really can compose the tools.
