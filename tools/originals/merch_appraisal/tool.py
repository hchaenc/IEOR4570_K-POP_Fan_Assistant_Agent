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

    counts = {"unofficial": 0, "bundle_or_multi_choice": 0, "other_category": 0}
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
        category = categorize(title) or target_category
        if target_category and category != target_category:
            counts["other_category"] += 1
            continue

        warnings = []
        if SIGNED_PATTERN.search(title):
            warnings.append("signed item: autographs are the most faked K-pop merch, ask for proof")
        fp, fc = item["seller_feedback_pct"], item["seller_feedback_count"]
        if (fp is not None and fp < 97) or (fc is not None and fc < 10):
            warnings.append(f"seller has {fp}% positive feedback over {fc} ratings")
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
        "typical_price_usd": round(median, 2),
        "typical_range_usd": [round(q1, 2), round(q3, 2)],
        "excluded": counts,
        "best_listings": [
            {"title": k["title"], "total_usd": k["total"], "ships_from": k["ships_from"],
             "seller_feedback_pct": k["seller_feedback_pct"], "url": k["url"]}
            for k in best
        ],
        "flagged_listings": [
            {"title": k["title"], "total_usd": k["total"], "why": k["warnings"], "url": k["url"]}
            for k in flagged[:MAX_FLAGGED_LISTINGS]
        ],
    }
    if target_price is not None:
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
