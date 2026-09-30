"""Weverse notice search: stable, JSON-safe contract for the tool layer."""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup
from pydantic import BaseModel

from .gateway import (
    GatewayError,
    NotFound,
    WeverseGatewayClient,
)

LOOKBACK_DAYS = 365
NOTICE_SCAN_LIMIT = 300
REQUEST_TIMEOUT_SECONDS = 120
# The model classifies events itself now, so the search returns the whole
# in-window feed rather than a short page. Bounded so a very noisy community
# cannot flood the model context with titles.
DEFAULT_NOTICE_LIMIT = 120
# The read tool's text budget: the model asks for detail, but whatever it
# reads is re-sent on every later turn of the session, so keep it bounded.
DEFAULT_READ_CHARS = 1500
MAX_READ_CHARS = 5000


class NoticeRecord(BaseModel):
    notice_id: str
    artist: str
    title: str
    published_at: datetime
    text: str
    url: str


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": code, "message": message}


def slugify_artist(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")


def slug_candidates(artist: str) -> list[str]:
    """Weverse urlPath guesses for a user-typed artist name, best first.

    Weverse matches the slug exactly and has no consistent spacing rule:
    'YOASOBI' is 'yoasobi' and 'NCT 127' is 'nct-127', but 'MONSTA X' is
    'monstax'. Trying the hyphenated form alone would fail the second case.
    """
    hyphenated = slugify_artist(artist)
    despaced = hyphenated.replace("-", "")
    return [candidate for candidate in (hyphenated, despaced) if candidate]


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")

    for element in soup.select("script, style, img, video, picture, source"):
        element.decompose()

    text = soup.get_text("\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def epoch_ms_to_utc(value) -> datetime:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError("timestamp is not epoch milliseconds")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _matches_query(record: NoticeRecord, query: str | None) -> bool:
    if not query or not query.strip():
        return True
    needle = query.casefold()
    return needle in record.title.casefold() or needle in record.text.casefold()


class WeverseNoticeService:
    def __init__(self, client: WeverseGatewayClient | None = None):
        self._client = client or WeverseGatewayClient()
        self._lock = threading.Lock()

    # -- upstream steps ----------------------------------------------------------

    def _resolve_community(self, artist: str, deadline: float | None = None) -> dict:
        for slug in slug_candidates(artist):
            data = self._client.get_json(
                "/community/v1.0/communityIdUrlPathByUrlPathArtistCode",
                {"keyword": slug},
                deadline=deadline,
            )
            community_id = data.get("communityId")
            url_path = data.get("urlPath")
            if isinstance(community_id, int) and isinstance(url_path, str) and url_path:
                return {"community_id": community_id, "url_path": url_path}
        raise GatewayError(
            "community_not_joined",
            "No Weverse community matches that artist name. Use the community's URL name, "
            "e.g. 'yoasobi' or 'ten'.",
        )

    def _fetch_notices(self, community_id: int, deadline: float | None = None) -> dict:
        """One request for the most recent notices (up to NOTICE_SCAN_LIMIT).

        The upstream tabContent endpoint ignores every pagination parameter
        (pageNo, from, after, startId all return identical content), but it
        honors large `limit` values, so a single request covers the scan
        window for most communities.

        Some communities expose no NOTICE tab at all (MONSTA X's returns HTTP
        404), which is permanent rather than transient, so it maps to a
        distinct message instead of "try again later".
        """
        try:
            data = self._client.get_json(
                f"/community/v1.0/community-{community_id}/NOTICE/tabContent",
                {
                    "fields": f"notices.fieldSet(noticesV1).limit({NOTICE_SCAN_LIMIT})",
                    "pagingType": "PAGE_NO",
                },
                deadline=deadline,
            )
        except NotFound:
            raise GatewayError(
                "community_not_joined",
                "This artist's Weverse community publishes no notice feed, so official "
                "announcements cannot be searched here. Say so rather than retrying.",
            )
        content = data.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("notices"), dict):
            raise GatewayError("upstream_schema_changed", "Weverse response format changed: missing notices block")
        block = content["notices"]
        items = block.get("data")
        if not isinstance(items, list):
            raise GatewayError(
                "upstream_schema_changed", "Weverse response format changed: missing notices data"
            )
        return {"items": items}

    # -- public entry point --------------------------------------------------------

    def search_notices(
        self,
        artist: str,
        query: str | None = None,
        limit: int | None = DEFAULT_NOTICE_LIMIT,
    ) -> dict:
        """Return in-window notices, newest first, capped at `limit`.

        `limit=None` removes the cap entirely; the scan is still bounded by
        NOTICE_SCAN_LIMIT notices fetched from upstream.
        """
        if not isinstance(artist, str) or not artist.strip():
            return _error("ambiguous_artist", "A single artist or community URL name is required.")

        try:
            with self._lock:
                return self._search_locked(artist.strip(), query, limit)
        except GatewayError as exc:
            return _error(exc.code, exc.message)

    def _search_locked(self, artist: str, query: str | None, limit: int | None) -> dict:
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        community = self._resolve_community(artist, deadline)
        # The name as the user typed it, not the uppercased urlPath: the model
        # reuses it as the Ticketmaster keyword, and Ticketmaster matches
        # "MONSTA X" but finds nothing for its Weverse slug "monstax".
        display_name = artist
        url_base = f"https://weverse.io/{community['url_path']}/notice/"

        page = self._fetch_notices(community["community_id"], deadline)

        records: list[NoticeRecord] = []
        seen: set[str] = set()
        oldest_parsed: datetime | None = None
        for item in page["items"]:
            if not isinstance(item, dict):
                raise GatewayError("upstream_schema_changed", "Weverse response format changed: bad notice item")
            notice_id = item.get("noticeId")
            if notice_id is None:
                raise GatewayError("upstream_schema_changed", "Weverse response format changed: notice without id")
            notice_id = str(notice_id)
            if notice_id in seen:
                continue
            seen.add(notice_id)
            try:
                published_at = epoch_ms_to_utc(item.get("publishAt"))
            except ValueError:
                raise GatewayError(
                    "upstream_schema_changed", "Weverse response format changed: notice without publishAt"
                )
            if oldest_parsed is None or published_at < oldest_parsed:
                oldest_parsed = published_at
            if published_at < cutoff:
                continue
            records.append(
                NoticeRecord(
                    notice_id=notice_id,
                    artist=display_name,
                    title=str(item.get("title") or ""),
                    published_at=published_at,
                    text=html_to_text(str(item.get("body") or "")),
                    url=str(item.get("shareUrl") or (url_base + notice_id)),
                )
            )

        # Honest truncation: we hit our own scan cap AND the cap boundary is
        # still inside the lookback window, so in-window notices may exist
        # that the upstream never returned.
        truncated = (
            len(seen) >= NOTICE_SCAN_LIMIT
            and oldest_parsed is not None
            and oldest_parsed >= cutoff
        )

        matched = [record for record in records if _matches_query(record, query)]
        matched.sort(key=lambda record: record.published_at, reverse=True)
        shown = matched if limit is None else matched[:limit]
        return {
            "ok": True,
            "artist": display_name,
            "community_id": community["community_id"],
            "cutoff": cutoff.isoformat(),
            "scanned_count": len(seen),
            "matched_count": len(matched),
            "truncated": truncated,
            "results": [record.model_dump(mode="json") for record in shown],
        }

    # -- single-notice retrieval (progressive disclosure) ------------------------

    def read_notice(
        self,
        artist: str,
        notice_id: str,
        max_chars: int | None = DEFAULT_READ_CHARS,
    ) -> dict:
        """Return the text of one notice the model saw in a previous notices[] list.

        The list endpoint already carries complete bodies, so this reuses that
        request rather than hunting for a per-notice endpoint.
        """
        if not isinstance(artist, str) or not artist.strip():
            return _error("ambiguous_artist", "A single artist or community URL name is required.")
        if not isinstance(notice_id, str) or not notice_id.strip():
            return _error(
                "notice_not_found",
                "A notice_id string is required, taken verbatim from a notices[] result.",
            )
        try:
            budget = int(DEFAULT_READ_CHARS if max_chars is None else max_chars)
        except (TypeError, ValueError):
            budget = DEFAULT_READ_CHARS
        budget = max(1, min(budget, MAX_READ_CHARS))

        try:
            with self._lock:
                return self._read_locked(artist.strip(), notice_id.strip(), budget)
        except GatewayError as exc:
            return _error(exc.code, exc.message)

    def _read_locked(self, artist: str, notice_id: str, budget: int) -> dict:
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        community = self._resolve_community(artist, deadline)
        page = self._fetch_notices(community["community_id"], deadline)
        url_base = f"https://weverse.io/{community['url_path']}/notice/"

        for item in page["items"]:
            if not isinstance(item, dict) or str(item.get("noticeId")) != notice_id:
                continue
            text = html_to_text(str(item.get("body") or ""))
            try:
                published_at: str | None = epoch_ms_to_utc(item.get("publishAt")).isoformat()
            except ValueError:
                published_at = None
            return {
                "ok": True,
                "artist": artist,
                "notice_id": notice_id,
                "title": str(item.get("title") or ""),
                "published_at": published_at,
                "url": str(item.get("shareUrl") or (url_base + notice_id)),
                "text": text[:budget],
                "text_chars": len(text),
                "truncated": len(text) > budget,
            }

        return _error(
            "notice_not_found",
            f"No notice with id {notice_id} in this artist's recent Weverse feed. "
            "Check the notice_id against the notices[] list; do not retry.",
        )


_shared_service: WeverseNoticeService | None = None
_shared_lock = threading.Lock()


def get_notice_service() -> WeverseNoticeService:
    """One lazily created service per process.

    Both Weverse tools share it so the rotated access token stays in memory; a
    fresh client per call would restart from the stale .env token and pay a
    401-plus-refresh cycle every time.
    """
    global _shared_service
    if _shared_service is None:
        with _shared_lock:
            if _shared_service is None:
                _shared_service = WeverseNoticeService()
    return _shared_service
