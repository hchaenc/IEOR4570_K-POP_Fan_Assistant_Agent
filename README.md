# K-pop Fan Assistant

A tool-calling chat agent that helps K-pop fans go to real events. Ask about an
artist and it reads their official Weverse notices, finds ticketed shows on
Ticketmaster, checks what merch really sells for on eBay, maps what is around
the venue, and reconstructs a comeback era from YouTube. Gemini decides which
tools to call and combines the results; every tool call is shown in the chat
so you can see exactly what the model was told.

IEOR 4570 Project 1 (a tool-calling agent). FastAPI + LiteLLM + Gemini on
Vertex AI.

## What it can do

| Ask something like | Tools it uses |
|---|---|
| "What aespa US tour dates are on sale, and when do tickets open?" | `weverse_notices` → `search_ticketmaster_events` |
| "What exactly does the aespa presale notice say about who can join?" | `weverse_notices` (digest, then full text) |
| "Is $60 a fair price for a SEVENTEEN lightstick ver 3?" | `appraise_kpop_merch` |
| "I'm queueing overnight at Capital One Arena in Washington D.C. – what's around?" | `venue_survival_kit` |
| "I like LE SSERAFIM's Licorice. Show me the comeback content." | `resolve_song_release` → `trace_kpop_comeback_era` |

The chat page draws a map for venue answers and photo cards for merch answers,
keeps several independent conversations in a sidebar, and lists each answer's
sources underneath it.

## Tools

Each team member contributes one **original** tool (`tools/originals/`); shared
API access lives in **common** (`tools/common/`).

| Tool | Kind | Data source | Needs a key | What the tool adds on top of the raw data |
|---|---|---|---|---|
| `weverse_notices` | original | Weverse (signed gateway) | Weverse account tokens | 365-day digest, event-type label with evidence, full-text mode |
| `appraise_kpop_merch` | original | eBay Browse API | `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | drops fakes, bundles, other artists and other editions; compares like with like; typical price, verdict, scam flags |
| `venue_survival_kit` | original | OpenStreetMap (Nominatim + Overpass) | none | nearby stores, stations, toilets, food and pharmacies with walking minutes, plus queue signals and gaps |
| `trace_kpop_comeback_era` | original | YouTube Data API v3 | `YOUTUBE_API_KEY` | filters fan uploads, classifies teasers / MV / performances, orders the comeback trail |
| `search_ticketmaster_events` | common | Ticketmaster Discovery API | `TICKETMASTER_API_KEY` | keeps only the artist's own events; on-sale and presale times |
| `resolve_song_release` | common | iTunes Search API | none | finds the album, EP or single a song belongs to |

Details for every tool (inputs, outputs, error codes, measured quirks of each
data source) are in [docs/TOOLS.md](docs/TOOLS.md).

## How it works

```
index.html  ──/chat──▶  app.py  run_agent(): Gemini ⇄ tools, up to 8 rounds
                           │
                           ▼
                  tools/__init__.py   registry: auto-discovers every package
                  ├── originals/      one original tool per team member
                  └── common/         shared API clients and tools
