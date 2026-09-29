"""Notice classifier for the offline-planning pipeline.

Deterministic, priority-ordered heuristic implementing the decision gate from
docs/TOOLS.md: every notice gets exactly one `event_type`, and only
`ticketed_event` spends Ticketmaster quota.

Priority matters more than vocabulary. Most Weverse notices are Korean, and a
Korean broadcast pre-recording notice contains the words ticket / reservation
in its boilerplate, while a tour merchandise notice contains "tour" and a
streaming notice contains "ticket". Matching ticket hints first would send all
of them to Ticketmaster and return nothing, so the online, pop-up and
fan-application checks run before the merchandise and ticketed checks.

The classifier is a pre-filter: the LLM still receives every notice and makes
the final call when writing the answer.
"""

from __future__ import annotations

# Broadcast / streamed watching parties: online, never on Ticketmaster.
# Deliberately excludes a bare "streaming" or the Korean 생방송 ("live broadcast"):
# broadcast-appearance notices mention being aired live while actually inviting
# fans to an in-person pre-recording.
ONLINE_HINTS = (
    "online streaming",
    "live streaming",
    "streaming ticket",
    "weverse live",
    "온라인 생중계",
    "실시간 중계",
    "시청 인증",
    "시청권",
)
# Goods sales tied to an event, including on-site booth sales: no seats to buy.
MERCHANDISE_HINTS = (
    "merchandise",
    "merch",
    "lucky draw",
    "현장 판매",
    "현장판매",
    "굿즈",
)
# Offline attendance by membership application, not purchase. Deliberately
# excludes first-come wording (선착순) because ticket presales use it too.
FAN_APPLICATION_HINTS = (
    "사전녹화",
    "사전 녹화",
    "사전신청",
    "사전 신청",
    "참여 신청",
    "참여신청",
    "공개홀",
    "가요대전",
    "가요대제전",
    "어워드",
    "시상식",
    "방송 출연",
    "출연 안내",
)
# Seats you can actually buy.
TICKETED_HINTS = (
    "ticket",
    "presale",
    "pre-sale",
    "reservation",
    "admission",
    "tour",
    "concert",
    "fan meeting",
    "fanmeeting",
    "live viewing",
    "티켓",
    "예매",
    "콘서트",
    "좌석",
    "팬미팅",
    "투어",
    "공연",
)
POPUP_HINTS = ("pop-up", "popup", "exhibition", "showcase store", "팝업", "팝업스토어")

# Evaluated in this order; the first matching group wins. Pop-up and
# fan-application labels come before merchandise and ticketed because those
# notices routinely mention goods sales or ticket boilerplate in their bodies.
CLASSIFICATION_RULES = (
    ("online_event", ONLINE_HINTS),
    ("popup", POPUP_HINTS),
    ("fan_event", FAN_APPLICATION_HINTS),
    ("merchandise", MERCHANDISE_HINTS),
    ("ticketed_event", TICKETED_HINTS),
)


def classify_notice(notice: dict) -> dict:
    """Return the standard decision object (docs/TOOLS.md, judge protocol)."""
    haystack = f"{notice.get('title', '')} {notice.get('text', '')}".casefold()
    event_type = "announcement"
    matched_signal = None
    for label, hints in CLASSIFICATION_RULES:
        for hint in hints:
            if hint.casefold() in haystack:
                event_type, matched_signal = label, hint
                break
        if matched_signal:
            break

    return {
        "notice_id": notice.get("notice_id"),
        "title": notice.get("title"),
        "event_type": event_type,
        "matched_signal": matched_signal,
        "ticketmaster_search": {
            # Only purchasable seats justify a Discovery API request.
            "should_search": event_type == "ticketed_event",
            "keyword": notice.get("artist"),
            # city/country come from the tool call, merged by the pipeline
        },
    }


def classify_notices(notices: list[dict], city: str | None = None, country_code: str = "US") -> list[dict]:
    """Classify a notice list; merges the caller's geo filters into search args."""
    decisions = []
    for notice in notices:
        decision = classify_notice(notice)
        decision["ticketmaster_search"]["city"] = city
        decision["ticketmaster_search"]["country_code"] = country_code
        decisions.append(decision)
    return decisions


def ticketed_decisions(decisions: list[dict]) -> list[dict]:
    """Unique Ticketmaster search arguments among ticketed decisions, in order."""
    seen: set[tuple] = set()
    unique = []
    for decision in decisions:
        args = decision["ticketmaster_search"]
        if not args["should_search"]:
            continue
        key = (args["keyword"], args["city"], args["country_code"])
        if key not in seen:
            seen.add(key)
            unique.append(dict(args))
    return unique
