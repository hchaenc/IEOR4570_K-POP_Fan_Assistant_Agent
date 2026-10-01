"""OpenStreetMap access for the venue survival tool: Nominatim and Overpass.

Both services are free and need no key:
    Nominatim (OpenStreetMap search) turns a venue name into coordinates.
    Overpass (OpenStreetMap query API) lists what is mapped around those coordinates.
Both ask for a descriptive User-Agent and light use.
"""

from __future__ import annotations

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Tried in order. The public servers are often overloaded for a minute or two,
# so a slow one is abandoned quickly in favour of the next. Measured October
# 2026 for one stadium query: overpass-api.de 2 s, maps.mail.ru 11 s;
# overpass.kumi.systems and overpass.private.coffee did not answer in 30 s.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
HEADERS = {"User-Agent": "kpop-fan-assistant-agent/1.0 (IEOR4570 class project)"}
NOMINATIM_TIMEOUT_SECONDS = 10
OVERPASS_QUERY_TIMEOUT_SECONDS = 12
OVERPASS_TIMEOUT_SECONDS = 15


class OsmError(Exception):
    """Adapter-internal error carrying a stable tool-layer error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _error(exc: requests.RequestException, service: str) -> OsmError:
    if isinstance(exc, requests.Timeout):
        return OsmError("timeout", f"The {service} did not answer in time. Tell the user to try again in a minute.")
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return OsmError("rate_limited", f"The {service} is busy. Tell the user to try again in a minute.")
    return OsmError(
        "unexpected_upstream_error",
        f"The {service} could not be reached. Tell the user to try again in a minute.",
    )


def find_venue(venue: str) -> dict | None:
    """Best Nominatim match as {name, lat, lon}, or None when nothing matches."""
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": venue, "format": "json", "limit": 1, "accept-language": "en"},
            headers=HEADERS,
            timeout=NOMINATIM_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        hits = resp.json()
    except requests.RequestException as exc:
        raise _error(exc, "map search service")
    except ValueError:
        raise OsmError("unexpected_upstream_error", "The map search service returned an unreadable answer. Try again in a minute.")
    if not hits:
        return None
    try:
        return {"name": hits[0]["display_name"], "lat": float(hits[0]["lat"]), "lon": float(hits[0]["lon"])}
    except (KeyError, TypeError, ValueError, IndexError):
        raise OsmError("upstream_schema_changed", "The map search service returned an unexpected shape. Try again later.")


def query_around(lat: float, lon: float, radius_m: int, tag_filters: list[str]) -> list[dict]:
    """Every node, way and relation within `radius_m` that matches one of the Overpass tag filters."""
    around = f"(around:{radius_m},{lat},{lon})"
    parts = "".join(f"nwr{around}{f};" for f in tag_filters)
    query = f"[out:json][timeout:{OVERPASS_QUERY_TIMEOUT_SECONDS}];({parts});out center tags;"

    last_error: OsmError | None = None
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(url, data={"data": query}, headers=HEADERS, timeout=OVERPASS_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json().get("elements", [])
        except requests.RequestException as exc:
            last_error = _error(exc, "map data service")
        except (ValueError, AttributeError):
            last_error = OsmError(
                "unexpected_upstream_error",
                "The map data service returned an unreadable answer. Tell the user to try again in a minute.",
            )
    raise last_error