```

- The model sees every tool's schema and decides what to call; tools never
  call each other. Combining sources (notices → Ticketmaster, song → release
  → comeback trail) happens in the model's tool loop.
- Every tool returns JSON: `{"ok": true, ...}` or
  `{"ok": false, "error": "<stable_code>", "message": "...", "source": "..."}`.
  Error messages tell the model what to do next.
- Conversations are kept in server memory per `session_id`; the model only
  ever sees the current conversation. Nothing is saved: restarting the server
  or refreshing the page starts over.

## Setup

### 1. Prerequisites

- Python 3.10+ and [uv](https://docs.astral.sh/uv/)
- A Google Cloud project with billing and the Vertex AI (Agent Platform) API
  enabled, and the gcloud CLI

```bash
gcloud auth application-default login
uv sync --group dev
```

### 2. Choose who pays for Gemini

gcloud bills the *quota project*, not the account that logged in, so each
person uses their own project. Put your project ID in a file named
`gcp-project.txt` in the repo (or its parent folder); it is git-ignored.

```bash
echo "your-gcp-project-id" > gcp-project.txt
```

Alternatives, in order of priority: `uv run app.py --gcp-project <id>`, then the
`GOOGLE_CLOUD_PROJECT` environment variable, then `gcp-project.txt`.

### 3. API keys

```bash
cp .env.example .env
```

Then fill in `.env` (it is git-ignored; never commit it):

| Variable | Where to get it |
|---|---|
| `TICKETMASTER_API_KEY` | [developer.ticketmaster.com](https://developer.ticketmaster.com/) → My Apps → Consumer Key (free, 5000 requests/day) |
| `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | [developer.ebay.com](https://developer.ebay.com/) → Application Keysets → **Production** keyset (App ID, Cert ID). A new keyset stays disabled until you opt out of Marketplace Account Deletion notifications ("I do not persist eBay data"). |
| `YOUTUBE_API_KEY` | Google Cloud console → APIs & Services → enable YouTube Data API v3 → Credentials → API key |
| `WEVERSE_HMAC_ACTIVE_KEY`, `WEVERSE_APP_ID`, `WEVERSE_APP_SECRET` | Weverse web client identifiers; ask a teammate (see [docs/TOOLS.md](docs/TOOLS.md)) |
| `WEVERSE_ACCESS_TOKEN`, `WEVERSE_REFRESH_TOKEN`, `WEVERSE_DEVICE_ID` | your own Weverse account, see below |

OpenStreetMap and iTunes need no key. A missing key does not crash anything:
that tool returns `missing_credentials` and the model says so.

### 4. Weverse tokens: one account per person

Weverse refresh tokens **rotate on every use**. If two people (or two running
servers, or your browser) use the same token, the second refresh looks like a
stolen token and Weverse revokes the whole set for everyone. So:

- Each teammate uses **their own Weverse account** and gets their own tokens.
- Get them with the login script rather than copying browser cookies (the
  browser keeps rotating the same token):

  ```bash
  # put WEVERSE_USERNAME and WEVERSE_PASSWORD in .env first
  uv run bootstrap_login.py      # emails you a code, then writes the tokens to .env
  ```
- Join the artist's community on Weverse with that account, or the tool returns
  `community_not_joined`.
- Run one server at a time. The app refreshes the access token itself and saves
  the newest refresh token back to `.env`.

If you see `authentication_failed`, stop the server, run `bootstrap_login.py`
again and restart.

### 5. Run

```bash
uv run app.py              # --port / --host to change, default 127.0.0.1:8000
```

Open http://localhost:8000. Restart the server after changing any Python file
or `.env`; changes to `index.html` only need a page refresh.

With `KPOP_DEBUG_UI=1` in `.env`, http://localhost:8000/bench runs any tool
directly with no model in the loop, which is handy for checking a key or a data
source. Keep it off anywhere public.

Optional settings: `KPOP_ASSISTANT_MODEL` (default
`vertex_ai/gemini-3.5-flash-lite`) and `VERTEX_LOCATION` (default `global`).

## Tests

```bash
uv run pytest -q -m "not live_weverse and not live_ticketmaster"   # offline suite, no network
uv run pytest -q -s -m "live_weverse or live_ticketmaster"         # real APIs, uses your .env
```

The offline suite fakes every network call and covers each tool's logic,
error envelopes, the registry and the agent loop (including a stubbed model).
Live tests skip themselves when their key is missing. A plain `uv run pytest`
runs both, so with a filled-in `.env` it also calls the real APIs, including a
Weverse token refresh: stop your server first (see the Weverse section).

## Project layout

```
app.py                  FastAPI server, system prompt, agent loop (run_agent)
index.html              chat page: conversations sidebar, tool traces, map, merch cards
bench.html              /bench tool test page (KPOP_DEBUG_UI=1 only)
bootstrap_login.py      one-time Weverse login with an emailed code
tools/
  __init__.py           registry: TOOLS (schemas) and run_tool()
  originals/
    weverse/            weverse_notices: gateway client, notice service, tool
    merch_appraisal/    appraise_kpop_merch
    venue_survival/     venue_survival_kit + osm.py (Nominatim, Overpass)
    comeback_trail/     trace_kpop_comeback_era
  common/
    ticketmaster.py     search_ticketmaster_events
    itunes.py           resolve_song_release
    ebay.py             eBay Browse client (internal, not a tool)
    youtube.py          YouTube Data API client (internal)
    classify.py         notice event-type classifier (internal)
docs/TOOLS.md           tool contract and data-source notes
tests/                  pytest suite
```

## Adding a tool

Create `tools/originals/<name>/__init__.py` (or a module in `tools/common/`)
that exposes:

```python
SCHEMAS: list[dict]              # OpenAI-style function schemas the model sees
HANDLERS: dict[str, callable]    # tool name -> function returning a JSON string
```

The registry picks it up on the next start; nothing else needs editing. A
module that exposes neither (like `ebay.py` or `classify.py`) stays internal.
Read the rules in [tools/originals/README.md](tools/originals/README.md) first:
handlers never raise, credentials come only from `.env`, and the description
must say when to call the tool and when not to.

## Limitations

- **Ticketmaster** does not sell in Korea or Japan, and publishes prices for
  only some K-pop events; an empty result there is an honest answer.
- **eBay** gives current asking prices on eBay US, not sold prices.
- **OpenStreetMap** coverage depends on volunteers: "not mapped" is not the same
  as "not there". The public Overpass servers are sometimes overloaded; the tool
  then still returns the venue's location and says nearby places are
  unavailable.
- **Weverse** covers the last 365 days of notices, and some communities
  (e.g. MONSTA X, ATEEZ) have no notice feed at all.
- Conversations are not saved.

## Deploying (notes)

Cloud Run works, but Weverse token rotation needs care: use a separate Weverse
account for the deployment, run a single instance (`--max-instances=1`), and
inject keys as environment variables or Secret Manager secrets, never in the
image. The rotated refresh token is only saved to a local `.env`, so a restarted
container would fall back to an already-used token; storing it in Secret
Manager is the fix still to be built. Leave `KPOP_DEBUG_UI` unset in
production.

## Team

By commit history: Jiashu Chen (merch appraisal, venue survival kit, chat
page), Lixiao Yang (Weverse notices, Ticketmaster, tool framework), Xinyi Wang
(comeback trail, iTunes resolver).

## License

MIT, see [LICENSE](LICENSE).
