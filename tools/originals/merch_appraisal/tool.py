"""Model-facing merch appraisal tool: price check and scam check for K-pop merch.

eBay only gives us live listings (asking prices), not sold prices, and fetching
them is `tools/common/ebay.py`'s job. This tool's own work is on top of that:
it sorts every listing into a merch category and condition, throws out bundles
and unofficial goods, adds shipping, drops outliers, and only compares like
with like.
"""

import json
import re
import statistics

from tools.common import ebay

SOURCE = "ebay"
MIN_QUERY_CHARS = 3
MIN_COMPARABLES = 3
MAX_BEST_LISTINGS = 3
MAX_FLAGGED_LISTINGS = 5
# When the upper quartile is this many times the lower one, the listings are
# different editions of the "same" item (a $14 album next to a $260 CD-player
# edition), and one typical price would be meaningless.
MIXED_VERSIONS_RATIO = 3.5

# --- What a title tells us ---

CATEGORY_WORDS = {
    "lightstick": ["lightstick", "light stick", "응원봉", "fanlight"],
    "seasons_greetings": ["season's greetings", "seasons greetings", "season greetings", "시즌그리팅"],
    "doll": ["doll", "plush", "skzoo", "bt21", "keyring", "인형"],
    "concert_merch": ["concert", "tour merch", "slogan", "official md", "tour md"],
    "album": ["album", " cd ", "앨범", "sealed", "unsealed", "jewel case", "platform ver", "digipack"],
    "photocard": ["photocard", "photo card", "포카", " pc ", "pob", "lucky draw"],
}
# When a title matches several categories, the first one here wins, except for the
# album/photocard case which is handled in categorize().
CATEGORY_ORDER = ["lightstick", "seasons_greetings", "doll", "concert_merch", "photocard", "album"]

UNOFFICIAL_WORDS = [
    "unofficial", "not official", "fanmade", "fan made", "fan-made", "lomo", "reprint",
    "custom", "replica", "handmade", "copy", "fansite", "fan site", "diy",
]
BUNDLE_PATTERN = re.compile(
    r"\blot\b|\bbundle\b|\bset of\b|\bfull set\b|\d+\s*(pcs|pieces|cards)\b|"
    r"choose|pick your|select (your )?member|you pick|u pick",
    re.I,
)
SIGNED_PATTERN = re.compile(r"\bsigned\b|autograph|싸인|사인", re.I)
NO_PC_PATTERN = re.compile(r"no (photo ?card|pc)|without (photo ?card|pc)|w/o (photo ?card|pc)", re.I)
SEALED_PATTERN = re.compile(r"(?<!un)sealed|미개봉", re.I)
BROKEN_PATTERN = re.compile(r"not working|broken|for parts|doesn'?t work|does not work", re.I)

# Listings that share the item's words but are a different product: a keyring
# replica of a lightstick, a CD-player or vinyl edition of an album, a photocard
# holder. Excluded unless the query itself asks for that variant.
VARIANT_PATTERNS = {
    "lightstick": re.compile(r"key ?ring|key ?chain|miniature|\bmini\b", re.I),
    "album": re.compile(r"\bcdp\b|cd player|\blp\b|vinyl|cassette|turntable", re.I),
    "photocard": re.compile(r"holder|sleeve|binder|top ?loader|\bframe\b", re.I),
}

PUNCTUATION = re.compile(r"[^\w\s'-]")


def _padded(text: str) -> str:
    return " " + PUNCTUATION.sub(" ", text.lower()) + " "


def categorize(title: str) -> str | None:
    t = _padded(title)
    hits = {c for c, words in CATEGORY_WORDS.items() if any(w in t for w in words)}
    if {"album", "photocard"} <= hits:
        # "IVE album photocard" is a single inclusion card; "album sealed w/ pc" is an album.
        return "album" if SEALED_PATTERN.search(title) or NO_PC_PATTERN.search(title) else "photocard"
    return next((c for c in CATEGORY_ORDER if c in hits), None)


def condition_of(category: str | None, title: str, ebay_condition: str) -> str:
    if category == "album":
        if SEALED_PATTERN.search(title):
            return "sealed"
        return "opened_no_photocard" if NO_PC_PATTERN.search(title) else "opened"
    if category == "lightstick":
        return "not_working" if BROKEN_PATTERN.search(title) else "working"
    return "new" if ebay_condition.lower().startswith("new") else "used"


