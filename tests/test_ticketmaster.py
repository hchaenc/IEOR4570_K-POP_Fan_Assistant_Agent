"""Mocked tests for the tools.ticketmaster package (no network, no real key)."""

import json

import pytest

from tools import TOOL_MAP, TOOLS, run_tool
from tools.integrations.ticketmaster.service import EventSearchClient, extract_events


DISCOVERY = "app.ticketmaster.com"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def discovery_payload(events):
    return {"_embedded": {"events": events}} if events else {"page": {"totalElements": 0}}


def sample_event(name="ATEEZ World Tour", eid="Z7r9jZ1Ad"):
    return {
        "name": name,
        "id": eid,
        "url": f"https://www.ticketmaster.com/event/{eid}",
        "dates": {
            "start": {"localDate": "2026-11-02", "localTime": "19:30:00"},
            "timezone": "America/New_York",
            "status": {"code": "onsale"},
        },
        "priceRanges": [{"min": 65.0, "max": 250.0, "currency": "USD"}],
        "_embedded": {
            "venues": [
                {
                    "name": "Barclays Center",
                    "city": {"name": "Brooklyn"},
                    "country": {"name": "United States"},
                }
            ]
        },
    }


def make_client(payload, status_code=200):
    calls = []

    def getter(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": dict(params or {})})
        return FakeResponse(payload, status_code=status_code)

    getter.calls = calls
    return EventSearchClient(getter=getter, api_key="test-tm-key"), getter


# --- service behavior ----------------------------------------------------------


def test_search_returns_shaped_events():
    client, getter = make_client(discovery_payload([sample_event()]))
    data = client.search_events("ATEEZ")
    events = extract_events(data)
    assert len(events) == 1
    event = events[0]
    assert event["name"] == "ATEEZ World Tour"
    assert event["date"] == "2026-11-02" and event["timezone"] == "America/New_York"
    assert event["venue"] == "Barclays Center" and event["city"] == "Brooklyn"
    assert event["price_min"] == 65.0 and event["price_max"] == 250.0
    assert event["url"].startswith("https://www.ticketmaster.com/")
    sent = getter.calls[0]["params"]
    assert sent["classificationName"] == "Music" and sent["countryCode"] == "US" and sent["size"] == 10
    # Date sorting would bury the artist's own tour under title matches.
    assert "sort" not in sent


def test_cache_prevents_second_network_call():
    client, getter = make_client(discovery_payload([sample_event()]))
    first = client.search_events("ATEEZ")
    second = client.search_events("ateez")  # same cache key, case-insensitive
    assert first.get("cached") is False and second.get("cached") is True
    assert len(getter.calls) == 1


def test_missing_key_raises_missing_credentials(monkeypatch):
    monkeypatch.setattr("tools.integrations.ticketmaster.service.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("TICKETMASTER_API_KEY", raising=False)
    client, _ = make_client({})
    client._api_key = None
    with pytest.raises(type(client).__mro__ and Exception) as excinfo:  # TicketmasterError family
        client.search_events("ATEEZ")
    assert getattr(excinfo.value, "code", "") == "missing_credentials"


def test_http_401_maps_to_authentication_failed():
    client, _ = make_client({"fault": "invalid"}, status_code=401)
    try:
        client.search_events("ATEEZ")
        raise AssertionError("expected error")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", "") == "authentication_failed"


def test_http_429_maps_to_rate_limited():
    client, _ = make_client({}, status_code=429)
    try:
        client.search_events("ATEEZ")
        raise AssertionError("expected error")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", "") == "rate_limited"


def test_empty_result_is_ok_with_zero_events():
    events = extract_events(discovery_payload([]))
    assert events == []


def test_extract_skips_malformed_entries_and_tolerates_missing_prices():
    raw = [
        sample_event("Good", "id1"),
        {"id": "no-name"},
        sample_event("NoPrice", "id2"),
    ]
    raw[2].pop("priceRanges")
    events = extract_events(discovery_payload(raw))
    assert [e["name"] for e in events] == ["Good", "NoPrice"]
    assert events[1]["price_min"] is None and events[1]["currency"] is None


def test_extract_surfaces_onsale_presale_and_ticket_limit():
    """The fields K-pop planning actually relies on: Discovery omits prices.

    Mirrors the real shape returned for the aespa SYNK tour, where
    priceRanges is absent but sales and ticketLimit are populated.
    """
    raw = sample_event("aespa LIVE TOUR", "idTM")
    raw.pop("priceRanges")
    raw["sales"] = {
        "public": {"startDateTime": "2026-05-06T20:00:00Z"},
        "presales": [
            {"name": "MY Membership Presale", "startDateTime": "2026-05-06T16:00:00Z"},
            {"startDateTime": "2026-05-06T17:00:00Z"},
            {"name": "No timestamp"},
        ],
    }
    raw["ticketLimit"] = {"info": "There is a ticket limit of 6 tickets per person."}
    raw["pleaseNote"] = "x" * 400

    event = extract_events(discovery_payload([raw]))[0]
    assert event["price_min"] is None
    assert event["public_on_sale_at"] == "2026-05-06T20:00:00Z"
    assert event["presale_windows"] == [
        {"name": "MY Membership Presale", "starts": "2026-05-06T16:00:00Z"},
        {"name": "Presale", "starts": "2026-05-06T17:00:00Z"},
    ]
    assert event["ticket_limit"].startswith("There is a ticket limit")
    assert len(event["please_note"]) == 240


def test_extract_tolerates_missing_sales_block():
    event = extract_events(discovery_payload([sample_event()]))[0]
    assert event["public_on_sale_at"] is None
    assert event["presale_windows"] == []
    assert event["ticket_limit"] is None
    assert event["please_note"] is None


def with_attractions(event, *names):
    event.setdefault("_embedded", {})["attractions"] = [{"name": n} for n in names]
    return event


def test_extract_drops_events_whose_headliner_is_not_the_artist():
    """Keyword search also matches titles, so TEN surfaced anniversary tours.

    Real case: keyword 'ten' returned 'Mt. Joy 2026: Celebrating 10 Years Of
    Mt. Joy' and 'Chance The Rapper - Coloring Book 10 Year Anniversary'.
    """
    noise = with_attractions(sample_event("Mt. Joy 2026: 10 Years Of Mt. Joy", "idNoise"), "Mt. Joy")
    real = with_attractions(sample_event("TEN - US Showcase Tour", "idReal"), "TEN")

    events = extract_events(discovery_payload([noise, real]), keyword="ten")
    assert [e["name"] for e in events] == ["TEN - US Showcase Tour"]
    assert events[0]["attractions"] == ["TEN"]


def test_extract_matches_multiword_artists_but_not_festival_lineups():
    tour = with_attractions(sample_event("2026 MONSTA X WORLD TOUR", "idM"), "MONSTA X")
    assert [e["name"] for e in extract_events(discovery_payload([tour]), keyword="MONSTA X")]

    festival = with_attractions(sample_event("Kamp Festival", "idF"), "Kamp Festival")
    assert extract_events(discovery_payload([festival]), keyword="MONSTA X") == []


def test_extract_keeps_events_that_publish_no_attraction_list():
    """Absence of data is not evidence against a match."""
    assert len(extract_events(discovery_payload([sample_event()]), keyword="ATEEZ")) == 1


def test_extract_sorts_events_chronologically():
    later = sample_event("Later", "idL")
    earlier = sample_event("Earlier", "idE")
    earlier["dates"]["start"]["localDate"] = "2026-10-01"
    assert [e["name"] for e in extract_events(discovery_payload([later, earlier]))] == [
        "Earlier",
        "Later",
    ]
