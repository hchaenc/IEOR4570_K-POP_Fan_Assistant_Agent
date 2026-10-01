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
              │   ├── venue_survival/   venue_survival_kit (OpenStreetMap)
              │   └── comeback_trail/   trace_kpop_comeback_era
              └── common/         shared services and tools
                  ├── ticketmaster.py  search_ticketmaster_events
                  ├── ebay.py          eBay Browse client (not a tool)
                  └── classify.py      notice event-type helper (not a tool)
                  ├── youtube.py       YouTube Data API client (not a tool)
                  └── itunes.py        resolve_song_release
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

1. Drop unofficial goods (`fanmade`, `lomo`, `reprint`, `replica`, ...),
   bundles or pick-your-member listings, and titles that do not name the
   query's first word (the group or member), counting each under `excluded`.
2. Sort every title into a category (`photocard`, `album`, `lightstick`,
   `seasons_greetings`, `doll`, `concert_merch`) and a condition (`sealed` /
   `opened` / `opened_no_photocard` for albums, `working` / `not_working` for
   lightsticks, else `new` / `used`), and keep only the query's category and
   condition. When the query names no condition, the most common one is used.
   Variants that share the item's words are dropped unless the query asks for
   them: keyring and mini replicas of a lightstick, CD-player (CDP) / LP /
   vinyl / cassette editions of an album, photocard holders and sleeves.
3. Add shipping to the price, leave signed items and weak sellers (under 97%
   positive or under 10 ratings) out of the price, and drop outliers beyond
   1.5 x IQR.
4. Flag anything under 40% of the typical price as a possible fake.
5. If the upper quartile is 3.5x the lower one or more, the listings are
   different editions: `mixed_versions: true`, `typical_price_usd: null`, no
   verdict, no "far below" flags, and a `mixed_versions_note` telling the model
   to ask which edition the user means.

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

