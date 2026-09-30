"""Model-facing Weverse notice tool.

One original tool, two modes, selected by whether `notice_id` is present:

  weverse_notices(artist)                    -> the annotated digest
  weverse_notices(artist, notice_id="36316") -> that notice's full text

Both responses carry a `mode` field so the shape is self-describing rather
than something the model has to infer. The digest is the cheap default; the
full text is opt-in, because all 86 aespa bodies would cost ~42,000 tokens on
every later turn of the session.
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


def weverse_notices(
    artist: str,
    query: str | None = None,
    limit: int | None = DEFAULT_NOTICE_LIMIT,
    notice_id: str | None = None,
    max_chars: int | None = DEFAULT_READ_CHARS,
) -> str:
    """An artist's official notices, or one notice from that list in full."""
    service = get_notice_service()
    normalized_artist = artist.strip() if isinstance(artist, str) else ""

    if isinstance(notice_id, str) and notice_id.strip():
        result = service.read_notice(normalized_artist, notice_id.strip(), max_chars)
        if result.get("ok"):
            result["mode"] = "full_text"
        return json.dumps(result, ensure_ascii=False)

    result = service.search_notices(
        normalized_artist,
        query=(query.strip() if isinstance(query, str) and query.strip() else None),
        limit=limit,
    )
    if result.get("ok"):
        results = result.pop("results") or []
        result["mode"] = "digest"
        result["notices"] = [annotate_notice(notice) for notice in results]
        result["notice_count"] = len(result["notices"])
    return json.dumps(result, ensure_ascii=False)


SCHEMA = {
    "type": "function",
    "function": {
        "name": "weverse_notices",
        "description": (
            "Read an artist's official Weverse notices. Two modes. Without "
            "notice_id: returns the last 365 days as a digest, each notice "
            "carrying title, date, source URL, an event_type (ticketed_event / "
            "popup / fan_event / merchandise / online_event / announcement), "
            "the word and field that produced that label, a short evidence "
            "excerpt, and ticket_relevant. With notice_id: returns that one "
            "notice's full text. Start with the digest; re-call with a "
            "notice_id whenever the answer turns on detail the excerpt cannot "
            "hold - exact dates and times, application windows, membership "
            "requirements, prices, or a sale channel. event_type is a "
            "heuristic pre-label: check it against the excerpt and trust the "
            "excerpt when they disagree. Only ticket_relevant notices usually "
            "warrant a search_ticketmaster_events call, since pop-up stores and "
            "membership-application events are not sold on Ticketmaster."
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
                        "Digest mode only: optional keyword filter across "
                        "notice titles and text, e.g. 'presale'."
                    ),
                },
                "limit": {
                    "type": ["integer", "null"],
                    "description": (
                        f"Digest mode only: optional cap on returned notices, "
                        f"newest first. Defaults to {DEFAULT_NOTICE_LIMIT}, "
                        "which covers the whole 365-day window for most "
                        "artists."
                    ),
                },
                "notice_id": {
                    "type": ["string", "null"],
                    "description": (
                        "Full-text mode: a notice_id copied verbatim from the "
                        "digest's notices[]. Leave it out to get the digest. "
                        "Never invent or modify one."
                    ),
                },
                "max_chars": {
                    "type": ["integer", "null"],
                    "description": (
                        f"Full-text mode only: character budget for the "
                        f"returned text. Defaults to {DEFAULT_READ_CHARS} and "
                        f"cannot exceed {MAX_READ_CHARS}; the response reports "
                        "the real length in text_chars."
                    ),
                },
            },
            "required": ["artist"],
        },
    },
}

HANDLER = weverse_notices