def _is_other_variant(category: str | None, query: str, title: str) -> bool:
    pattern = VARIANT_PATTERNS.get(category)
    if pattern is None:
        return False
    return bool(pattern.search(query)) != bool(pattern.search(title))


def _names_artist(query: str, title: str) -> bool:
    """The query starts with the group or member (the schema asks for that), so the title must name it.

    eBay's relevance search happily returns an ATEEZ lightstick for 'SEVENTEEN
    lightstick ver 3'.
    """
    words = _padded(query).split()
    return not words or f" {words[0]} " in _padded(title)


def _card(item: dict, **extra) -> dict:
    return {
        "title": item["title"],
        "total_usd": item["total"],
        **extra,
        "url": item["url"],
        "image_url": item.get("image_url"),
    }


def _verdict(target: float, median: float) -> str:
    if target <= median * 0.85:
        return "good_deal"
    return "fair" if target <= median * 1.15 else "overpriced"


def _failure(code: str, message: str, **extra) -> str:
    return json.dumps({"ok": False, "error": code, "message": message, "source": SOURCE, **extra}, ensure_ascii=False)


# --- The tool ---


def appraise_kpop_merch(query: str, target_price: float | None = None) -> str:
    """Estimate what a K-pop merch item goes for on eBay and flag risky listings."""
    query = query.strip() if isinstance(query, str) else ""
    if len(query) < MIN_QUERY_CHARS:
        return _failure(
            "bad_arguments",
            "Query is too short. Name the group or member and the item, e.g. 'IVE Wonyoung LOVE DIVE photocard'.",
        )
    if not isinstance(target_price, (int, float)) or isinstance(target_price, bool):
        target_price = None

    try:
        raw = ebay.get_listing_client().search_listings(query)
    except ebay.EbayError as exc:
        return _failure(exc.code, exc.message)

    if not raw:
        return _failure(
            "no_results",
            f"No eBay listings for '{query}'. Retry with fewer words, e.g. drop the version name "
            "or use the English group name.",
        )

    target_category = categorize(query)
    target_condition = None
    if target_category == "lightstick":
        target_condition = condition_of(target_category, query, "")
    elif target_category == "album" and (SEALED_PATTERN.search(query) or NO_PC_PATTERN.search(query)):
        target_condition = condition_of(target_category, query, "")
    # Otherwise the user didn't say, so the most common condition is picked below.

    counts = {
        "unofficial": 0,
        "bundle_or_multi_choice": 0,
        "other_category": 0,
        "other_variant_or_accessory": 0,
        "other_artist": 0,
    }
    kept = []
    for item in raw:
        title = item["title"]
        low = _padded(title)
        if any(w in low for w in UNOFFICIAL_WORDS):
            counts["unofficial"] += 1
            continue
        if BUNDLE_PATTERN.search(title):
            counts["bundle_or_multi_choice"] += 1
            continue
        if not _names_artist(query, title):
            counts["other_artist"] += 1
            continue
        category = categorize(title) or target_category
        if target_category and category != target_category:
            counts["other_category"] += 1
            continue
        if _is_other_variant(category, query, title):
            counts["other_variant_or_accessory"] += 1
            continue

        warnings = []
        if SIGNED_PATTERN.search(title):
            warnings.append("signed item: autographs are the most faked K-pop merch, ask for proof")
        fp, fc = item["seller_feedback_pct"], item["seller_feedback_count"]
        # eBay reports 0% for sellers with almost no ratings, so the count is
        # checked first: "0% positive over 0 ratings" means new, not bad.
        if fc is not None and fc < 10:
            warnings.append(f"new seller: only {fc} rating{'' if fc == 1 else 's'}")
        elif fp is not None and fp < 97:
            warnings.append(f"seller has only {fp:g}% positive feedback ({fc} ratings)")
        kept.append({
            **item,
            "category": category,
            "condition": condition_of(category, title, item["ebay_condition"]),
            "total": round(item["price"] + (item["shipping"] or 0), 2),
            "warnings": warnings,
        })

    if target_condition is None and kept:
        target_condition = statistics.mode(k["condition"] for k in kept)
    group = [k for k in kept if k["condition"] == target_condition]
    # Signed items and risky sellers would skew the typical price, so leave them out of it.
    clean = sorted(k["total"] for k in group if not k["warnings"])

    if len(clean) < MIN_COMPARABLES:
        return _failure(
            "too_few_comparables",
            f"Only {len(clean)} comparable official listings for '{query}', too few for a price. "
            "Retry with a broader query (drop the version or member name), or with a clearer item word "
            "such as 'photocard', 'album', or 'lightstick'.",
            excluded=counts,
        )

    q1, _, q3 = statistics.quantiles(clean, n=4)
    spread = q3 - q1
    inliers = [p for p in clean if q1 - 1.5 * spread <= p <= q3 + 1.5 * spread]
    median = statistics.median(inliers)
    mixed_versions = q1 > 0 and q3 / q1 >= MIXED_VERSIONS_RATIO

    # With mixed editions a cheap listing is usually just the cheap edition.
    if not mixed_versions:
        for k in group:
            if k["total"] < median * 0.4:
                k["warnings"].append("price is far below the typical price: possible fake or scam")

    best = sorted((k for k in group if not k["warnings"]), key=lambda k: k["total"])[:MAX_BEST_LISTINGS]
    flagged = [k for k in group if k["warnings"]]

    result = {
        "ok": True,
        "query": query,
        "source": SOURCE,
        "note": "Prices are current asking prices on eBay US, shipping included, in USD. Not sold prices.",
        "category": target_category or "unknown",
        "condition": target_condition,
        "listings_compared": len(inliers),
        "typical_price_usd": None if mixed_versions else round(median, 2),
        "typical_range_usd": [round(q1, 2), round(q3, 2)],
        "excluded": counts,
        "best_listings": [
            _card(k, ships_from=k["ships_from"], seller_feedback_pct=k["seller_feedback_pct"]) for k in best
        ],
        "flagged_listings": [_card(k, why=k["warnings"]) for k in flagged[:MAX_FLAGGED_LISTINGS]],
    }
    if mixed_versions:
        result["mixed_versions"] = True
        result["mixed_versions_note"] = (
            f"Prices run from about ${q1:.0f} to ${q3:.0f}: these listings are different editions of the item, "
            "so there is no single typical price. Ask the user which version or edition they mean, then "
            "call again with it in the query (e.g. 'Poster ver', 'CDP ver', 'Barnes & Noble exclusive')."
        )
    elif target_price is not None:
        result["target_price_usd"] = target_price
        result["verdict"] = _verdict(target_price, median)
    return json.dumps(result, ensure_ascii=False)


