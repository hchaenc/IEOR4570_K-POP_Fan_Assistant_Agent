"""Offline-planning pipeline: Weverse notices -> judge -> Ticketmaster -> plan.

One orchestration step implementing the whiteboard architecture: a single
tool call fetches official notices, classifies them, searches Ticketmaster
only for ticketed events, and returns one merged JSON envelope. The LLM
synthesizes the final answer from this envelope; it never has to orchestrate
multi-tool calls for this slice.
"""

from __future__ import annotations

import re

from tools.integrations.ticketmaster.service import EventSearchClient, TicketmasterError, extract_events
from tools.integrations.weverse.notices import WeverseNoticeService, get_notice_service

from .judge import classify_notices, ticketed_decisions

# The judge must see every in-window notice, not the ten-result presentation
# cap the standalone notice search uses. Bounded so a very noisy community
# cannot flood the model context with titles.
PLANNING_NOTICE_LIMIT = 120
EXCERPT_MAX_CHARS = 180
BODY_WINDOW_CHARS = 80


def _error(code: str, message: str, source: str) -> dict:
    return {"ok": False, "error": code, "message": message, "source": source}


def _excerpt(text: str, signal: str | None, matched_field: str | None) -> str:
    """Evidence for the label, so the model can audit or override it.

    When the body decided the label the excerpt is centered on the matched
    signal: a head excerpt of an 8,000-character notice is greeting
    boilerplate and proves nothing about the classification.
    """
    text = (text or "").strip()
    if not text:
        return ""
    if matched_field == "body" and signal:
        hit = re.search(re.escape(signal), text, re.IGNORECASE)
        if hit:
            start = max(0, hit.start() - BODY_WINDOW_CHARS)
            end = min(len(text), hit.end() + BODY_WINDOW_CHARS)
            return (
                ("…" if start > 0 else "")
                + text[start:end].strip()
                + ("…" if end < len(text) else "")
            )
    return text[:EXCERPT_MAX_CHARS] + ("…" if len(text) > EXCERPT_MAX_CHARS else "")


def plan_offline_attendance(
    artist: str,
    city: str | None = None,
    country_code: str = "US",
    notice_query: str | None = None,
    notice_service: WeverseNoticeService | None = None,
    event_client: EventSearchClient | None = None,
) -> dict:
    """Run the full pipeline and return the merged JSON envelope.

    Failure semantics: the notice source is the spine of the tool, so any
    Weverse failure fails the call. A Ticketmaster failure does NOT discard
    the notices already fetched - it is reported under `ticketmaster_error`
    inside an otherwise successful envelope, because the official notice is
    what tells a fan where to buy when the artist sells off Ticketmaster.
    Success envelopes carry trace-safe summary keys (scanned_count /
    matched_count / notice_matched / truncated) plus payloads under notices /
    ticketmaster_events. Each notice carries its own classification evidence
    (`event_type`, `matched_signal`, `matched_field`, `excerpt`); the separate
    `decisions[]` array was removed because it duplicated every title and
    spent ~6,400 tokens to say nothing new.
    """
    notices = (notice_service or get_notice_service()).search_notices(
        artist, query=notice_query, limit=PLANNING_NOTICE_LIMIT
    )
    if not notices.get("ok"):
        return _error(notices["error"], notices.get("message", ""), "weverse")

    notice_results = notices.get("results") or []
    decisions = classify_notices(notice_results, city=city, country_code=country_code)
    annotated_notices = []
    for notice, decision in zip(notice_results, decisions):
        annotated_notices.append(
            {
                "notice_id": notice.get("notice_id"),
                "title": notice.get("title"),
                "published_at": notice.get("published_at"),
                "url": notice.get("url"),
                "event_type": decision["event_type"],
                # The label plus why it was assigned, so the model can check
                # the classifier's work instead of trusting it blindly.
                "matched_signal": decision["matched_signal"],
                "matched_field": decision["matched_field"],
                "excerpt": _excerpt(
                    str(notice.get("text") or ""),
                    decision["matched_signal"],
                    decision["matched_field"],
                ),
                "ticket_relevant": decision["ticketmaster_search"]["should_search"],
            }
        )

    ticketmaster_events: list[dict] = []
    searched: list[str] = []
    ticketmaster_error = None
    try:
        client = event_client or EventSearchClient()
        for args in ticketed_decisions(decisions):
            keyword = args["keyword"]
            if not keyword:
                continue
            data = client.search_events(
                keyword, city=args.get("city"), country_code=args.get("country_code") or "US"
            )
            searched.append(keyword)
            for event in extract_events(data, keyword=keyword):
                event = dict(event)
                event["matched_keyword"] = keyword
                ticketmaster_events.append(event)
    except TicketmasterError as exc:
        ticketmaster_error = {"error": exc.code, "message": exc.message, "source": "ticketmaster"}

    matched_count = len(ticketmaster_events)
    envelope = {
        "ok": True,
        "artist": notices.get("artist"),
        "scanned_count": notices.get("scanned_count", 0),
        "matched_count": matched_count,
        "truncated": bool(notices.get("truncated")),
        # Distinct from matched_count: how many in-window notices matched the
        # query, so "we read 86 notices and found 4 Ticketmaster events" is
        # expressible instead of the two counts being conflated.
        "notice_matched": notices.get("matched_count", len(notice_results)),
        "notice_count": len(annotated_notices),
        "notices": annotated_notices,
        "ticketmaster_searched": searched,
        "ticketmaster_events": ticketmaster_events,
    }
    if ticketmaster_error is not None:
        envelope["ticketmaster_error"] = ticketmaster_error
    return envelope
