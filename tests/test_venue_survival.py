"""Tests for the venue survival original tool: ranking, signals and envelopes.

OpenStreetMap is faked at the `osm` module seam, so these exercise the real
tool logic without network.
"""

import json

import pytest
import requests

import tools.originals.venue_survival.osm as osm_module
import tools.originals.venue_survival.tool as tool_module
from tools import TOOL_MAP, TOOLS, run_tool

VENUE = {"name": "KSPO Dome, Songpa-gu, Seoul, South Korea", "lat": 37.5194, "lon": 127.1273}
M_PER_DEGREE_LAT = 111_195  # one degree of latitude on the tool's 6371 km sphere


def node(tags, north_m):
    """An OSM node `north_m` meters due north of the venue."""
    return {"type": "node", "lat": VENUE["lat"] + north_m / M_PER_DEGREE_LAT, "lon": VENUE["lon"], "tags": tags}


def well_mapped():
    return [
        node({"shop": "convenience", "name": "GS25", "opening_hours": "24/7"}, 80),
        node({"shop": "convenience", "name": "CU", "opening_hours": "Mo-Su 07:00-23:00"}, 160),
        node({"station": "subway", "name": "올림픽공원역", "name:en": "Olympic Park"}, 240),
        node({"railway": "station", "name:en": "Olympic Park"}, 250),  # same station mapped twice
        node({"station": "subway", "name:en": "Mongchontoseong"}, 400),
        node({"amenity": "toilets", "fee": "yes"}, 120),
        node({"amenity": "cafe", "brand": "Starbucks"}, 200),
        node({"amenity": "fast_food", "name": "Lotteria"}, 320),
        node({"amenity": "pharmacy", "name": "Olympic Pharmacy"}, 450),
        node({"amenity": "bank", "name": "not a category we list"}, 50),
        {"type": "way", "center": {"lat": VENUE["lat"], "lon": VENUE["lon"]}, "tags": {"amenity": "cafe", "name": "Dome Cafe"}},
        {"type": "way", "tags": {"amenity": "cafe", "name": "no position"}},
    ]


@pytest.fixture(autouse=True)
def fresh_cache():
    tool_module._cache.clear()


def use_osm(monkeypatch, elements=None, venue=VENUE, find_error=None, query_error=None):
    calls = {"find": [], "query": []}

    def find_venue(name):
        calls["find"].append(name)
        if find_error:
            raise find_error
        return dict(venue) if venue else None

    def query_around(lat, lon, radius_m, tag_filters):
        calls["query"].append({"lat": lat, "lon": lon, "radius_m": radius_m, "tag_filters": tag_filters})
        if query_error:
            raise query_error
        return elements or []

    monkeypatch.setattr(osm_module, "find_venue", find_venue)
    monkeypatch.setattr(osm_module, "query_around", query_around)
    return calls


def kit(**args):
    return json.loads(run_tool("venue_survival_kit", args))


# --- the kit -----------------------------------------------------------------------


def test_tool_is_registered_as_an_original_tool():
    assert "venue_survival_kit" in TOOL_MAP
    assert "venue_survival_kit" in [tool["function"]["name"] for tool in TOOLS]


def test_kit_groups_places_by_category_nearest_first(monkeypatch):
    calls = use_osm(monkeypatch, well_mapped())
    result = kit(venue=" KSPO Dome Seoul ")

    assert result["ok"] is True and result["source"] == "openstreetmap"
    assert result["venue"] == VENUE["name"] and result["radius_m"] == 500
    assert calls["find"] == ["KSPO Dome Seoul"]
    assert calls["query"][0]["radius_m"] == 500 and len(calls["query"][0]["tag_filters"]) == 7

    nearby = result["nearby"]
    assert set(nearby) == {"convenience_store", "station", "toilets", "cafe", "fast_food", "pharmacy"}
    assert [(p["name"], p["meters"], p["walk_min"]) for p in nearby["convenience_store"]] == [("GS25", 80, 1), ("CU", 160, 2)]
    assert nearby["convenience_store"][0]["open_24h"] is True and nearby["convenience_store"][1]["open_24h"] is False
    assert [p["name"] for p in nearby["cafe"]] == ["Dome Cafe", "Starbucks"], "ways use their center; brand is a fallback name"
    toilet = nearby["toilets"][0]
    assert {k: toilet[k] for k in ("category", "name", "meters", "walk_min", "paid")} == {
        "category": "toilets", "name": "unnamed toilets", "meters": 120, "walk_min": 2, "paid": True,
    }
    assert toilet["lon"] == VENUE["lon"] and abs(toilet["lat"] - VENUE["lat"]) < 0.002, "the page map plots each place"
    assert result["map_url"].startswith("https://www.openstreetmap.org/?mlat=37.5194")