SCHEMA = {
    "type": "function",
    "function": {
        "name": "appraise_kpop_merch",
        "description": (
            "Check what a K-pop merch item (photocard, album, lightstick, season's greetings, doll, "
            "concert merch) currently sells for on eBay, and flag risky listings. Filters out "
            "unofficial/fanmade goods, bundles and multi-choice listings, compares only the same "
            "category and condition (e.g. sealed vs opened album), includes shipping, and returns "
            "the typical price, the price range, the 3 best-value safe listings, and flagged ones. "
            "The chat page shows those listings' photos itself, so do not paste image links; you cannot "
            "see the photos, so never judge authenticity from them. If mixed_versions is true there is "
            "no typical price: ask which edition the user means instead of quoting one. "
            "Use it when the user asks how much merch costs, whether a price is fair, or where to buy. "
            "Do not use it for concert tickets, and do not quote a price when it returns an error: "
            "prices are asking prices on eBay US in USD, not sold prices."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The item in English as a seller would title it: group, member, "
                                   "release, and item type, e.g. 'NewJeans Hanni Get Up weverse pob photocard' "
                                   "or 'SEVENTEEN lightstick ver 3' or 'aespa Armageddon album sealed'.",
                },
                "target_price": {
                    "type": ["number", "null"],
                    "description": "Optional. A price in USD the user is considering paying; "
                                   "the result then includes a verdict: good_deal, fair or overpriced.",
                },
            },
            "required": ["query"],
        },
    },
}

HANDLER = appraise_kpop_merch
