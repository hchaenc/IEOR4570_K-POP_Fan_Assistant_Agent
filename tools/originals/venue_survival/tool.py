"""Model-facing venue survival tool: what's around a concert venue, for fans queueing outside.

The map data comes from OpenStreetMap through `osm.py`. This tool's own work is
on top of that: it ranks places by walking time, marks 24-hour shops, measures
how much further a second station is, and reduces the map to `signals`, the
facts that matter for a queue (nearest toilet, any 24-hour store, gaps).

It deliberately returns facts, not advice. Whether a missing 24-hour store
matters depends on whether the fan is queueing overnight, which only the
conversation knows, so turning signals into advice is the model's job.

OpenStreetMap asks for light use, so results are cached per venue for the life
of the process.
"""

import json
import math

from . import osm

SOURCE = "openstreetmap"
MIN_VENUE_CHARS = 3
DEFAULT_RADIUS_M = 500
MIN_RADIUS_M = 200
MAX_RADIUS_M = 1500
WALK_M_PER_MIN = 80
PER_CATEGORY = 3
TOILET_NEAR_M = 300

# category -> OpenStreetMap tag filters that count as that category
CATEGORIES = {
    "convenience_store": ['["shop"="convenience"]'],
    "station": ['["station"="subway"]', '["railway"="station"]'],
    "toilets": ['["amenity"="toilets"]'],
    "cafe": ['["amenity"="cafe"]'],
    "fast_food": ['["amenity"="fast_food"]'],
    "pharmacy": ['["amenity"="pharmacy"]'],
}

_cache: dict[tuple, str] = {}


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in meters."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _category_of(tags: dict) -> str | None:
    if tags.get("shop") == "convenience":
        return "convenience_store"
    if tags.get("station") == "subway" or tags.get("railway") == "station":
        return "station"
    amenity = tags.get("amenity")
    return amenity if amenity in ("toilets", "cafe", "fast_food", "pharmacy") else None


def _place(element: dict, lat: float, lon: float) -> dict | None:
    tags = element.get("tags", {})
    category = _category_of(tags)
    # Ways and relations (a shop mapped as a building) carry their position in "center".
    plat = element.get("lat", element.get("center", {}).get("lat"))
    plon = element.get("lon", element.get("center", {}).get("lon"))
    if category is None or plat is None or plon is None:
        return None
    meters = round(_distance_m(lat, lon, plat, plon))
    name = tags.get("name:en") or tags.get("name") or tags.get("brand") or f"unnamed {category.replace('_', ' ')}"
    place = {
        "category": category,
        "name": name,
        "meters": meters,
        "walk_min": max(1, round(meters / WALK_M_PER_MIN)),
        # For the page's map; the model does not need them.
        "lat": round(float(plat), 6),
        "lon": round(float(plon), 6),
    }
    hours = tags.get("opening_hours")
    if hours:
        place["opening_hours"] = hours
        place["open_24h"] = hours.strip() == "24/7"
    if category == "toilets" and tags.get("fee") == "yes":
        place["paid"] = True
    return place


def _signals(nearby: dict[str, list[dict]]) -> dict:
    """The queue-relevant facts, as values rather than sentences, computed over every mapped place."""
    toilets = nearby["toilets"]
    stores = nearby["convenience_store"]
    stations = nearby["station"]
    food = sorted(nearby["cafe"] + nearby["fast_food"], key=lambda p: p["meters"])

    gaps = []
    if not toilets or toilets[0]["meters"] > TOILET_NEAR_M:
        gaps.append("no_public_toilet_within_300m")
    if not stores:
        gaps.append("no_convenience_store")
    elif not any(s.get("open_24h") for s in stores):
        gaps.append("no_store_marked_24h")
    if not stations:
        gaps.append("no_station")
    elif len(stations) == 1:
        gaps.append("single_station")
    if not food:
        gaps.append("no_cafe_or_fast_food")
    if not nearby["pharmacy"]:
        gaps.append("no_pharmacy")

    return {
        "nearest_toilet_m": toilets[0]["meters"] if toilets else None,
        "nearest_toilet_paid": bool(toilets[0].get("paid")) if toilets else None,
        "nearest_food_or_cafe": food[0]["name"] if food else None,
        "convenience_store_count": len(stores),
        "store_24h_count": sum(1 for s in stores if s.get("open_24h")),
        "station_count": len(stations),
        "second_station_extra_walk_min": (
            stations[1]["walk_min"] - stations[0]["walk_min"] if len(stations) >= 2 else None
        ),
        "gaps": gaps,
    }


