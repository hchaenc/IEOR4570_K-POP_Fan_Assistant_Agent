"""Tests for the merch appraisal original tool: filtering, pricing and envelopes.

The eBay client is faked at the `search_listings` seam, so these exercise the
real appraisal logic without network or credentials.
"""

import json

import tools.common.ebay as ebay_module
from tools import TOOL_MAP, TOOLS, run_tool
from tools.originals.merch_appraisal.tool import categorize, condition_of


def listing(title, price, shipping=0.0, feedback_pct=99.5, feedback_count=500, ships_from="US", condition="New", image_url=None):
    return {
        "title": title,
        "price": price,
        "shipping": shipping,
        "ebay_condition": condition,
        "seller_feedback_pct": feedback_pct,
        "seller_feedback_count": feedback_count,
        "ships_from": ships_from,
        "url": "https://www.ebay.com/itm/sample",
        "image_url": image_url,
    }


def sample_listings(q="IVE Wonyoung photocard"):
    """A realistic mix: fair listings, one outlier, a too-cheap one, junk and risky sellers."""
    return [
        listing(f"{q} official", 14.0, 4.0, 99.8, 1520, "KR"),
        listing(f"{q} official kpop", 16.5, 0.0, 100.0, 310),
        listing(f"{q} K-POP official", 18.0, 3.5, 99.1, 88),
        listing(f"{q} authentic", 19.99, 4.99, 98.7, 2400, "KR"),
        listing(f"{q} official new", 21.0, 0.0, 99.5, 640),
        listing(f"{q} rare official", 24.0, 5.0, 100.0, 45, "JP"),
        listing(f"{q} official mint", 58.0, 0.0, 99.9, 900),
        listing(f"{q} official fast ship", 5.0, 0.0, 99.2, 150),
        listing(f"{q} unofficial fanmade lomo", 3.0, 1.0, 97.5, 1200, "CN"),
        listing(f"{q} reprint copy", 2.5, 0.0, 95.0, 30, "CN"),
        listing(f"{q} lot of 10 random", 30.0, 5.0, 99.0, 500),
        listing(f"{q} official", 6.0, 0.0, 91.0, 3),
        listing(f"{q} signed autograph", 120.0, 10.0, 96.0, 12),
    ]


class FakeClient:
    def __init__(self, listings=None, error=None):
        self._listings = listings or []
        self._error = error
        self.queries = []

    def search_listings(self, query):
        self.queries.append(query)
        if self._error:
            raise self._error
        return [dict(item) for item in self._listings]


def use_listings(monkeypatch, listings=None, error=None):
    client = FakeClient(listings, error)
    monkeypatch.setattr(ebay_module, "get_listing_client", lambda: client)
    return client


def appraise(**args):
    return json.loads(run_tool("appraise_kpop_merch", args))


# --- title reading ---------------------------------------------------------------


def test_categorize_reads_the_item_type_from_a_title():
    assert categorize("SEVENTEEN Official Light Stick ver 3") == "lightstick"
    assert categorize("aespa 2026 Season's Greetings") == "seasons_greetings"
    assert categorize("Stray Kids SKZOO plush") == "doll"
    assert categorize("NewJeans Hanni Get Up weverse POB") == "photocard"
    assert categorize("BTS poster") is None


def test_album_with_a_photocard_word_is_a_card_unless_it_is_a_sealed_album():
    assert categorize("IVE LOVE DIVE album photocard Wonyoung") == "photocard"
    assert categorize("IVE LOVE DIVE album sealed with photocard") == "album"
    assert categorize("IVE LOVE DIVE album no photocard") == "album"


def test_condition_depends_on_the_category():
    assert condition_of("album", "aespa Armageddon sealed", "New") == "sealed"
    assert condition_of("album", "aespa Armageddon unsealed", "New") == "opened"
    assert condition_of("album", "aespa Armageddon no pc", "Used") == "opened_no_photocard"
    assert condition_of("lightstick", "Carat bong not working", "Used") == "not_working"
    assert condition_of("photocard", "Wonyoung pc", "Pre-owned") == "used"


# --- appraisal ---------------------------------------------------------------------


def test_tool_is_registered_as_an_original_tool():
    assert "appraise_kpop_merch" in TOOL_MAP
    assert "appraise_kpop_merch" in [tool["function"]["name"] for tool in TOOLS]


