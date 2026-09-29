# offline_planning — the offline event-planning tools

Two model-callable tools that package the whole offline-planning slice:
`plan_offline_attendance` fetches official Weverse notices, classifies them
and conditionally searches Ticketmaster; `read_weverse_notice` returns the
full text of any notice the first tool listed. Together they implement
progressive disclosure - a compact digest with evidence per notice, and an
escalation path when the digest is not enough.

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
        one merged JSON envelope, each notice carrying its own evidence
                |
                v   (only when the excerpt is not enough)
model call: read_weverse_notice(artist, notice_id, max_chars?)
                ->  read_tool.py  ->  integrations/weverse.read_notice()
                ->  one notice's full text
```

## Components

| File | Role | Input | Output |
|---|---|---|---|
| `__init__.py` | Registry contract: exposes `SCHEMAS` (model-facing schema list) and `HANDLERS` (name -> handler). The `tools/` registry auto-discovers them; no shared file edits needed. | - | - |
| `tool.py` | `plan_offline_attendance_tool`: validates/normalizes arguments, calls the pipeline, serializes the result dict to a JSON string for the tool loop. | tool arguments (see below) | JSON string of the result envelope |
| `read_tool.py` | `read_weverse_notice_tool`: the escalation path - returns one notice's full text (default 1500 chars, capped at 5000) for a `notice_id` copied from a previous envelope. | artist, notice_id, max_chars | JSON string of the read envelope |
| `pipeline.py` | Orchestrator `plan_offline_attendance`: (1) fetch every in-window notice, (2) classify them, (3) search Ticketmaster once per unique ticketed keyword, (4) merge into one envelope with trace-safe summary keys; a Ticketmaster failure is reported inside a successful envelope instead of discarding the notices. Also builds each notice's evidence `excerpt`. | artist, city, country_code, notice_query + optional injected `notice_service` / `event_client` (for tests) | result dict (envelopes below) |
| `judge.py` | Deterministic, **tiered** bilingual classifier implementing the decision protocol in `docs/TOOLS.md`: the title decides, the body is consulted only when the title carries no signal. Labels each notice `ticketed_event` / `popup` / `fan_event` / `merchandise` / `online_event` / `announcement`, records the hint and which field matched it, decides `should_search`, and de-duplicates Ticketmaster search arguments. | notice dict / notice list + geo filters | decision dicts (`classify_notice`, `classify_notices`, `ticketed_decisions`) |
| `../integrations/weverse/` | Data-source adapter (NOT a registered tool): signed Weverse gateway client (`client.py`: tokens, auto-refresh) + notice search and single-notice read (`notices.py`: `search_notices`, `read_notice`, `get_notice_service`). | artist, query, notice_id | result dict |
| `../integrations/ticketmaster/` | Data-source adapter (NOT a registered tool): Discovery API client (`service.py`: `EventSearchClient.search_events`, in-process cache) + event shaping (`extract_events`). | keyword, city, country_code | raw Discovery dict / shaped event list |

`get_notice_service()` returns one lazily shared service per process, so both
tools reuse the same authenticated client instead of paying a 401-plus-refresh
cycle on every call.

The classifier is a deterministic pre-filter, not an oracle. Every label ships
with the word that produced it (`matched_signal`), which field it came from
(`matched_field`) and the text around it (`excerpt`), so the LLM can disagree
using evidence it can actually read. The pipeline only decides where to spend
Ticketmaster quota.

## Tool input (model-facing)

`plan_offline_attendance`:

| Argument | Type | Required | Meaning |
|---|---|---|---|
| `artist` | string | yes | Artist name as typed, e.g. `"aespa"`, `"MONSTA X"`, `"ten"`. Also used verbatim as the Ticketmaster keyword. |
| `city` | string \| null | no | Ticketmaster city filter, e.g. `"New York"` |
| `country_code` | string \| null | no | Ticketmaster ISO country filter, default `"US"` |
| `notice_query` | string \| null | no | Keyword filter for notices, e.g. `"ticket"` |

`read_weverse_notice`:

| Argument | Type | Required | Meaning |
|---|---|---|---|
| `artist` | string | yes | The same artist value used in the planning call |
| `notice_id` | string | yes | Copied verbatim from `notices[]`; never invented |
| `max_chars` | integer \| null | no | Text budget, default 1500, capped at 5000 |

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
      "matched_signal": "tour",
      "matched_field": "title",
      "excerpt": "Hello. We are aespa. 2026-27 aespa LIVE TOUR ...",
      "ticket_relevant": true
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

`read_weverse_notice` success:

```json
{
  "ok": true,
  "artist": "aespa",
  "notice_id": "35315",
  "title": "[NOTICE/REVISED] 2026-27 aespa LIVE TOUR - SYNK : COMPLaeXITY- US/CA Membership (GL) Pre-Sale",
  "published_at": "2026-04-27T05:00:00+00:00",
  "url": "https://weverse.io/aespa/notice/35315",
  "text": "Hello. This is the notice body, HTML stripped, plain text only…",
  "text_chars": 4210,
  "truncated": true
}
```

Field notes:

- Trace-safe summary keys (`ok`, `scanned_count`, `matched_count`,
  `notice_matched`, `truncated`, `notice_id`, `text_chars`) are what the chat
  UI shows. `text` and `excerpt` are deliberately **not** whitelisted: they
  exist so the model can audit a label, not to be echoed in the interface.
- Every notice carries why it was labeled: `matched_signal` (the word that
  fired), `matched_field` (`"title"`, `"body"`, or `null`) and `excerpt`. When
  the body decided the label the excerpt is centered on the matched signal
  (±80 chars) rather than the head of the notice, because a head excerpt of an
  8,000-character notice is greeting boilerplate and proves nothing.
- Removing the duplicated `decisions[]` array paid for the excerpts: the aespa
  envelope went from 47,387 characters with zero body text to 45,515
  characters carrying 86 excerpts.
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
`upstream_schema_changed`, `timeout`, `unexpected_upstream_error`,
`notice_not_found` (plus harness-level `unknown_tool` / `bad_arguments`), with
`source` set to `weverse` or `ticketmaster`.

## Classification rules (`judge.py`)

Each notice gets one `event_type` from a **tiered**, bilingual hint scan, and
only `ticketed_event` spends Ticketmaster quota.

| Order | Label | Meaning | Ticketmaster |
|---|---|---|---|
| 1 | `online_event` | streamed / watch-online | no |
| 2 | `popup` | pop-up store, exhibition | no |
| 3 | `fan_event` | offline attendance by membership application | no |
| 4 | `merchandise` | goods sales, including on-site booths | no |
| 5 | `ticketed_event` | seats to buy | **yes** |
| 6 | `announcement` | everything else | no |

Two tiers, in this order:

1. **The title decides.** Agency titles are written to be self-describing
   (`Ticket Reservation & Admission Instructions`, `사전녹화 참여 안내`).
2. **The body is consulted only when the title carried no signal**, and with a
   deliberately narrower hint list.

Both tiers use the same ladder above, evaluated in order, so a body hit on
`online_event` still outranks `ticketed_event`.

Why the split rather than one scan over title+body: bodies are long and full of
boilerplate. Scanning them together made a BTS legal-action notice look
ticketed because it mentions "ticket", and demoted aespa's real `Ticket
Reservation & Admission` notice to `online_event` because its body offers a
streamed alternative. Scanning titles only would miss the cases where the type
appears nowhere in the title (lightstick sales, streamed birthday parties).
`TICKETED_BODY_HINTS` therefore drops the generic English words (`tour`,
`concert`, `ticket`) that the title tier can safely use, and `생방송` is allowed
in the body tier only because pre-recording notices are caught by their titles
first.

Live effect on aespa's 86 in-window notices: 45 labels came from the title, 6
from the body, 35 stayed `announcement`. Mix: `announcement` 35,
`merchandise` 14, `ticketed_event` 10, `fan_event` 13, `online_event` 9,
`popup` 5. Before tiering, 18 were labeled `ticketed_event` (11 wasted
Ticketmaster searches) and 8 after the first priority fix. On BTS's 209
notices, the body tier recovered 14 labels including the whole `BTS THE CITY
ARIRANG` pop-up series, whose titles carry no type word.

Tune the hint lists in `judge.py` only, and re-check the mix against a live
artist before trusting a change.

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
