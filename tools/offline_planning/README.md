# offline_planning — the offline event-planning tool

One model-callable tool that packages the whole offline-planning slice:
official Weverse notices, per-notice event classification, and conditional
Ticketmaster event search with on-sale times, presale windows and ticket
limits. It matches the project architecture where "offline planning" is a
single tool backed by several data sources.

```
model call: plan_offline_attendance(artist, city?, country_code?, notice_query?)
                |
                v
        tool.py (handler)  ->  pipeline.py (orchestration)
                |                    |
                |                    ├── judge.py          classify each notice
                |                    ├── integrations/weverse      (notices)
                |                    └── integrations/ticketmaster (events)
                v
        one merged JSON envelope back to the LLM
```

## Components

| File | Role | Input | Output |
|---|---|---|---|
| `__init__.py` | Registry contract: exposes `SCHEMAS` (model-facing schema list) and `HANDLERS` (name -> handler). The `tools/` registry auto-discovers them; no shared file edits needed. | - | - |
| `tool.py` | `plan_offline_attendance_tool`: validates/normalizes arguments, calls the pipeline, serializes the result dict to a JSON string for the tool loop. | tool arguments (see below) | JSON string of the result envelope |
| `pipeline.py` | Orchestrator `plan_offline_attendance`: (1) fetch every in-window notice, (2) classify them, (3) search Ticketmaster once per unique ticketed keyword, (4) merge into one envelope with trace-safe summary keys; a Ticketmaster failure is reported inside a successful envelope instead of discarding the notices. | artist, city, country_code, notice_query + optional injected `notice_service` / `event_client` (for tests) | result dict (envelopes below) |
| `judge.py` | Deterministic, priority-ordered bilingual classifier implementing the decision protocol in `docs/TOOLS.md`: labels each notice `ticketed_event` / `popup` / `fan_event` / `merchandise` / `online_event` / `announcement`, records the hint that fired, decides `should_search`, and de-duplicates Ticketmaster search arguments. | notice dict / notice list + geo filters | decision dicts (`classify_notice`, `classify_notices`, `ticketed_decisions`) |
| `../integrations/weverse/` | Data-source adapter (NOT a registered tool): signed Weverse gateway client (`client.py`: tokens, auto-refresh) + notice search (`notices.py`: `search_notices`). | artist, query | `search_notices` result dict |
| `../integrations/ticketmaster/` | Data-source adapter (NOT a registered tool): Discovery API client (`service.py`: `EventSearchClient.search_events`, in-process cache) + event shaping (`extract_events`). | keyword, city, country_code | raw Discovery dict / shaped event list |

The classifier is a deterministic pre-filter. The LLM still receives every
notice and both sources' data, so it makes the final judgment when writing
the answer; the pipeline only decides where to spend Ticketmaster quota.

## Tool input (model-facing)

| Argument | Type | Required | Meaning |
|---|---|---|---|
| `artist` | string | yes | Artist name as typed, e.g. `"aespa"`, `"MONSTA X"`, `"ten"`. Also used verbatim as the Ticketmaster keyword. |
| `city` | string \| null | no | Ticketmaster city filter, e.g. `"New York"` |
| `country_code` | string \| null | no | Ticketmaster ISO country filter, default `"US"` |
| `notice_query` | string \| null | no | Keyword filter for notices, e.g. `"ticket"` |

## Tool output (JSON envelope)

Success:

```json
{
  "ok": true,
  "artist": "aespa",
  "scanned_count": 300,
  "matched_count": 4,
  "truncated": false,
  "notice_matched": 86,
  "notice_count": 86,
  "notices": [
    {
      "notice_id": "36316",
      "title": "[NOTICE] 2026-27 aespa LIVE TOUR Announcement",
      "published_at": "2026-04-21T04:00:00+00:00",
      "url": "https://weverse.io/aespa/notice/36316",
      "event_type": "ticketed_event",
      "ticket_relevant": true
    }
  ],
  "decisions": [
    {
      "notice_id": "36316",
      "title": "[NOTICE] 2026-27 aespa LIVE TOUR Announcement",
      "event_type": "ticketed_event",
      "matched_signal": "ticket",
      "ticketmaster_search": {
        "should_search": true,
        "keyword": "aespa",
        "city": null,
        "country_code": "US"
      }
    }
  ],
  "ticketmaster_searched": ["aespa"],
  "ticketmaster_events": [
    {
      "name": "aespa LIVE TOUR - SYNK : COMPLæXITY - in DALLAS",
      "event_id": "0C0064953638B705",
      "date": "2026-09-29",
      "time": "20:00:00",
      "timezone": "America/Chicago",
      "status": "onsale",
      "venue": "American Airlines Center",
      "city": "Dallas",
      "country": "United States",
      "attractions": ["aespa"],
      "url": "https://www.ticketmaster.com/event/...",
      "price_min": null,
      "price_max": null,
      "currency": null,
      "public_on_sale_at": "2026-05-06T20:00:00Z",
      "presale_windows": [
        { "name": "MY Membership Presale", "starts": "2026-05-06T16:00:00Z" }
      ],
      "ticket_limit": "Please note: There is a ticket limit of 6 tickets per person and per credit card on this event.",
      "please_note": "Tickets are not available at the Box Office on the first day of the public on-sale...",
      "matched_keyword": "aespa"
    }
  ]
}
```

Field notes:

- Trace-safe summary keys (`ok`, `scanned_count`, `matched_count`,
  `notice_matched`, `truncated`) are what the chat UI shows; the large payload
  keys (`notices`, `decisions`, `ticketmaster_events`) stay in the model
  context but out of the visible trace.