def test_appraisal_filters_junk_and_prices_like_with_like(monkeypatch):
    client = use_listings(monkeypatch, sample_listings())
    result = appraise(query="  IVE Wonyoung photocard ")

    assert client.queries == ["IVE Wonyoung photocard"]
    assert result["ok"] is True and result["source"] == "ebay"
    assert result["category"] == "photocard" and result["condition"] == "new"
    assert result["excluded"] == {
        "unofficial": 2, "bundle_or_multi_choice": 1, "other_category": 0,
        "other_variant_or_accessory": 0, "other_artist": 0,
    }
    # 8 clean totals: 5, 16.5, 18, 21, 21.5, 24.99, 29, 58 -> 58 is an outlier.
    assert result["listings_compared"] == 7
    assert result["typical_price_usd"] == 21.0
    assert "Not sold prices" in result["note"]
    assert "verdict" not in result


def test_listing_cards_carry_the_photo_for_the_page(monkeypatch):
    listings = sample_listings()
    listings[1]["image_url"] = "https://i.ebayimg.com/images/g/x/s-l225.jpg"
    use_listings(monkeypatch, listings)
    best = appraise(query="IVE Wonyoung photocard")["best_listings"]
    assert best[0]["image_url"] == "https://i.ebayimg.com/images/g/x/s-l225.jpg"
    assert "image_url" in best[1] and best[1]["image_url"] is None


def test_best_listings_are_the_cheapest_safe_ones(monkeypatch):
    use_listings(monkeypatch, sample_listings())
    result = appraise(query="IVE Wonyoung photocard")

    assert [item["total_usd"] for item in result["best_listings"]] == [16.5, 18.0, 21.0]
    assert all(item["url"] for item in result["best_listings"])


def test_risky_listings_are_flagged_with_a_reason(monkeypatch):
    use_listings(monkeypatch, sample_listings())
    flagged = {item["total_usd"]: " ".join(item["why"]) for item in appraise(query="IVE Wonyoung photocard")["flagged_listings"]}

    assert "far below the typical price" in flagged[5.0]
    assert "new seller: only 3 ratings" in flagged[6.0]
    assert "signed item" in flagged[130.0]
    assert "only 96% positive feedback (12 ratings)" in flagged[130.0]


def test_a_perfect_but_brand_new_seller_is_called_new_not_badly_rated(monkeypatch):
    listings = sample_listings()
    listings.append(listing("IVE Wonyoung photocard official", 19.0, 0.0, 100.0, 4))
    use_listings(monkeypatch, listings)
    flagged = {item["total_usd"]: item["why"] for item in appraise(query="IVE Wonyoung photocard")["flagged_listings"]}
    assert flagged[19.0] == ["new seller: only 4 ratings"]


def test_zero_percent_from_a_seller_without_ratings_reads_as_new(monkeypatch):
    """eBay reports 0% positive for sellers with (almost) no ratings."""
    listings = sample_listings()
    listings.append(listing("IVE Wonyoung photocard official", 19.0, 0.0, 0.0, 0))
    use_listings(monkeypatch, listings)
    flagged = {item["total_usd"]: item["why"] for item in appraise(query="IVE Wonyoung photocard")["flagged_listings"]}
    assert flagged[19.0] == ["new seller: only 0 ratings"]


def test_target_price_gets_a_verdict(monkeypatch):
    use_listings(monkeypatch, sample_listings())
    verdicts = {
        price: appraise(query="IVE Wonyoung photocard", target_price=price)["verdict"]
        for price in (15, 22, 30)
    }
    assert verdicts == {15: "good_deal", 22: "fair", 30: "overpriced"}


def test_sealed_album_query_ignores_opened_albums_and_cards(monkeypatch):
    listings = [listing(f"aespa Armageddon album sealed {i}", 20.0 + i) for i in range(4)]
    listings += [listing("aespa Armageddon album opened", 8.0), listing("aespa Armageddon photocard Karina", 12.0)]
    use_listings(monkeypatch, listings)
    result = appraise(query="aespa Armageddon album sealed")

    assert result["condition"] == "sealed" and result["listings_compared"] == 4
    assert result["excluded"]["other_category"] == 1
    assert result["typical_price_usd"] == 21.5


def test_unknown_shipping_counts_as_zero_rather_than_crashing(monkeypatch):
    listings = [listing(f"IVE photocard official {i}", 10.0 + i, shipping=None) for i in range(3)]
    use_listings(monkeypatch, listings)
    assert appraise(query="IVE photocard")["typical_price_usd"] == 11.0


# --- envelopes ---------------------------------------------------------------------


def test_short_query_is_rejected_before_any_search(monkeypatch):
    client = use_listings(monkeypatch, sample_listings())
    result = appraise(query=" a ")
    assert result["ok"] is False and result["error"] == "bad_arguments"
    assert client.queries == []


