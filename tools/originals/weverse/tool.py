"""Model-facing Weverse notice tools.

`search_weverse_notices` returns an artist's in-window notices annotated with
an event type and the evidence behind that label; `read_weverse_notice`
returns the full text of one notice. Together they are progressive disclosure:
a compact digest first, the raw body only when the answer needs it.

This is an original tool: it returns annotated data from its own source and
performs no cross-source orchestration. Whether to spend a Ticketmaster request
is the model's call, informed by `ticket_relevant`.
"""

import json
import re

from tools.common.classify import classify_notice

from .notices import (
    DEFAULT_NOTICE_LIMIT,
    DEFAULT_READ_CHARS,
    MAX_READ_CHARS,
    get_notice_service,
)

EXCERPT_MAX_CHARS = 180
BODY_WINDOW_CHARS = 80


def _excerpt(text: str, signal: str | None, matched_field: str | None) -> str:
    """Evidence for the label, so the model can audit or override it.

    When the body decided the label the excerpt is centered on the matched
    signal: a head excerpt of an 8,000-character notice is greeting boilerplate
    and proves nothing about the classification.
    """
    text = (text or "").strip()
    if not text:
        return ""
    if matched_field == "body" and signal:
        hit = re.search(re.escape(signal), text, re.IGNORECASE)
        if hit:
            start = max(0, hit.start() - BODY_WINDOW_CHARS)
            end = min(len(text), hit.end() + BODY_WINDOW_CHARS)
            return ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")
    return text[:EXCERPT_MAX_CHARS] + ("…" if len(text) > EXCERPT_MAX_CHARS else "")


def annotate_notice(notice: dict) -> dict:
    """Attach the event label and its evidence, and drop the full body."""
    text = str(notice.get("text") or "")
    annotation = classify_notice(notice.get("title"), text)
    return {
        "notice_id": notice.get("notice_id"),
        "title": notice.get("title"),
        "published_at": notice.get("published_at"),
        "url": notice.get("url"),
        **annotation,
        "excerpt": _excerpt(text, annotation["matched_signal"], annotation["matched_field"]),
    }


def search_weverse_notices(
    artist: str,
    query: str | None = None,
    limit: int | None = DEFAULT_NOTICE_LIMIT,
) -> str:
    """Official notices for one artist, annotated with event type and evidence."""
    result = get_notice_service().search_notices(
        artist=(artist.strip() if isinstance(artist, str) else ""),
        query=(query.strip() if isinstance(query, str) and query.strip() else None),
        limit=limit,
    )
    if result.get("ok"):
        results = result.pop("results") or []
        result["notices"] = [annotate_notice(notice) for notice in results]
        result["notice_count"] = len(result["notices"])
    return json.dumps(result, ensure_ascii=False)


def read_weverse_notice(
    artist: str,
    notice_id: str,
    max_chars: int | None = DEFAULT_READ_CHARS,
) -> str:
    """Fetch one notice's full text by the id a previous search reported."""
    result = get_notice_service().read_notice(
        artist=(artist.strip() if isinstance(artist, str) else ""),
        notice_id=(str(notice_id).strip() if notice_id is not None else ""),
        max_chars=max_chars,
    )
    return json.dumps(result, ensure_ascii=False)


SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_weverse_notices",
        "description": (
            "Search an artist's official Weverse notices from the last 365 "
            "days. Returns each notice with its title, date, source URL, an "
            "event_type (ticketed_event / popup / fan_event / merchandise / "
            "online_event / announcement), the word and field that produced "
            "that label, a short evidence excerpt, and ticket_relevant. Start "
            "here for any question about announcements, tours, concerts, fan "
            "meetings or pop-ups. Only ticket_relevant notices usually warrant "
            "a search_ticketmaster_events call: pop-up stores and "
            "membership-application events are not sold on Ticketmaster, so "
            "use the channel named in the notice instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artist": {
                    "type": "string",
                    "description": (
                        "Artist name as the user typed it, for example "
                        "'aespa', 'MONSTA X' or 'ten'. Reuse this exact value "
                        "as the Ticketmaster keyword later."
                    ),
                },
                "query": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional keyword filter across notice titles and "
                        "text, e.g. 'presale'."
                    ),
                },
                "limit": {
                    "type": ["integer", "null"],
                    "description": (
                        f"Optional cap on returned notices, newest first. "
                        f"Defaults to {DEFAULT_NOTICE_LIMIT}, which covers the "
                        "whole 365-day window for most artists."
                    ),
                },
            },
            "required": ["artist"],
        },
    },
}

READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_weverse_notice",
        "description": (
            "Read the full text of one official Weverse notice. Use it after "
            "search_weverse_notices whenever the answer needs detail the "
            "excerpt cannot hold: exact dates and times, application windows, "
            "membership requirements, prices, or a sale channel. Copy the "
            "notice_id verbatim from the notices list, read at most three "
            "notices per answer, and never quote a notice you did not read."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artist": {
                    "type": "string",
                    "description": (
                        "The same artist value used in search_weverse_notices."
                    ),
                },
                "notice_id": {
                    "type": "string",
                    "description": (
                        "notice_id copied verbatim from notices[]. Do not "
                        "invent or modify one."
                    ),
                },
                "max_chars": {
                    "type": ["integer", "null"],
                    "description": (
                        f"Optional character budget for the returned text. "
                        f"Defaults to {DEFAULT_READ_CHARS} and cannot exceed "
                        f"{MAX_READ_CHARS}; the response reports the real "
                        "length in text_chars."
                    ),
                },
            },
            "required": ["artist", "notice_id"],
        },
    },
}

SCHEMAS = [SEARCH_SCHEMA, READ_SCHEMA]
HANDLERS = {
    "search_weverse_notices": search_weverse_notices,
    "read_weverse_notice": read_weverse_notice,
}
