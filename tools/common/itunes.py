"""Common tool: resolve a song to its parent music release with the iTunes Search API.

Shared by original tools that need release metadata. The iTunes Search API does
not require an API key. This tool searches public song metadata, prefers exact
artist and track matches, and returns the parent collection so the model can
connect a song the user knows to the comeback era that contains it.

For K-pop releases, collection names often contain packaging labels such as
"The 1st Album", "EP" or "Single". `release_title` removes those suffixes while
`collection_name` preserves Apple's original metadata.
"""

from __future__ import annotations

import json
import re
import threading

import requests

SEARCH_URL = "https://itunes.apple.com/search"
SEARCH_LIMIT = 25
CACHE_MAX_ENTRIES = 100
REQUEST_TIMEOUT_SECONDS = 20


class ITunesError(Exception):
    """Adapter-internal error carrying a stable tool-layer error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RateLimited(ITunesError):
    def __init__(self):
        super().__init__(
            "rate_limited",
            "iTunes Search API rate limit hit. Try again later.",
        )


class UnexpectedUpstream(ITunesError):
    def __init__(self):
        super().__init__(
            "unexpected_upstream_error",
            "iTunes Search API request failed unexpectedly. Try again later.",
        )


class SongSearchClient:
    def __init__(self, getter=None):
        self._getter = getter or requests.get
        self._cache: dict[tuple, dict] = {}
        self._cache_order: list[tuple] = []
        self._lock = threading.Lock()

    @staticmethod
    def _cache_key(artist: str, song: str, country: str) -> tuple:
        return (
            artist.casefold().strip(),
            song.casefold().strip(),
            country.casefold().strip(),
        )

    def search_songs(
        self,
        artist: str,
        song: str,
        country: str = "US",
    ) -> dict:
        cache_key = self._cache_key(artist, song, country)

        with self._lock:
            if cache_key in self._cache:
                return dict(self._cache[cache_key], cached=True)

        params = {
            "term": f"{artist} {song}",
            "country": country,
            "media": "music",
            "entity": "song",
            "limit": SEARCH_LIMIT,
        }

        try:
            resp = self._getter(
                SEARCH_URL,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            raise UnexpectedUpstream()

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


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _name_matches(expected: str, actual: str) -> bool:
    return _normalize_name(expected) == _normalize_name(actual)

DERIVATIVE_RELEASE_WORDS = (
    "remix",
    "remixes",
    "sped up",
    "slowed",
    "instrumental",
    "karaoke",
)


def _is_derivative_release(collection_name: str) -> bool:
    normalized = _normalize_name(collection_name)
    return any(
        _normalize_name(word) in normalized
        for word in DERIVATIVE_RELEASE_WORDS
    )

def _release_title(collection_name: str) -> str:
    """Remove common release-format suffixes while preserving the real title."""
    title = collection_name.strip()

    patterns = [
        r"\s*-\s*the\s+\d+(?:st|nd|rd|th)\s+(?:full\s+)?album\s*$",
        r"\s*-\s*the\s+\d+(?:st|nd|rd|th)\s+mini\s+album\s*$",
        r"\s*-\s*\d+(?:st|nd|rd|th)\s+(?:full\s+)?album\s*$",
        r"\s*-\s*\d+(?:st|nd|rd|th)\s+mini\s+album\s*$",
        r"\s*-\s*ep\s*$",
        r"\s*-\s*single\s*$",
    ]

    for pattern in patterns:
        title = re.sub(pattern, "", title, flags=re.I)

    return title.strip()


def resolve_song_results(data: dict, artist: str, song: str) -> list[dict]:
    """Keep exact artist/song matches and shape them into stable release metadata."""
    results = []

    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue

        track_name = str(item.get("trackName") or "")
        artist_name = str(item.get("artistName") or "")
        collection_name = str(item.get("collectionName") or "")

        if not track_name or not artist_name or not collection_name:
            continue

        if not _name_matches(song, track_name):
            continue

        if not _name_matches(artist, artist_name):
            continue

        results.append(
            {
                "artist": artist_name,
                "song": track_name,
                "release_title": _release_title(collection_name),
                "collection_name": collection_name,
                "is_derivative_release": _is_derivative_release(collection_name),
                "track_count": item.get("trackCount"),
                "collection_id": item.get("collectionId"),
                "track_id": item.get("trackId"),
                "release_date": item.get("releaseDate"),
                "genre": item.get("primaryGenreName"),
                "track_url": item.get("trackViewUrl"),
                "collection_url": item.get("collectionViewUrl"),
            }
        )

    results.sort(
        key=lambda item: (
            str(item.get("release_date") or ""),
            str(item.get("collection_id") or ""),
        )
    )

    return results


# --- model-facing common tool --------------------------------------------------

_shared_client: SongSearchClient | None = None
_client_lock = threading.Lock()


def get_song_client() -> SongSearchClient:
    """One lazily created client per process, so its response cache is shared."""
    global _shared_client

    if _shared_client is None:
        with _client_lock:
            if _shared_client is None:
                _shared_client = SongSearchClient()

    return _shared_client


def resolve_song_release(
    artist: str,
    song: str,
    country: str = "US",
) -> str:
    """Resolve one song to the release or collection that contains it."""
    artist = artist.strip() if isinstance(artist, str) else ""
    song = song.strip() if isinstance(song, str) else ""
    country = (
        country.strip().upper()
        if isinstance(country, str) and country.strip()
        else "US"
    )

    if not artist or not song:
        return json.dumps(
            {
                "ok": False,
                "error": "bad_arguments",
                "message": "Both artist and song are required.",
                "source": "itunes",
            },
            ensure_ascii=False,
        )

    try:
        data = get_song_client().search_songs(
            artist,
            song,
            country,
        )
    except ITunesError as exc:
        return json.dumps(
            {
                "ok": False,
                "error": exc.code,
                "message": exc.message,
                "source": "itunes",
            },
            ensure_ascii=False,
        )

    matches = resolve_song_results(data, artist, song)

    if not matches:
        return json.dumps(
            {
                "ok": False,
                "error": "no_results",
                "message": (
                    f"No exact iTunes song match was found for '{artist} - {song}'. "
                    "Retry with the official English artist and song names."
                ),
                "source": "itunes",
            },
            ensure_ascii=False,
        )

    match = min(
    matches,
    key=lambda item: (
        item["is_derivative_release"],
        -(item.get("track_count") or 0),
        str(item.get("release_date") or "9999"),
    ),
)

    return json.dumps(
        {
            "ok": True,
            "source": "itunes",
            **match,
            "match_count": len(matches),
            "note": (
                "release_title is derived from Apple's collection name by removing "
                "format labels such as EP, Single or numbered album suffixes. "
                "Use release_title as the release_title argument for "
                "trace_kpop_comeback_era, and reuse song as anchor_song."
            ),
        },
        ensure_ascii=False,
    )


SCHEMA = {
    "type": "function",
    "function": {
        "name": "resolve_song_release",
        "description": (
            "Resolve a song to the album, EP or single release that contains it "
            "using public iTunes music metadata. Use this before "
            "trace_kpop_comeback_era when the user gives a song but does not name "
            "its parent release. Pass the returned release_title into "
            "trace_kpop_comeback_era and pass the returned song as anchor_song. "
            "Do not guess the release from conversation history when this tool can "
            "resolve it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artist": {
                    "type": "string",
                    "description": "Artist or group name, e.g. 'aespa'.",
                },
                "song": {
                    "type": "string",
                    "description": "Song title, e.g. 'Licorice'.",
                },
                "country": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional two-letter iTunes storefront country code. "
                        "Defaults to 'US'."
                    ),
                },
            },
            "required": ["artist", "song"],
        },
    },
}

SCHEMAS = [SCHEMA]
HANDLERS = {"resolve_song_release": resolve_song_release}