def test_no_listings_tells_the_model_how_to_retry(monkeypatch):
    use_listings(monkeypatch, [])
    result = appraise(query="IVE Wonyoung photocard")
    assert result["ok"] is False and result["error"] == "no_results"
    assert "fewer words" in result["message"]


def test_too_few_comparables_refuses_to_price(monkeypatch):
    use_listings(monkeypatch, sample_listings()[:2] + sample_listings()[8:11])
    result = appraise(query="IVE Wonyoung photocard")

    assert result["ok"] is False and result["error"] == "too_few_comparables"
    assert result["excluded"] == {
        "unofficial": 2, "bundle_or_multi_choice": 1, "other_category": 0,
        "other_variant_or_accessory": 0, "other_artist": 0,
    }
    assert "typical_price_usd" not in result


def test_missing_keys_return_missing_credentials_not_sample_prices(monkeypatch):
    use_listings(monkeypatch, error=ebay_module.MissingCredentials())
    result = appraise(query="IVE Wonyoung photocard")

    assert result["ok"] is False and result["error"] == "missing_credentials"
    assert result["source"] == "ebay"
    assert "typical_price_usd" not in result


def test_upstream_failure_maps_to_a_safe_envelope(monkeypatch):
    use_listings(monkeypatch, error=ebay_module.RateLimited())
    result = appraise(query="IVE Wonyoung photocard")
    assert result == {
        "ok": False,
        "error": "rate_limited",
        "message": "eBay quota or rate limit hit. Tell the user to try again in a few minutes.",
        "source": "ebay",
    }


def test_non_numeric_target_price_is_ignored(monkeypatch):
    use_listings(monkeypatch, sample_listings())
    result = appraise(query="IVE Wonyoung photocard", target_price="cheap")
    assert result["ok"] is True and "verdict" not in result


# --- real-data corrections ---------------------------------------------------------


def test_lightstick_keyrings_and_other_artists_are_not_compared(monkeypatch):
    """Real eBay results for 'SEVENTEEN lightstick ver 3' included keyring
    replicas at ~$30 and an ATEEZ lightstick."""
    listings = [listing(f"SEVENTEEN Official Light Stick Ver.3 #{i}", 60.0 + i) for i in range(4)]
    listings += [
        listing("SEVENTEEN Official Light Stick Ver.3 10th Anniv. Keyring", 33.5),
        listing("Seventeen Ver.3 Mini Light Stick Keychain Multi-Color LED", 29.98),
        listing("ATEEZ Official Light Stick Ver.3 for KPOP Concert", 65.99),
    ]
    use_listings(monkeypatch, listings)
    result = appraise(query="SEVENTEEN lightstick ver 3")

    assert result["listings_compared"] == 4
    assert result["excluded"]["other_variant_or_accessory"] == 2
    assert result["excluded"]["other_artist"] == 1
    assert all("Keyring" not in item["title"] and "ATEEZ" not in item["title"] for item in result["best_listings"])


def test_a_query_for_the_variant_keeps_only_that_variant(monkeypatch):
    listings = [listing(f"aespa Armageddon CDP Ver CD Player sealed #{i}", 250.0 + 10 * i) for i in range(4)]
    listings += [listing(f"aespa Armageddon Poster Ver sealed #{i}", 20.0 + i) for i in range(4)]
    use_listings(monkeypatch, listings)

    cdp = appraise(query="aespa Armageddon CDP ver sealed")
    assert cdp["listings_compared"] == 4 and cdp["typical_price_usd"] == 265.0
    regular = appraise(query="aespa Armageddon album sealed")
    assert regular["listings_compared"] == 4 and regular["typical_price_usd"] == 21.5


def test_mixed_editions_get_no_typical_price_and_no_false_scam_flags(monkeypatch):
    """Real case: sealed aespa Armageddon ran from a $14 MY Power ver to a $125
    UK exclusive, so a median priced nothing and flagged the cheap edition as fake."""
    prices = [13.99, 14.0, 19.4, 19.4, 28.5, 30.98, 34.99, 44.99, 59.99, 79.12, 111.08, 124.99]
    use_listings(monkeypatch, [listing(f"aespa Armageddon album sealed #{i}", p) for i, p in enumerate(prices)])
    result = appraise(query="aespa Armageddon album sealed", target_price=30)

    assert result["ok"] is True and result["mixed_versions"] is True
    assert result["typical_price_usd"] is None
    assert "Ask the user which version" in result["mixed_versions_note"]
    assert "verdict" not in result, "a verdict against a mixed median would be invented"
    assert result["flagged_listings"] == []
