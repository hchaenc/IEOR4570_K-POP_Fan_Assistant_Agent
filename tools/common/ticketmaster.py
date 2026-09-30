"""Common tool: Ticketmaster Discovery API event search.

Shared by any original tool that needs ticketed events. Discovery API v2, free
key, 5000 requests/day. This client keeps a small in-process response cache so
repeated demo questions do not burn quota. Live seat inventory is NOT available
through Discovery (that is a partner API), and priceRanges is published for
only some events - for K-pop tours it is usually absent. What Discovery does
always provide is the public on-sale timestamp, named presale windows, the
ticket limit and the purchase URL.

Coverage is territorial: North America and Europe work, South Korea and Japan
do not, because Ticketmaster does not sell there.
"""

from __future__ import annotations

import json
import os
import re
import threading

import requests
from dotenv import load_dotenv

DISCOVERY_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
PAGE_SIZE = 10
CACHE_MAX_ENTRIES = 100
REQUEST_TIMEOUT_SECONDS = 20
MAX_PRESALE_WINDOWS = 3
PLEASE_NOTE_MAX_CHARS = 240


class TicketmasterError(Exception):
    """Adapter-internal error carrying a stable tool-layer error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MissingCredentials(TicketmasterError):
    def __init__(self):
        super().__init__(
            "missing_credentials",
            "Ticketmaster is not configured. Set TICKETMASTER_API_KEY in the local .env.",
        )


class AuthenticationFailed(TicketmasterError):
    def __init__(self):
        super().__init__(
            "authentication_failed",
            "Ticketmaster rejected the configured API key. Check TICKETMASTER_API_KEY in .env.",
        )


class RateLimited(TicketmasterError):
    def __init__(self):
        super().__init__("rate_limited", "Ticketmaster quota or rate limit hit. Try again later.")


class UnexpectedUpstream(TicketmasterError):
    def __init__(self):
        super().__init__("unexpected_upstream_error", "Ticketmaster request failed unexpectedly. Try again later.")


class EventSearchClient:
    def __init__(self, getter=None, api_key: str | None = None):
        load_dotenv()
        self._getter = getter or requests.get
        self._api_key = api_key
        self._cache: dict[tuple, dict] = {}
        self._cache_order: list[tuple] = []
        self._lock = threading.Lock()

    def _key(self) -> str:
        key = (self._api_key or os.environ.get("TICKETMASTER_API_KEY", "")).strip()
        if not key:
            raise MissingCredentials()
        return key

    @staticmethod
    def _cache_key(keyword: str, city: str | None, country_code: str) -> tuple:
        return (keyword.casefold(), (city or "").casefold(), country_code.casefold())

    def search_events(self, keyword: str, city: str | None = None, country_code: str = "US") -> dict:
        """Return the raw Discovery response dict for upcoming music events.

        Left in Discovery's own relevance order on purpose: sorting by date
        pushes an artist's real tour off the first page whenever other events
        carry the name in their titles ("10 Years Of Mt. Joy" for TEN).
        `extract_events` restores date order after filtering.

        Raises TicketmasterError subclasses on expected failures.
        """
        cache_key = self._cache_key(keyword, city, country_code)
        with self._lock:
            if cache_key in self._cache:
                return dict(self._cache[cache_key], cached=True)

        params = {
            "apikey": self._key(),
            "keyword": keyword,
            "classificationName": "Music",
            "countryCode": country_code,
            "size": PAGE_SIZE,
        }
        if city:
            params["city"] = city
        try:
            resp = self._getter(DISCOVERY_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException:
            raise UnexpectedUpstream()
        if resp.status_code == 401:
            raise AuthenticationFailed()
        if resp.status_code == 429:
            raise RateLimited()
        if resp.status_code != 200:
            raise UnexpectedUpstream()
        try:
            data = resp.json()
        except ValueError:
            raise UnexpectedUpstream()
        if not isinstance(data, dict):
            raise UnexpectedUpstream()

        with self._lock:
            if len(self._cache_order) >= CACHE_MAX_ENTRIES:
                self._cache.pop(self._cache_order.pop(0), None)
            self._cache[cache_key] = data
            self._cache_order.append(cache_key)
        return dict(data, cached=False)


def _presale_windows(sales: dict) -> list[dict]:
    windows = []
    for presale in (sales.get("presales") or [])[:MAX_PRESALE_WINDOWS]:
        if not isinstance(presale, dict) or not presale.get("startDateTime"):
            continue
        windows.append(
            {
                "name": str(presale.get("name") or "Presale"),
                "starts": str(presale.get("startDateTime")),
            }
        )
    return windows


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _name_matches(keyword: str, attraction: str) -> bool:
    """Decide whether an attraction really is the artist the caller asked for.

    Discovery's keyword search also matches event titles, so sorting by date
    surfaces "10 Years Of Mt. Joy" for the keyword TEN. Attraction names are
    the headliner, so they are the reliable signal. Single-word keywords must
    match exactly: "ten" is an ordinary English word and a substring rule
    would keep the band El Ten Eleven.
    """
    key = _normalize_name(keyword)
    act = _normalize_name(attraction)
    if not key or not act:
        return False
    if key == act:
        return True
    key_tokens = key.split()
    if len(key_tokens) == 1:
        return False
    act_tokens = act.split()
    return any(
        act_tokens[i : i + len(key_tokens)] == key_tokens
        for i in range(len(act_tokens) - len(key_tokens) + 1)
    )


def _attraction_names(raw: dict) -> list[str]:
    attractions = ((raw.get("_embedded") or {}).get("attractions") or [])
    return [str(a.get("name")) for a in attractions if isinstance(a, dict) and a.get("name")]


def extract_events(data: dict, keyword: str | None = None) -> list[dict]:
    """Shape a Discovery response into the stable event list (sorted by date).

    Pass `keyword` to keep only events whose headliner actually is that artist.

    `price_min` / `price_max` stay null for most K-pop events: the Discovery
    API publishes price ranges only for some events, so callers must treat a
    null price as unknown rather than as zero. The on-sale and presale
    timestamps below are always present and are what planning actually needs.
    """
    embedded = data.get("_embedded") or {}
    raw_events = embedded.get("events") or []
    events = []
    for raw in raw_events:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        attractions = _attraction_names(raw)
        if keyword and attractions and not any(_name_matches(keyword, a) for a in attractions):
            continue
        dates = raw.get("dates") or {}
        start = dates.get("start") or {}
        status = (dates.get("status") or {}).get("code")
        venues = ((raw.get("_embedded") or {}).get("venues") or [{}])
        venue = venues[0] if isinstance(venues[0], dict) else {}
        city = ((venue.get("city") or {}).get("name")) if isinstance(venue.get("city"), dict) else None
        country = ((venue.get("country") or {}).get("name")) if isinstance(venue.get("country"), dict) else None
        prices = raw.get("priceRanges") or [{}]
        price = prices[0] if isinstance(prices[0], dict) else {}
        sales = raw.get("sales") or {}
        public_sale = sales.get("public") or {}
        please_note = raw.get("pleaseNote")
        ticket_limit = (raw.get("ticketLimit") or {}).get("info")
        events.append(
            {
                "name": str(raw.get("name")),
                "event_id": str(raw.get("id") or ""),
                "date": start.get("localDate"),
                "time": start.get("localTime"),
                "timezone": dates.get("timezone"),
                "status": status,
                "venue": venue.get("name"),
                "city": city,
                "country": country,
                "attractions": attractions,
                "url": raw.get("url"),
                "price_min": price.get("min"),
                "price_max": price.get("max"),
                "currency": price.get("currency"),
                "public_on_sale_at": public_sale.get("startDateTime"),
                "presale_windows": _presale_windows(sales),
                "ticket_limit": str(ticket_limit).strip() if ticket_limit else None,
                "please_note": str(please_note).strip()[:PLEASE_NOTE_MAX_CHARS] if please_note else None,
            }
        )
    # Planning reads chronologically, so restore date order locally.
    events.sort(key=lambda event: (event.get("date") or "9999-12-31", event.get("time") or "23:59:59"))
    return events


# --- model-facing common tool --------------------------------------------------

_shared_client: EventSearchClient | None = None
_client_lock = threading.Lock()


def get_event_client() -> EventSearchClient:
    """One lazily created client per process, so its response cache is shared."""
    global _shared_client
    if _shared_client is None:
        with _client_lock:
            if _shared_client is None:
                _shared_client = EventSearchClient()
    return _shared_client


def search_ticketmaster_events(
    keyword: str,
    city: str | None = None,
    country_code: str = "US",
) -> str:
    """One Discovery lookup, filtered down to events whose headliner is `keyword`."""
    try:
        data = get_event_client().search_events(
            keyword,
            city=(city.strip() if isinstance(city, str) and city.strip() else None),
            country_code=(country_code.strip().upper() if isinstance(country_code, str) and country_code.strip() else "US"),
        )
    except TicketmasterError as exc:
        return json.dumps(
            {"ok": False, "error": exc.code, "message": exc.message, "source": "ticketmaster"},
            ensure_ascii=False,
        )
    events = extract_events(data, keyword=keyword)
    return json.dumps(
        {
            "ok": True,
            "keyword": keyword,
            "city": city,
            "country_code": country_code,
            "matched_count": len(events),
            "events": events,
        },
        ensure_ascii=False,
    )


SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_ticketmaster_events",
        "description": (
            "Look up upcoming events on Ticketmaster for one artist: venue, "
            "city, date, public on-sale time, named presale windows, ticket "
            "limit and the official purchase link. Results are filtered to "
            "events whose headliner actually is the keyword. Call it for "
            "notices marked ticket_relevant; pop-up stores, membership "
            "application events and Asian dates are usually not on "
            "Ticketmaster at all, so an empty result is an honest answer "
            "rather than a failure. Ticketmaster does not report live seat "
            "inventory and publishes prices for only some events."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": (
                        "Artist name as the user typed it, e.g. 'MONSTA X'. "
                        "Do not use a Weverse URL slug: 'monstax' matches "
                        "nothing here."
                    ),
                },
                "city": {
                    "type": ["string", "null"],
                    "description": "Optional city filter, e.g. 'New York'.",
                },
                "country_code": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional ISO country code, default 'US'. Ticketmaster "
                        "does not operate in South Korea or Japan, so leave the "
                        "default for other regions rather than guessing."
                    ),
                },
            },
            "required": ["keyword"],
        },
    },
}

SCHEMAS = [SCHEMA]
HANDLERS = {"search_ticketmaster_events": search_ticketmaster_events}
