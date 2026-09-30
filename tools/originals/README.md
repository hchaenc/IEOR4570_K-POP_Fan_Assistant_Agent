# Original tools — one per team member

Each subpackage here is one member's primary contribution and the course rubric
requires one original tool per person that no other team in the class built.

## Contract

A package registers itself automatically just by exposing two attributes in its
`__init__.py` — `tools/__init__.py` discovers them, so nothing else needs
editing:

```python
SCHEMAS: list[dict]              # OpenAI-style function schemas the model sees
HANDLERS: dict[str, callable]    # tool_name -> function returning a JSON string
```

Rules every original tool follows:

1. **One source, no orchestration.** Return annotated data from your own
   source. Deciding to combine sources is the model's job in its tool loop, or
   belongs in `tools/common/`.
2. **Handlers return a JSON string and never raise** through the loop.
3. **Envelope shape:** success `{"ok": true, ...}`, failure
   `{"ok": false, "error": "<stable_code>", "message": "<short, safe, actionable>",
   "source": "<integration>"}`.
4. **Stable error codes only:** `missing_credentials`, `authentication_failed`,
   `community_not_joined`, `ambiguous_artist`, `rate_limited`,
   `upstream_schema_changed`, `timeout`, `notice_not_found`,
   `unexpected_upstream_error`, `no_results`, `too_few_comparables`,
   `venue_not_found`.
5. **Configuration comes from environment variables** in the git-ignored
   `.env`; never accept credentials as tool arguments and never return tokens,
   headers or raw exceptions.
6. **Describe everything explicitly** (course slides p.23-24): what the tool is
   for, when to call it and when not to, and the format of each argument.
   Error messages are prompt engineering — tell the model what to do next.

Shared API access (Ticketmaster and eBay today; YouTube, iTunes later) lives in
`tools/common/` so several original tools can use one client, cache and error
mapping.

## Current packages

| Package | Owner | Model-facing tools |
|---|---|---|
| `weverse/` | offline planning | `weverse_notices` |
| `merch_appraisal/` | merch price and scam check (eBay via `tools/common/ebay.py`) | `appraise_kpop_merch` |
| `venue_survival/` | venue surroundings and queueing tips (OpenStreetMap) | `venue_survival_kit` |
| _yours here_ | stage / MV slice | - |
| _yours here_ | song / lyrics slice | - |

Nothing is scaffolded for you: create `tools/originals/<your_tool>/__init__.py`
exposing `SCHEMAS` and `HANDLERS` and the registry picks it up on the next
import. A package that exposes neither is skipped silently, so an empty
directory costs nothing.

`Ticketmaster` is deliberately **not** in this folder - it is a shared
`tools/common/` tool that any original tool may call, and it is not what makes
your contribution original.