Every card in `best_listings` / `flagged_listings` carries the listing's
`image_url` (eBay's own thumbnail on i.ebayimg.com). The model does not use it:
`index.html` draws the cards with their photos above the answer, linking each
to the eBay listing, and accepts only `https://i.ebayimg.com/` images and
`https://www.ebay.com/` links. The model cannot see the photos, so the prompt
forbids judging authenticity from them.

Measured on live eBay (October 2026): "aespa Armageddon album sealed" mixed a
$14 regular edition with $235-$420 CDP editions, giving a "typical" $235 and
flagging regular albums as fakes; "SEVENTEEN lightstick ver 3" counted $30
keyring replicas and an ATEEZ lightstick. The variant, artist and
mixed-editions rules above come from those two queries.

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
mapped, `opening_hours` / `open_24h` / `paid`; `signals`, computed over every
mapped place rather than the top 3; `coordinates`; and a `map_url`.

```json
"signals": {
  "nearest_toilet_m": 120, "nearest_toilet_paid": true,
  "nearest_food_or_cafe": "Dome Cafe",
  "convenience_store_count": 2, "store_24h_count": 1,
  "station_count": 2, "second_station_extra_walk_min": 2,
  "gaps": []
}
```

`gaps` uses fixed codes: `no_public_toilet_within_300m`,
`no_convenience_store`, `no_store_marked_24h`, `no_station`, `single_station`,
`no_cafe_or_fast_food`, `no_pharmacy`.

The tool returns facts, not advice. An earlier version returned finished tip
sentences, and the model repeated them word for word to every user. Whether a
missing 24-hour store matters depends on whether the fan queues overnight, which
only the conversation knows, so the system prompt tells the model to work out
the user's plan (asking once if it is unclear), lead with the two or three
points that matter for it, and not recite every category.

The `note` field says that an empty category means nothing is mapped there, not
that nothing exists, so the model does not tell a fan there is no toilet.

Each place also carries `lat` / `lon`. The model does not need them: the chat
page (`index.html`) finds any successful `venue_survival_kit` result in the
tool trace and draws a Leaflet map above the answer, with the venue, the search
radius and one colored dot per place (popups show name and walking minutes).
Leaflet loads from cdnjs and tiles from openstreetmap.org; if either is
unreachable the map is skipped and the answer still renders.

### `trace_kpop_comeback_era(artist, release_title, anchor_song?)` - original

Reconstructs the content lifecycle of one K-pop comeback rather than doing a
generic YouTube search.

The tool retrieves several release-oriented searches, filters noise and
unrecognized uploaders, then classifies surviving videos into five phases:

1. `pre_release` - MV teasers, highlight medleys, concept films and trailers
2. `release` - official MVs, performance videos and song-specific content
3. `promotion` - Music Bank, Inkigayo, Music Core, M Countdown and related stages
4. `choreography` - dance practices and choreography content
5. `era_behind` - recording, jacket-shoot, MV and performance behind-the-scenes

The K-pop-specific part is the lifecycle reconstruction itself. The tool does
not hardcode one artist: the same rules have been verified with aespa's
`Drama` / `Armageddon` eras and LE SSERAFIM's `EASY` era.

`anchor_song` lets a user enter the era through a B-side or follow-up track.
For example, `Licorice` can be highlighted inside aespa's wider `Armageddon`
era, while `Smart` can be highlighted inside LE SSERAFIM's `EASY` era.

When the user names only a song, the model first calls the common
`resolve_song_release` tool to find its parent album/EP, then calls
`trace_kpop_comeback_era` with that release and the song as `anchor_song`.

### `search_ticketmaster_events(keyword, city?, country_code?)` - common

Discovery API v2, one request per call. Returns `matched_count` and `events[]`
with name, date, time, timezone, status, venue, city, country, `attractions`
(the headliners), `public_on_sale_at`, `presale_windows`, `ticket_limit`,
`please_note` and the purchase `url`.

### `resolve_song_release(artist, song, country?)` - common

Uses the public iTunes Search API to resolve a song to its parent album, EP or
single. No API key is required.

When several exact song matches exist, original album/EP releases are preferred
over derivative releases such as remixes, sped-up, slowed or instrumental
versions. This prevents a track such as LE SSERAFIM's `Smart` from resolving to
`Smart (Remixes)` instead of the original `EASY` EP.

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
life of the process. Overpass is often busy: each server gets 15 s, then
`maps.mail.ru` is tried (`overpass.kumi.systems` and `overpass.private.coffee`
did not answer within 30 s when measured). If every server fails after the
venue itself was found, the tool still returns `ok: true` with the venue's
coordinates and map link plus `nearby_unavailable: true` and no `nearby` /
`signals`, so the model cannot read empty lists as "nothing nearby"; the page
still draws the venue on its map. That result is not cached. Coverage depends on volunteers, which is why the result
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

### YouTube (`tools/common/youtube.py`)

Uses YouTube Data API v3 with `YOUTUBE_API_KEY`. The shared client searches
public videos, fetches video metadata and statistics, caches responses in
process, and maps quota, authentication and upstream failures to stable tool
errors.

The common client deliberately does not decide what counts as K-pop comeback
content. It only returns video metadata. The original `comeback_trail` tool
owns the domain logic: uploader filtering, noise removal, lifecycle
classification and comeback-era reconstruction.

One broad search was not sufficient for comeback reconstruction because
YouTube relevance ranking often omitted teasers, music-show stages or
behind-the-scenes videos. The original tool therefore performs several
release-oriented searches such as the release name, teaser, music show, dance
practice and behind-the-scenes, then deduplicates the combined results.


### iTunes Search (`tools/common/itunes.py`)

Uses Apple's public iTunes Search API and requires no API key. It searches song
metadata and resolves an exact artist + song match to its parent album, EP or
single.

The resolver removes packaging suffixes such as `- EP`, `- Single` and
`- The 1st Album` to produce the `release_title` consumed by
`trace_kpop_comeback_era`.

The same song can appear in several collections. Original album/EP releases
are preferred over derivative releases whose collection names contain signals
such as remix, remixes, sped up, slowed or instrumental. For example,
LE SSERAFIM's `Smart` resolves to `EASY - EP` rather than `Smart (Remixes)`.

## 5. Chatbot integration

`app.py` passes `TOOLS` to the model and executes requests through `run_tool`.
The system prompt tells the model to start from the notices, treat `event_type`
as a heuristic pre-label it may override using the excerpt, call
`search_ticketmaster_events` only for `ticket_relevant` notices, read a notice
in full rather than guess when detail exceeds the excerpt, state a price only
when one appears in text it actually read, and attribute every fact to its
source URL.

For comeback discovery, the model can compose the shared metadata resolver
with the original comeback tool.
When the user gives only an artist and song, the intended sequence is:
`resolve_song_release(artist, song)`
→ obtain the parent `release_title`
→ `trace_kpop_comeback_era(artist, release_title, anchor_song=song)`
When the user already names the release, the model skips iTunes and calls
`trace_kpop_comeback_era` directly.
This separation is intentional. iTunes answers the factual metadata question
"which release contains this song?", while the original tool performs the
K-pop-specific reasoning over the YouTube comeback-content ecosystem.

- Demo questions (all verified against the live sources):
  - "What aespa US tour dates are on sale, and when do tickets open?"
  - "What exactly does the aespa presale notice say about who can join?"
    (drives a digest call, then the same tool again with a `notice_id`)
  - "Search YOASOBI notices from the last year for pop-up events."
  - "I want to see NCT TEN live - help me plan."
  - "Someone is selling an IVE Wonyoung LOVE DIVE photocard for $30 - is that fair?"
  - "I'm queueing overnight at UBS Arena New York - what's around?"
  - "I like aespa's Licorice. Show me the comeback content."
  - "I like LE SSERAFIM's Smart. Show me the comeback content."
  - "Show me the comeback trail for aespa's Drama era."
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
  `EBAY_CLIENT_SECRET`, `YOUTUBE_API_KEY` and `GOOGLE_CLOUD_PROJECT`
  via `--set-env-vars` or Secret Manager; never bake secrets into the image.
  The iTunes Search API requires no credential.
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
`tests/test_itunes.py` covers exact song matching, release-title cleanup and
preference for original album/EP releases over remix derivatives.
`tests/test_comeback_trail.py` covers K-pop lifecycle classification, noise and
uploader filtering, anchor-song behavior and cross-artist generalization.