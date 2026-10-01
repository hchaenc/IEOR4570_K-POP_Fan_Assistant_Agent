"""Common service: YouTube Data API video search (not a model-facing tool).

Shared by the K-pop comeback trail and any future original tool that needs
public YouTube video metadata. This module exposes neither SCHEMAS nor HANDLERS
on purpose: the model never searches YouTube directly. An original tool does
its own filtering, classification and reasoning on top of these results.

The client uses YouTube Data API v3 with an API key, so no user login or OAuth
flow is involved. Search responses are cached in process so repeated demo
questions do not unnecessarily consume quota.

Configuration (local .env):
    YOUTUBE_API_KEY    Google Cloud API key restricted to YouTube Data API v3
"""

from __future__ import annotations

import os
import threading

import requests
from dotenv import load_dotenv


SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

SEARCH_LIMIT = 25
CACHE_MAX_ENTRIES = 100
REQUEST_TIMEOUT_SECONDS = 10


class YouTubeError(Exception):
    """Adapter-internal error carrying a stable tool-layer error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MissingCredentials(YouTubeError):
    def __init__(self):
        super().__init__(
            "missing_credentials",
            "YouTube is not configured. Set YOUTUBE_API_KEY in the local .env.",
        )


class AuthenticationFailed(YouTubeError):
    def __init__(self):
        super().__init__(
            "authentication_failed",
            "YouTube rejected the configured API key. Check YOUTUBE_API_KEY "
            "and its YouTube Data API v3 restriction in .env.",
        )


class RateLimited(YouTubeError):
    def __init__(self):
        super().__init__(
            "rate_limited",
            "YouTube quota or rate limit hit. Try again later.",
        )


class Timeout(YouTubeError):
    def __init__(self):
        super().__init__(
            "timeout",
            "YouTube did not answer in time. Try again.",
        )


class UnexpectedUpstream(YouTubeError):
    def __init__(self):
        super().__init__(
            "unexpected_upstream_error",
            "YouTube request failed unexpectedly. Try again later.",
        )


class VideoSearchClient:
    def __init__(self, getter=None, api_key: str | None = None):
        load_dotenv()
        self._getter = getter or requests.get
        self._api_key = api_key
        self._cache: dict[str, list[dict]] = {}
        self._cache_order: list[str] = []
        self._lock = threading.Lock()

    def _key(self) -> str:
        key = (self._api_key or os.environ.get("YOUTUBE_API_KEY", "")).strip()
        if not key:
            raise MissingCredentials()
        return key

    def _request(self, url: str, params: dict) -> dict:
        """Send one YouTube Data API request and map expected failures."""

        request_params = dict(params)
        request_params["key"] = self._key()

        try:
            resp = self._getter(
                url,
                params=request_params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.Timeout:
            raise Timeout()
        except requests.RequestException:
            raise UnexpectedUpstream()

        try:
            data = resp.json()
        except ValueError:
            raise UnexpectedUpstream()

        if not isinstance(data, dict):
            raise UnexpectedUpstream()

        if resp.status_code == 429:
            raise RateLimited()

        if resp.status_code in (400, 401, 403):
            error = data.get("error") or {}
            errors = error.get("errors") or []

            reasons = {
                str(item.get("reason") or "")
                for item in errors
                if isinstance(item, dict)
            }
            message = str(error.get("message") or "").casefold()

            quota_reasons = {
                "quotaExceeded",
                "dailyLimitExceeded",
                "rateLimitExceeded",
                "userRateLimitExceeded",
            }

            if reasons & quota_reasons or "quota" in message:
                raise RateLimited()

            raise AuthenticationFailed()

        if resp.status_code != 200:
            raise UnexpectedUpstream()

        return data

    def search_videos(self, query: str, max_results: int = SEARCH_LIMIT) -> list[dict]:
        cache_key = query.casefold().strip()

        with self._lock:
            if cache_key in self._cache:
                return [dict(video) for video in self._cache[cache_key]]

        search_data = self._request(
            SEARCH_URL,
            {
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": max(1, min(max_results, SEARCH_LIMIT)),
                "order": "relevance",
            },
        )

        video_ids = [
            str(item.get("id", {}).get("videoId"))
            for item in search_data.get("items", [])
            if isinstance(item, dict)
            and isinstance(item.get("id"), dict)
            and item.get("id", {}).get("videoId")
        ]

        if not video_ids:
            return []

        video_data = self._request(
            VIDEOS_URL,
            {
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(video_ids),
            },
        )

        videos = []
        for item in video_data.get("items", []):
            if not isinstance(item, dict):
                continue

            snippet = item.get("snippet") or {}
            statistics = item.get("statistics") or {}
            content_details = item.get("contentDetails") or {}
            video_id = str(item.get("id") or "")

            if not video_id:
                continue

            videos.append(
                {
                    "video_id": video_id,
                    "title": str(snippet.get("title") or ""),
                    "channel_title": str(snippet.get("channelTitle") or ""),
                    "channel_id": str(snippet.get("channelId") or ""),
                    "published_at": snippet.get("publishedAt"),
                    "description": str(snippet.get("description") or ""),
                    "duration": content_details.get("duration"),
                    "view_count": int(statistics.get("viewCount", 0))
                    if str(statistics.get("viewCount", "")).isdigit()
                    else None,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                }
            )

        with self._lock:
            if cache_key not in self._cache:
                if len(self._cache_order) >= CACHE_MAX_ENTRIES:
                    self._cache.pop(self._cache_order.pop(0), None)
                self._cache_order.append(cache_key)

            self._cache[cache_key] = videos

        return [dict(video) for video in videos]


_shared_client: VideoSearchClient | None = None
_client_lock = threading.Lock()


def get_video_client() -> VideoSearchClient:
    """One lazily created client per process, so its response cache is shared."""
    global _shared_client

    if _shared_client is None:
        with _client_lock:
            if _shared_client is None:
                _shared_client = VideoSearchClient()

    return _shared_client