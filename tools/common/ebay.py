"""Common service: eBay Browse API listing search (not a model-facing tool).

Shared by any original tool that needs marketplace listings. This module
exposes neither SCHEMAS nor HANDLERS on purpose: the model never searches eBay
directly, it calls an original tool (merch appraisal today) that does its own
work on top of these listings.

Browse API with an application token (client-credentials grant), so no user
login is involved. Browse returns live listings, which means asking prices:
sold prices are a different, restricted API. The token lasts about two hours
and is cached in the client; responses are cached too, so repeated demo
questions do not burn the 5000 requests/day quota.

Configuration (local .env):
    EBAY_CLIENT_ID, EBAY_CLIENT_SECRET   developer.ebay.com, "Production" keyset
    EBAY_ENV=sandbox                     optional, to use the sandbox keyset
"""

from __future__ import annotations

import base64
import os
import threading
import time

import requests
from dotenv import load_dotenv

HOSTS = {"production": "https://api.ebay.com", "sandbox": "https://api.sandbox.ebay.com"}
TOKEN_PATH = "/identity/v1/oauth2/token"
SEARCH_PATH = "/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
MARKETPLACE_ID = "EBAY_US"
CURRENCY = "USD"
SEARCH_LIMIT = 50
CACHE_MAX_ENTRIES = 100
REQUEST_TIMEOUT_SECONDS = 10
TOKEN_REFRESH_MARGIN_SECONDS = 60


class EbayError(Exception):
    """Adapter-internal error carrying a stable tool-layer error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MissingCredentials(EbayError):
    def __init__(self):
        super().__init__(
            "missing_credentials",
            "eBay is not configured. Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in the local .env. "
            "Tell the user the price check is unavailable rather than estimating a price.",
        )


class AuthenticationFailed(EbayError):
    def __init__(self):
        super().__init__(
            "authentication_failed",
            "eBay rejected the configured keys. Check EBAY_CLIENT_ID, EBAY_CLIENT_SECRET and EBAY_ENV in .env. "
            "Tell the user the price check is unavailable right now.",
        )


class RateLimited(EbayError):
    def __init__(self):
        super().__init__("rate_limited", "eBay quota or rate limit hit. Tell the user to try again in a few minutes.")


class Timeout(EbayError):
    def __init__(self):
        super().__init__("timeout", "eBay did not answer in time. Try once more, or answer without live prices.")


class UnexpectedUpstream(EbayError):
    def __init__(self):
        super().__init__(
            "unexpected_upstream_error",
            "eBay request failed unexpectedly. Try again later, or answer without live prices.",
        )


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def shape_listings(data: dict) -> list[dict]:
    """Shape a Browse search response into the stable listing list.

    Listings priced in another currency are dropped rather than converted: a
    price comparison across exchange rates would look exact and not be.
    `shipping` is None when the seller publishes no flat cost (calculated
    shipping), which callers must treat as unknown rather than free.
    """
    listings = []
    for item in data.get("itemSummaries") or []:
        if not isinstance(item, dict):
            continue
        price = item.get("price") or {}
        amount = _number(price.get("value"))
        if price.get("currency") != CURRENCY or amount is None:
            continue
        shipping_options = item.get("shippingOptions") or [{}]
        shipping = (shipping_options[0] or {}).get("shippingCost") or {}
        seller = item.get("seller") or {}
        listings.append(
            {
                "title": str(item.get("title") or ""),
                "price": amount,
                "shipping": _number(shipping.get("value")),
                "ebay_condition": str(item.get("condition") or ""),
                "seller_feedback_pct": _number(seller.get("feedbackPercentage")),
                "seller_feedback_count": seller.get("feedbackScore"),
                "ships_from": (item.get("itemLocation") or {}).get("country"),
                "url": item.get("itemWebUrl"),
            }
        )
    return listings


class ListingSearchClient:
    def __init__(self, getter=None, poster=None, client_id: str | None = None, client_secret: str | None = None):
        load_dotenv()
        self._getter = getter or requests.get
        self._poster = poster or requests.post
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._cache: dict[str, list[dict]] = {}
        self._cache_order: list[str] = []
        self._lock = threading.Lock()

    def _host(self) -> str:
        return HOSTS.get(os.environ.get("EBAY_ENV", "production").strip().lower(), HOSTS["production"])

    def _credentials(self) -> tuple[str, str]:
        client_id = (self._client_id or os.environ.get("EBAY_CLIENT_ID", "")).strip()
        client_secret = (self._client_secret or os.environ.get("EBAY_CLIENT_SECRET", "")).strip()
        if not client_id or not client_secret:
            raise MissingCredentials()
        return client_id, client_secret

    def _request(self, call, url: str, **kwargs) -> dict:
        try:
            resp = call(url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
        except requests.Timeout:
            raise Timeout()
        except requests.RequestException:
            raise UnexpectedUpstream()
        if resp.status_code in (401, 403):
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
        return data

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
            return self._token
        client_id, client_secret = self._credentials()
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        body = self._request(
            self._poster,
            self._host() + TOKEN_PATH,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": SCOPE},
        )
        token = body.get("access_token")
        if not token:
            raise UnexpectedUpstream()
        self._token = str(token)
        self._token_expires_at = time.time() + (_number(body.get("expires_in")) or 7200)
        return self._token

    def search_listings(self, query: str) -> list[dict]:
        """Return up to SEARCH_LIMIT live eBay US listings for `query`, in eBay's relevance order.

        Raises EbayError subclasses on expected failures.
        """
        cache_key = query.casefold()
        with self._lock:
            if cache_key in self._cache:
                return [dict(listing) for listing in self._cache[cache_key]]

        try:
            data = self._request(
                self._getter,
                self._host() + SEARCH_PATH,
                headers={"Authorization": f"Bearer {self._access_token()}", "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID},
                params={"q": query, "limit": SEARCH_LIMIT},
            )
        except AuthenticationFailed:
            # Expired or revoked; the next call fetches a new token.
            self._token = None
            raise
        listings = shape_listings(data)

        with self._lock:
            if cache_key not in self._cache:
                if len(self._cache_order) >= CACHE_MAX_ENTRIES:
                    self._cache.pop(self._cache_order.pop(0), None)
                self._cache_order.append(cache_key)
            self._cache[cache_key] = listings
        return [dict(listing) for listing in listings]


_shared_client: ListingSearchClient | None = None
_client_lock = threading.Lock()


def get_listing_client() -> ListingSearchClient:
    """One lazily created client per process, so its token and response cache are shared."""
    global _shared_client
    if _shared_client is None:
        with _client_lock:
            if _shared_client is None:
                _shared_client = ListingSearchClient()
    return _shared_client
