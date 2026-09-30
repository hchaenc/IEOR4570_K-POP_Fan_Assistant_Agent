"""Notice event-type annotation, shared by every tool that reads notices.

Deterministic, tiered heuristic:

  1. the title decides;
  2. the body is consulted only when the title says nothing.

The tiering is what makes the hint lists workable. Weverse titles are written
by the agencies to be self-describing ("Ticket Reservation & Admission
Instruction", "사전녹화 참여 안내"), while bodies are long and full of
boilerplate: a legal-action notice mentions "ticket", a pre-recording notice
recites ticket and reservation wording, and a tour merchandise notice contains
"tour". Scanning title and body together produced false positives out of that
boilerplate, and scanning titles alone missed notices whose type only appears
in the body (lightstick sales, streamed birthday parties). Each tier therefore
gets its own hint list, with the body tier deliberately narrower.

This is a pre-filter, not an oracle. Callers ship `matched_signal`,
`matched_field` and an evidence excerpt alongside the label so the LLM can
disagree with it using text it can actually see; `ticket_relevant` is the
suggestion of when a Ticketmaster lookup is worth a request.
"""

from __future__ import annotations

TICKETED_EVENT = "ticketed_event"

# --- title tier ---------------------------------------------------------------

# Broadcast / streamed watching parties announced as such.
ONLINE_TITLE_HINTS = (
    "online streaming",
    "live streaming",
    "streaming ticket",
    "weverse live",
)
POPUP_HINTS = ("pop-up", "popup", "exhibition", "showcase store", "팝업", "팝업스토어")
# Offline attendance by membership application or broadcast appearance, not purchase.
FAN_TITLE_HINTS = (
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
MERCHANDISE_HINTS = ("merchandise", "merch", "lucky draw", "현장 판매", "현장판매", "굿즈")
# Seats you can actually buy.
TICKETED_TITLE_HINTS = (
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

# --- body tier ----------------------------------------------------------------
# Narrower on purpose: these only run on notices whose title carried no signal,
# and the body is where boilerplate lives.

# A bare "live broadcast" is a reliable body signal here: pre-recording notices,
# which also mention broadcasts, are already caught by the title tier.
ONLINE_BODY_HINTS = ONLINE_TITLE_HINTS + (
    "생방송",
    "온라인 생중계",
    "실시간 중계",
    "시청 인증",
    "시청권",
)
FAN_BODY_HINTS = (
    "사전녹화",
    "공개홀",
    "참여 신청",
    "참여신청",
    "가요대전",
    "가요대제전",
    "선착순 신청",
)
# "tour", "concert" and "ticket" are excluded: they appear in legal notices,
# venue advisories and box-office boilerplate, which is how a BTS legal-action
# announcement was once classified as a ticketed event.
TICKETED_BODY_HINTS = (
    "ticket reservation",
    "presale",
    "pre-sale",
    "티켓 예매",
    "예매",
    "좌석",
    "콘서트",
    "팬미팅",
)

# One ladder, two projections: adding a label means adding one row.
_RULES = (
    ("online_event", ONLINE_TITLE_HINTS, ONLINE_BODY_HINTS),
    ("popup", POPUP_HINTS, POPUP_HINTS),
    ("fan_event", FAN_TITLE_HINTS, FAN_BODY_HINTS),
    ("merchandise", MERCHANDISE_HINTS, MERCHANDISE_HINTS),
    (TICKETED_EVENT, TICKETED_TITLE_HINTS, TICKETED_BODY_HINTS),
)

TITLE_RULES = tuple((label, tuple(h.casefold() for h in title_hints)) for label, title_hints, _ in _RULES)
BODY_RULES = tuple((label, tuple(h.casefold() for h in body_hints)) for label, _, body_hints in _RULES)


def _first_match(haystack: str, rules: tuple) -> tuple[str | None, str | None]:
    for label, hints in rules:
        for hint in hints:
            if hint in haystack:
                return label, hint
    return None, None


def classify_notice(title: str, text: str = "") -> dict:
    """Label one notice and report the evidence behind the label."""
    label, signal = _first_match(str(title or "").casefold(), TITLE_RULES)
    matched_field = "title"
    if label is None:
        label, signal = _first_match(str(text or "").casefold(), BODY_RULES)
        matched_field = "body"
    if label is None:
        label, matched_field = "announcement", None

    return {
        "event_type": label,
        "matched_signal": signal,
        "matched_field": matched_field,
        # A suggestion for the model: only purchasable seats usually justify a
        # Ticketmaster request.
        "ticket_relevant": label == TICKETED_EVENT,
    }