def test_a_station_mapped_twice_is_listed_once_by_its_english_name(monkeypatch):
    use_osm(monkeypatch, well_mapped())
    stations = kit(venue="KSPO Dome Seoul")["nearby"]["station"]
    assert [s["name"] for s in stations] == ["Olympic Park", "Mongchontoseong"]


def test_at_most_three_places_per_category(monkeypatch):
    use_osm(monkeypatch, [node({"amenity": "cafe", "name": f"Cafe {i}"}, 50 * i) for i in range(1, 6)])
    cafes = kit(venue="KSPO Dome Seoul")["nearby"]["cafe"]
    assert [c["name"] for c in cafes] == ["Cafe 1", "Cafe 2", "Cafe 3"]


def test_well_mapped_venue_has_no_gaps_and_measures_the_second_station(monkeypatch):
    use_osm(monkeypatch, well_mapped())
    assert kit(venue="KSPO Dome Seoul")["signals"] == {
        "nearest_toilet_m": 120,
        "nearest_toilet_paid": True,
        "nearest_food_or_cafe": "Dome Cafe",
        "convenience_store_count": 2,
        "store_24h_count": 1,
        "station_count": 2,
        "second_station_extra_walk_min": 2,
        "gaps": [],
    }


def test_gaps_in_the_map_become_signal_codes(monkeypatch):
    use_osm(monkeypatch, [
        node({"amenity": "fast_food", "name": "Lotteria"}, 100),
        node({"shop": "convenience", "name": "CU"}, 160),
        node({"amenity": "toilets"}, 350),
        node({"station": "subway", "name": "Olympic Park"}, 240),
    ])
    signals = kit(venue="KSPO Dome Seoul")["signals"]

    assert signals["gaps"] == ["no_public_toilet_within_300m", "no_store_marked_24h", "single_station", "no_pharmacy"]
    assert signals["nearest_toilet_m"] == 350 and signals["nearest_toilet_paid"] is False
    assert signals["nearest_food_or_cafe"] == "Lotteria"
    assert signals["second_station_extra_walk_min"] is None


def test_result_carries_facts_not_prewritten_advice(monkeypatch):
    """Finished tip sentences got parroted to every user; advice is the model's job."""
    use_osm(monkeypatch, well_mapped())
    result = kit(venue="KSPO Dome Seoul")
    assert "tips" not in result
    assert "own words" in result["note"]


def test_signals_count_every_mapped_place_not_just_the_listed_three(monkeypatch):
    stores = [node({"shop": "convenience", "name": f"Store {i}"}, 50 * i) for i in range(1, 5)]
    stores.append(node({"shop": "convenience", "name": "Far 24h", "opening_hours": "24/7"}, 450))
    use_osm(monkeypatch, stores)
    result = kit(venue="KSPO Dome Seoul")
    assert len(result["nearby"]["convenience_store"]) == 3
    assert result["signals"]["convenience_store_count"] == 5 and result["signals"]["store_24h_count"] == 1


def test_nothing_mapped_is_still_a_successful_answer(monkeypatch):
    use_osm(monkeypatch, [])
    result = kit(venue="KSPO Dome Seoul")
    assert result["ok"] is True
    assert all(places == [] for places in result["nearby"].values())
    assert result["signals"]["gaps"] == [
        "no_public_toilet_within_300m", "no_convenience_store", "no_station", "no_cafe_or_fast_food", "no_pharmacy",
    ]
    assert "not that nothing exists" in result["note"]


def test_radius_is_clamped_and_the_adjustment_is_reported(monkeypatch):
    calls = use_osm(monkeypatch, [])
    result = kit(venue="KSPO Dome Seoul", radius_m=5000)
    assert result["radius_m"] == 1500 and calls["query"][0]["radius_m"] == 1500
    assert "5000 was adjusted to 1500" in result["radius_note"]

    assert kit(venue="KSPO Dome Seoul", radius_m=None)["radius_m"] == 500
    assert kit(venue="UBS Arena New York", radius_m="far")["radius_m"] == 500


def test_repeat_question_is_served_from_cache(monkeypatch):
    calls = use_osm(monkeypatch, well_mapped())
    first = kit(venue="KSPO Dome Seoul")
    second = kit(venue="kspo dome seoul")
    assert first == second and len(calls["find"]) == 1 and len(calls["query"]) == 1


