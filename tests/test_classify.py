"""Tests for the shared notice classifier in tools/common/classify.py.

Pure functions over fake notices - no network, no model.
"""

from tools.common.classify import classify_notice


def label(title, text=""):
    return classify_notice(title, text)


# --- tiering -------------------------------------------------------------------


def test_title_decides_and_is_not_overruled_by_body_boilerplate():
    """The aespa "Ticket Reservation & Admission" case.

    Its body offers a streamed alternative, so a combined scan demoted a real
    ticketed event to online_event.
    """
    result = label(
        "[NOTICE] “-SYNK : COMPLaeXITY-” Ticket Reservation & Admission Instructions",
        "Seating opens at 6 PM. Those unable to attend may buy an online streaming ticket.",
    )
    assert result["event_type"] == "ticketed_event"
    assert result["matched_field"] == "title"
    assert result["ticket_relevant"] is True


def test_body_tier_only_runs_when_the_title_carries_no_signal():
    result = label(
        "[알림] aespa FANLIGHT EMBLEM- ONLINE SALES DETAILS",
        "Sales details for the official merchandise below.",
    )
    assert result["event_type"] == "merchandise"
    assert result["matched_field"] == "body"
    assert result["matched_signal"] == "merchandise"
    assert result["ticket_relevant"] is False


def test_no_signal_anywhere_is_an_announcement_with_no_evidence():
    result = label("[Notice] Update on our fan community guidelines", "Please be kind online.")
    assert result["event_type"] == "announcement"
    assert result["matched_field"] is None
    assert result["matched_signal"] is None


# --- body tier must not fall for boilerplate ------------------------------------


def test_body_tier_ignores_generic_ticket_wording():
    """A legal-action notice mentions tickets; that must not look ticketed."""
    result = label(
        "[NOTICE] Update Notice on Legal Proceedings Against Violation of Artist Rights",
        "We take legal action against infringement, including tickets resold above face value.",
    )
    assert result["event_type"] == "announcement"
    assert result["ticket_relevant"] is False


def test_live_broadcast_is_online_only_when_the_title_says_nothing_else():
    result = label("[공지] aespa 'WDA' 방송 안내", "멤버들은 생방송으로 팬들을 만납니다.")
    assert result["event_type"] == "online_event"
    assert result["matched_field"] == "body"


# --- label precedence -----------------------------------------------------------


def test_korean_pre_recording_is_a_fan_event_not_a_ticketed_one():
    """Attendance by membership application: offline, but never on Ticketmaster."""
    result = label(
        "[공지] aespa “6/7 (일) SBS 인기가요 ‘LEMONADE’ 사전녹화” 참여 안내",
        "등촌동 SBS 공개홀 앞. 팬클럽 본인확인 모임. 티켓 예매는 필요 없습니다.",
    )
    assert result["event_type"] == "fan_event"
    assert result["matched_signal"] == "사전녹화"
    assert result["ticket_relevant"] is False


def test_popup_label_wins_over_merchandise():
    result = label(
        'YOASOBI "NEVER ENDING STORIES" COMPLEX POP-UP in LA',
        "The pop-up store sells limited merchandise.",
    )
    assert result["event_type"] == "popup"
    assert result["ticket_relevant"] is False


def test_streaming_and_merch_are_routed_away_from_ticketmaster():
    streaming = label(
        "[NOTICE] 2026 aespa LIVE TOUR -SYNK : COMPLaeXITY-Online Streaming",
        "Watch online. Ticket details for the venue are not part of this stream.",
    )
    merch = label("[알림] 2026-27 aespa LIVE TOUR OFFICIAL MERCHANDISE DETAILS", "On-site booth sales only.")
    assert streaming["event_type"] == "online_event"
    assert merch["event_type"] == "merchandise"
    assert streaming["ticket_relevant"] is False
    assert merch["ticket_relevant"] is False


def test_tour_and_presale_wording_still_reads_as_ticketed():
    assert label("[NOTICE] 2026-27 aespa LIVE TOUR Announcement")["event_type"] == "ticketed_event"
    assert label("[NOTICE] US/CA Membership (GL) Pre-Sale Information")["event_type"] == "ticketed_event"


def test_missing_fields_do_not_raise():
    assert label(None, None)["event_type"] == "announcement"
    assert label("", "")["event_type"] == "announcement"