- `matched_count` = number of Ticketmaster events matched across all
  ticketed notices (de-duplicated by search arguments). `notice_matched` =
  how many in-window notices matched the query, so "read 86 notices, found 4
  Ticketmaster events" is expressible instead of the two counts colliding.
- `scanned_count` = notices inspected (up to `NOTICE_SCAN_LIMIT = 300` per
  call). The upstream NOTICE endpoint ignores every pagination parameter
  (pageNo/from/after all return identical content), but honors large
  `limit` values, so the scan is a single request covering the most recent
  300 notices - for major artists that spans the whole 365-day window
  (verified: BTS, 1040+ total notices, oldest of 300 was 338 days old).
  `truncated: true` means the scan cap was hit AND its boundary is still
  inside the window.
- Every in-window notice reaches the judge, up to
  `PLANNING_NOTICE_LIMIT = 120`. The standalone notice search keeps its own
  ten-result presentation cap.
- **Prices are usually unavailable.** The Discovery API omits `priceRanges`
  entirely for K-pop events - verified live on the aespa SYNK : COMPLæXITY
  tour and the MONSTA X THE X : NEXUS tour, both `status: onsale` - so
  `price_min` / `price_max` / `currency` are normally null. What Discovery
  does always provide is `public_on_sale_at`, named `presale_windows`,
  `ticket_limit`, `please_note` and the purchase `url`. The tool description
  and the system prompt say this out loud so the model links the event page
  instead of inventing a number.
- `attractions` is the headliner list Ticketmaster publishes per event, and
  the pipeline filters on it (`extract_events(data, keyword=...)`). Keyword
  search also matches event titles: for the artist TEN, date-sorted
  Discovery returned "Mt. Joy 2026: Celebrating 10 Years Of Mt. Joy" and
  "Chance The Rapper - Coloring Book 10 Year Anniversary" as the first ten
  hits. A single-word keyword must match an attraction exactly (so the band
  El Ten Eleven is dropped too); a multi-word one may match a contiguous run
  inside a longer attraction name.
- Requests stay in Discovery's own relevance order rather than
  `sort=date,asc`, because date sorting pushes the artist's real tour off the
  first page; `extract_events` restores chronological order after filtering.
- `ticketmaster_error` appears inside an otherwise successful envelope when
  Ticketmaster fails (quota, outage). The already-fetched notices are still
  delivered, because the official notice is what tells a fan where to buy
  when the artist sells outside Ticketmaster.
- Artist names are resolved to a Weverse `urlPath` by trying the hyphenated
  slug and then the despaced one: Weverse files MONSTA X as `monstax`, so a
  single guess would 404 on the name in the project's own demo question.
- Some communities expose no NOTICE tab at all and answer HTTP 404 (MONSTA X
  and ATEEZ both do). That is permanent, so it maps to `community_not_joined`
  with an explicit message rather than "try again later".

Failure (`ok: false`) carries the stable error code, a safe message, and the
failing data source:

```json
{
  "ok": false,
  "error": "community_not_joined",
  "message": "This artist's Weverse community publishes no notice feed, so official announcements cannot be searched here. Say so rather than retrying.",
  "source": "weverse"
}
```

Error codes: `missing_credentials`, `authentication_failed`,
`community_not_joined`, `ambiguous_artist`, `rate_limited`,
`upstream_schema_changed`, `timeout`, `unexpected_upstream_error` (plus
harness-level `unknown_tool` / `bad_arguments`), with `source` set to
`weverse` or `ticketmaster`.

## Classification rules (`judge.py`)

Each notice gets one `event_type` from a priority-ordered, bilingual hint
scan, and only `ticketed_event` spends Ticketmaster quota.

| Order | Label | Meaning | Ticketmaster |
|---|---|---|---|
| 1 | `online_event` | streamed / watch-online | no |
| 2 | `popup` | pop-up store, exhibition | no |
| 3 | `fan_event` | offline attendance by membership application | no |
| 4 | `merchandise` | goods sales, including on-site booths | no |
| 5 | `ticketed_event` | seats to buy | **yes** |
| 6 | `announcement` | everything else | no |

Priority is the whole point. Most Weverse notices are Korean, and a Korean
broadcast pre-recording notice (`사전녹화 ... 참여 안내`) mentions tickets and
reservation in its boilerplate, while a tour merchandise notice contains
"tour" and an online streaming notice contains "ticket". Testing ticket hints
first would send all of them to Ticketmaster and match nothing. It also means
a hint list cannot simply grow: `생방송` ("live broadcast") had to be dropped
from the online hints because nine in-person pre-recording notices were
labeled online_event by it.

Live effect on aespa's 86 in-window notices: 18 used to be labeled
`ticketed_event` (11 wasted Ticketmaster searches); now 8 are, with the rest
landing on `announcement` 36, `merchandise` 14, `fan_event` 13,
`online_event` 10, `popup` 5. Tune the hint lists in `judge.py` only, and
re-check the mix against a live artist before trusting a change.

## Environment

```dotenv
WEVERSE_ACCESS_TOKEN=<jwt>        # ~3 days, auto-refreshed on 401
WEVERSE_REFRESH_TOKEN=<jwt>       # ~90 days, rotated and persisted back to .env
TICKETMASTER_API_KEY=<key>        # free key, 5000 requests/day, cached in-process
```

Token re-authentication and all endpoint details: `docs/TOOLS.md` and
`bootstrap_login.py`.

## Tests

```powershell
python -m pytest tests/test_offline_planning.py -q                 # mocked pipeline + judge + registry
python -m pytest -m "live_weverse or live_ticketmaster" -q -s      # both real sources
```
