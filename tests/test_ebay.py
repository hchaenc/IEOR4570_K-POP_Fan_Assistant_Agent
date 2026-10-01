"""Mocked tests for the eBay common client (no network, no real keys)."""

import pytest
import requests

import tools.common.ebay as ebay_module
from tools import TOOL_MAP
from tools.common.ebay import ListingSearchClient, shape_listings


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def item(title="IVE Wonyoung photocard official", price="14.00", currency="USD", shipping="4.00"):
    return {
        "title": title,
        "price": {"value": price, "currency": currency},
        "shippingOptions": [{"shippingCost": {"value": shipping, "currency": "USD"}}] if shipping else [],
        "condition": "New",
        "seller": {"feedbackPercentage": "99.8", "feedbackScore": 1520},
        "itemLocation": {"country": "KR"},
        "itemWebUrl": "https://www.ebay.com/itm/1",
        "image": {"imageUrl": "https://i.ebayimg.com/images/g/abc/s-l225.jpg"},
    }


def make_client(search_payload, search_status=200, token_status=200):
    calls = {"token": [], "search": []}

    def poster(url, headers=None, data=None, timeout=None):
        calls["token"].append({"url": url, "headers": dict(headers or {}), "data": dict(data or {})})
        return FakeResponse({"access_token": "test-app-token", "expires_in": 7200}, status_code=token_status)

    def getter(url, headers=None, params=None, timeout=None):
        calls["search"].append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        return FakeResponse(search_payload, status_code=search_status)

    client = ListingSearchClient(getter=getter, poster=poster, client_id="test-id", client_secret="test-secret")
    return client, calls


def error_code(client, query="IVE photocard"):
    with pytest.raises(ebay_module.EbayError) as excinfo:
        client.search_listings(query)
    return excinfo.value.code


def test_search_returns_shaped_listings():
    client, calls = make_client({"itemSummaries": [item()]})
    listings = client.search_listings("IVE Wonyoung photocard")

    assert listings == [
        {
            "title": "IVE Wonyoung photocard official",
            "price": 14.0,
            "shipping": 4.0,
            "ebay_condition": "New",
            "seller_feedback_pct": 99.8,
            "seller_feedback_count": 1520,
            "ships_from": "KR",
            "url": "https://www.ebay.com/itm/1",
            "image_url": "https://i.ebayimg.com/images/g/abc/s-l225.jpg",
        }
    ]
    sent = calls["search"][0]
    assert sent["url"] == "https://api.ebay.com/buy/browse/v1/item_summary/search"
    assert sent["params"] == {"q": "IVE Wonyoung photocard", "limit": 50}
    assert sent["headers"]["Authorization"] == "Bearer test-app-token"
    assert sent["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    assert calls["token"][0]["data"]["grant_type"] == "client_credentials"


def test_token_and_responses_are_cached():
    client, calls = make_client({"itemSummaries": [item()]})
    client.search_listings("IVE photocard")
    client.search_listings("ive PHOTOCARD")  # same cache key, case-insensitive
    client.search_listings("aespa album")

    assert len(calls["token"]) == 1, "one application token serves every search"
    assert len(calls["search"]) == 2


def test_cached_listings_cannot_be_mutated_by_a_caller():
    client, _ = make_client({"itemSummaries": [item()]})
    client.search_listings("IVE photocard")[0]["price"] = 0
    assert client.search_listings("IVE photocard")[0]["price"] == 14.0


def test_shape_drops_other_currencies_and_tolerates_missing_fields():
    data = {
        "itemSummaries": [
            item("GBP listing", currency="GBP"),
            item("No flat shipping", shipping=None),
            {"title": "No price at all"},
            "not a dict",
        ]
    }
    listings = shape_listings(data)
    assert [listing["title"] for listing in listings] == ["No flat shipping"]
    assert listings[0]["shipping"] is None, "unknown shipping must not be reported as free"
    assert shape_listings({}) == []


def test_image_falls_back_to_thumbnail_and_rejects_non_https():
    thumb_only = item()
    thumb_only.pop("image")
    thumb_only["thumbnailImages"] = [{"imageUrl": "https://i.ebayimg.com/thumb.jpg"}]
    plain_http = item()
    plain_http["image"] = {"imageUrl": "http://example.com/x.jpg"}
    no_image = item()
    no_image.pop("image")

    shaped = shape_listings({"itemSummaries": [thumb_only, plain_http, no_image]})
    assert [listing["image_url"] for listing in shaped] == ["https://i.ebayimg.com/thumb.jpg", None, None]


def test_missing_keys_raise_missing_credentials(monkeypatch):
    monkeypatch.setattr(ebay_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    client, calls = make_client({})
    client._client_id = client._client_secret = None

    assert error_code(client) == "missing_credentials"
    assert calls["token"] == [] and calls["search"] == []


def test_rejected_keys_map_to_authentication_failed():
    client, _ = make_client({}, token_status=401)
    assert error_code(client) == "authentication_failed"


def test_expired_token_is_dropped_so_the_next_call_fetches_a_new_one():
    client, calls = make_client({}, search_status=401)
    assert error_code(client) == "authentication_failed"
    assert error_code(client) == "authentication_failed"
    assert len(calls["token"]) == 2


def test_http_429_maps_to_rate_limited():
    client, _ = make_client({}, search_status=429)
    assert error_code(client) == "rate_limited"


def test_http_500_and_bad_json_map_to_unexpected_upstream():
    client, _ = make_client({}, search_status=500)
    assert error_code(client) == "unexpected_upstream_error"
    client, _ = make_client(None)
    assert error_code(client) == "unexpected_upstream_error"


def test_timeout_maps_to_timeout():
    client, _ = make_client({})

    def slow(*args, **kwargs):
        raise requests.Timeout()

    client._getter = slow
    assert error_code(client) == "timeout"


def test_sandbox_env_switches_host(monkeypatch):
    monkeypatch.setenv("EBAY_ENV", "sandbox")
    client, calls = make_client({"itemSummaries": []})
    client.search_listings("IVE photocard")
    assert calls["search"][0]["url"].startswith("https://api.sandbox.ebay.com/")


def test_client_is_not_a_model_facing_tool():
    assert not hasattr(ebay_module, "SCHEMAS") and not hasattr(ebay_module, "HANDLERS")
    assert not any("ebay" in name for name in TOOL_MAP)