# --- envelopes ---------------------------------------------------------------------


def test_short_venue_is_rejected_before_any_lookup(monkeypatch):
    calls = use_osm(monkeypatch, well_mapped())
    result = kit(venue=" K ")
    assert result["ok"] is False and result["error"] == "bad_arguments"
    assert calls["find"] == []


def test_unknown_venue_tells_the_model_how_to_retry(monkeypatch):
    calls = use_osm(monkeypatch, venue=None)
    result = kit(venue="Nowhere Dome")
    assert result["ok"] is False and result["error"] == "venue_not_found"
    assert result["source"] == "openstreetmap" and "city added" in result["message"]
    assert calls["query"] == []


def test_busy_overpass_still_returns_the_venue_location(monkeypatch):
    """Real case: Allegiant Stadium timed out after 36 s and the user got nothing,
    although Nominatim had already found the stadium."""
    use_osm(monkeypatch, query_error=osm_module.OsmError("timeout", "slow"))
    result = kit(venue="Allegiant Stadium Las Vegas")

    assert result["ok"] is True and result["nearby_unavailable"] is True
    assert result["nearby_error"] == "timeout"
    assert result["coordinates"] == [VENUE["lat"], VENUE["lon"]] and result["map_url"]
    assert "nearby" not in result and "signals" not in result, "empty lists would read as 'nothing nearby'"
    assert "Do not say there is nothing nearby" in result["note"]


def test_failures_are_not_cached(monkeypatch):
    use_osm(monkeypatch, query_error=osm_module.OsmError("rate_limited", "busy"))
    assert kit(venue="KSPO Dome Seoul")["nearby_unavailable"] is True

    use_osm(monkeypatch, well_mapped())
    assert "nearby_unavailable" not in kit(venue="KSPO Dome Seoul")


def test_a_failed_venue_search_is_still_an_error(monkeypatch):
    use_osm(monkeypatch, find_error=osm_module.OsmError("timeout", "slow"))
    assert kit(venue="KSPO Dome Seoul") == {"ok": False, "error": "timeout", "message": "slow", "source": "openstreetmap"}


# --- osm client --------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def test_find_venue_shapes_the_best_nominatim_hit(monkeypatch):
    sent = {}

    def get(url, params=None, headers=None, timeout=None):
        sent.update(url=url, params=params, headers=headers)
        return FakeResponse([{"display_name": "KSPO Dome, Seoul", "lat": "37.5194", "lon": "127.1273"}])

    monkeypatch.setattr(osm_module.requests, "get", get)
    assert osm_module.find_venue("KSPO Dome Seoul") == {"name": "KSPO Dome, Seoul", "lat": 37.5194, "lon": 127.1273}
    assert sent["params"]["q"] == "KSPO Dome Seoul" and sent["params"]["limit"] == 1
    assert "kpop-fan-assistant-agent" in sent["headers"]["User-Agent"], "Nominatim requires a descriptive User-Agent"

    monkeypatch.setattr(osm_module.requests, "get", lambda *a, **k: FakeResponse([]))
    assert osm_module.find_venue("Nowhere Dome") is None


def test_find_venue_maps_a_timeout_to_a_stable_code(monkeypatch):
    def get(*args, **kwargs):
        raise requests.Timeout()

    monkeypatch.setattr(osm_module.requests, "get", get)
    with pytest.raises(osm_module.OsmError) as excinfo:
        osm_module.find_venue("KSPO Dome Seoul")
    assert excinfo.value.code == "timeout"


def test_overpass_falls_back_to_the_mirror_when_the_first_server_is_busy(monkeypatch):
    urls = []

    def post(url, data=None, headers=None, timeout=None):
        urls.append(url)
        if len(urls) == 1:
            return FakeResponse({}, status_code=504)
        return FakeResponse({"elements": [{"type": "node"}]})

    monkeypatch.setattr(osm_module.requests, "post", post)
    assert osm_module.query_around(37.5, 127.1, 500, ['["amenity"="cafe"]']) == [{"type": "node"}]
    assert urls == osm_module.OVERPASS_URLS


def test_overpass_busy_everywhere_maps_to_rate_limited(monkeypatch):
    monkeypatch.setattr(osm_module.requests, "post", lambda *a, **k: FakeResponse({}, status_code=429))
    with pytest.raises(osm_module.OsmError) as excinfo:
        osm_module.query_around(37.5, 127.1, 500, ['["amenity"="cafe"]'])
    assert excinfo.value.code == "rate_limited"