def _failure(code: str, message: str) -> str:
    return json.dumps({"ok": False, "error": code, "message": message, "source": SOURCE}, ensure_ascii=False)


# --- The tool ---


def venue_survival_kit(venue: str, radius_m: int | None = DEFAULT_RADIUS_M) -> str:
    """Find convenience stores, stations, toilets and food around a concert venue, plus queue signals."""
    venue = venue.strip() if isinstance(venue, str) else ""
    if len(venue) < MIN_VENUE_CHARS:
        return _failure("bad_arguments", "Venue name is too short. Give the venue and city, e.g. 'KSPO Dome Seoul'.")
    try:
        radius_m = int(radius_m)
    except (TypeError, ValueError):
        radius_m = DEFAULT_RADIUS_M
    clamped = min(max(radius_m, MIN_RADIUS_M), MAX_RADIUS_M)

    key = (venue.lower(), clamped)
    if key in _cache:
        return _cache[key]

    try:
        spot = osm.find_venue(venue)
        if spot is None:
            return _failure(
                "venue_not_found",
                f"Could not find a place called '{venue}' on OpenStreetMap. Retry with the city added "
                "or the English name, e.g. 'KSPO Dome Seoul' or 'Prudential Center Newark'.",
            )
        filters = [f for tag_filters in CATEGORIES.values() for f in tag_filters]
        elements = osm.query_around(spot["lat"], spot["lon"], clamped, filters)
    except osm.OsmError as exc:
        return _failure(exc.code, exc.message)

    nearby: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    seen = set()
    for el in elements:
        place = _place(el, spot["lat"], spot["lon"]) if isinstance(el, dict) else None
        if place is None:
            continue
        # A station is often mapped several times (station node, platform, building).
        dedupe = (place["category"], place["name"])
        if dedupe in seen:
            continue
        seen.add(dedupe)
        nearby[place["category"]].append(place)
    for places in nearby.values():
        places.sort(key=lambda p: p["meters"])

    result = {
        "ok": True,
        "venue": spot["name"],
        "source": SOURCE,
        "coordinates": [spot["lat"], spot["lon"]],
        "radius_m": clamped,
        "note": "From OpenStreetMap. A category with nothing listed means nothing is mapped there, "
                "not that nothing exists; say so rather than claiming there is none. These are facts, "
                "not advice: pick what matters for this user's plan and explain it in your own words.",
        "nearby": {c: places[:PER_CATEGORY] for c, places in nearby.items()},
        "signals": _signals(nearby),
        "map_url": f"https://www.openstreetmap.org/?mlat={spot['lat']}&mlon={spot['lon']}#map=17/{spot['lat']}/{spot['lon']}",
    }
    if clamped != radius_m:
        result["radius_note"] = f"radius_m {radius_m} was adjusted to {clamped} (allowed {MIN_RADIUS_M} to {MAX_RADIUS_M})."
    out = json.dumps(result, ensure_ascii=False)
    _cache[key] = out
    return out


SCHEMA = {
    "type": "function",
    "function": {
        "name": "venue_survival_kit",
        "description": (
            "Map what is around a concert venue from OpenStreetMap: the nearest convenience stores "
            "(marking 24-hour ones), subway/train stations, public toilets, cafes, fast food and pharmacies, "
            "each with walking minutes, plus signals such as the nearest toilet distance, how many "
            "stores are open 24 hours, how much further the second station is, and gaps in the map. "
            "It returns facts, not advice: you turn them into advice that fits the user's plan "
            "(overnight queue, same-day arrival, leaving after the show). Use it when the user is going "
            "to a concert, queueing for merch or a fan event, or asks what is near a venue. Do not use "
            "it to find events or tickets, and when Ticketmaster already returned the venue, pass that "
            "venue name and city here."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "venue": {
                    "type": "string",
                    "description": "Venue name with its city, in English if possible, "
                                   "e.g. 'KSPO Dome Seoul', 'Gocheok Sky Dome Seoul', 'UBS Arena New York'.",
                },
                "radius_m": {
                    "type": ["integer", "null"],
                    "description": f"Optional search radius in meters around the venue, {MIN_RADIUS_M} to "
                                   f"{MAX_RADIUS_M}. Default {DEFAULT_RADIUS_M}.",
                },
            },
            "required": ["venue"],
        },
    },
}

HANDLER = venue_survival_kit
