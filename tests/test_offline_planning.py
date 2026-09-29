"""Tests for the offline-planning tool: judge, pipeline, registry.

All pipeline tests inject fake data-source clients - no network, no
credentials. Live variants live in test_live_smoke.py and
test_chat_chain_live.py.
"""

import json

from tools import TOOL_MAP, TOOLS, run_tool
from tools.integrations.ticketmaster.service import EventSearchClient
from tools.integrations.weverse.notices import MAX_RETURNED_RESULTS, WeverseNoticeService
from tools.offline_planning.judge import classify_notices, classify_notice, ticketed_decisions
from tools.offline_planning.pipeline import PLANNING_NOTICE_LIMIT, plan_offline_attendance
from tools.offline_planning.read_tool import read_weverse_notice_tool
from tools.offline_planning.tool import plan_offline_attendance_tool

TICKETED_NOTICE = {
    "notice_id": "100",
    "artist": "MONSTA X",
    "title": "[Notice] Ticket Sales Now Open!!",
    "published_at": "2026-09-01T00:00:00+00:00",
    "text": "The world tour tickets go on sale via Ticketmaster.",
    "url": "https://weverse.io/monstax/notice/100",
}
POPUP_NOTICE = {
    "notice_id": "101",
    "artist": "YOASOBI",
    "title": "POP-UP STORE in LA",
    "published_at": "2026-08-11T00:00:00+00:00",
    "text": "The pop-up store opens with limited merchandise.",
    "url": "https://weverse.io/yoasobi/notice/101",
}
PLAIN_NOTICE = {
    "notice_id": "102",
    "artist": "YOASOBI",
    "title": "New single out now",
    "published_at": "2026-07-01T00:00:00+00:00",
    "text": "Streaming links inside.",
    "url": "https://weverse.io/yoasobi/notice/102",
}


def notice_success(results):
    return {
        "ok": True,
        "artist": "YOASOBI",
        "community_id": 206,
        "cutoff": "2025-09-29T00:00:00+00:00",
        "scanned_count": len(results),
        "matched_count": len(results),
        "truncated": False,
        "results": results,
    }


class FakeNoticeService:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def search_notices(self, artist, query=None, limit=None):
        self.calls.append({"artist": artist, "query": query, "limit": limit})
        return self._result


class FakeEventClient:
    def __init__(self, events=None, error=None):
        self.events = events or []
        self.error = error
        self.calls = []

    def search_events(self, keyword, city=None, country_code="US"):
        self.calls.append({"keyword": keyword, "city": city, "country_code": country_code})
        if self.error is not None:
            raise self.error
        shaped = []
        for name in self.events:
            shaped.append(
                {
                    "name": name,
                    "event_id": f"id-{name}",
                    "date": "2026-10-06",
                    "time": "19:30:00",
                    "timezone": "America/New_York",
                    "status": "onsale",
                    "venue": "Arena",
                    "city": "New York",
                    "country": "United States",
                    "url": "https://www.ticketmaster.com/event/x",
                    "price_min": 50.0,
                    "price_max": 200.0,
                    "currency": "USD",
                }
            )
        return {"_embedded": {"events": [dict(e, id=e["event_id"]) for e in shaped]}} if shaped else {"page": {}}


# --- judge ----------------------------------------------------------------------


def test_judge_classifies_ticketed_popup_announcement():
    decisions = classify_notices([TICKETED_NOTICE, POPUP_NOTICE, PLAIN_NOTICE])
    assert [d["event_type"] for d in decisions] == ["ticketed_event", "popup", "announcement"]
    assert [d["ticketmaster_search"]["should_search"] for d in decisions] == [True, False, False]


def test_judge_deduplicates_ticketmaster_search_arguments():
    second_tour = dict(TICKETED_NOTICE, notice_id="103", title="Tour date announced")
    decisions = classify_notices([TICKETED_NOTICE, second_tour], city="New York", country_code="US")
    searches = ticketed_decisions(decisions)
    assert len(searches) == 1
    assert searches[0]["keyword"] == "MONSTA X"
    assert searches[0]["city"] == "New York"
    assert searches[0]["country_code"] == "US"


def test_classify_notice_keeps_standard_decision_shape():
    decision = classify_notices([TICKETED_NOTICE])[0]
    assert set(decision) == {
        "notice_id",
        "title",
        "event_type",
        "matched_signal",
        "matched_field",
        "ticketmaster_search",
    }
    assert set(decision["ticketmaster_search"]) == {"should_search", "keyword", "city", "country_code"}


def test_title_decides_and_body_boilerplate_cannot_overrule_it():
    """The aespa "Ticket Reservation & Admission" case.

    Its body offers a streamed alternative, so a combined scan demoted a real
    ticketed event to online_event.
    """
    notice = dict(
        TICKETED_NOTICE,
        notice_id="300",
        title="[NOTICE] “-SYNK : COMPLaeXITY-” Ticket Reservation & Admission Instructions",
        text="Seating opens at 6 PM. Those unable to attend may buy an online streaming ticket.",
    )
    decision = classify_notice(notice)
    assert decision["event_type"] == "ticketed_event"
    assert decision["matched_field"] == "title"
    assert decision["ticketmaster_search"]["should_search"] is True


def test_body_tier_decides_only_when_the_title_carries_no_signal():
    lightstick = dict(
        TICKETED_NOTICE,
        notice_id="301",
        title="[알림] aespa FANLIGHT EMBLEM- ONLINE SALES DETAILS",
        text="Sales details for the official merchandise below.",
    )
    decision = classify_notice(lightstick)
    assert decision["event_type"] == "merchandise"
    assert decision["matched_field"] == "body"
    assert decision["matched_signal"] == "merchandise"


def test_body_tier_ignores_generic_ticket_wording():
    """A legal-action notice mentions tickets; that must not look ticketed."""
    legal = dict(
        TICKETED_NOTICE,
        notice_id="302",
        title="[NOTICE] Update Notice on Legal Proceedings Against Violation of Artist Rights",
        text=(
            "We regularly take legal action against activities that infringe upon the "
            "artist's rights, including tickets resold above face value."
        ),
    )
    decision = classify_notice(legal)
    assert decision["event_type"] == "announcement"
    assert decision["matched_field"] is None
    assert decision["ticketmaster_search"]["should_search"] is False


def test_judge_routes_streaming_and_merch_away_from_ticketmaster():
    """Both mention ticket/tour wording, so they must not spend Discovery quota."""
    streaming = dict(
        TICKETED_NOTICE,
        notice_id="200",
        title="[NOTICE] 2026 aespa LIVE TOUR -SYNK : COMPLaeXITY-Online Streaming",
        text="Watch online. Ticket details for the venue are not part of this stream.",
    )
    merch = dict(
        TICKETED_NOTICE,
        notice_id="201",
        title="[알림] 2026-27 aespa LIVE TOUR OFFICIAL MERCHANDISE DETAILS",
        text="On-site booth sales only.",
    )
    decisions = classify_notices([streaming, merch])
    assert [d["event_type"] for d in decisions] == ["online_event", "merchandise"]
    assert [d["ticketmaster_search"]["should_search"] for d in decisions] == [False, False]


def test_judge_labels_korean_pre_recording_as_fan_event():
    """A broadcast pre-recording invites fans on site by membership application.

    Its boilerplate mentions tickets and reservation, which would otherwise
    send it to Ticketmaster and match nothing.
    """
    pre_recording = {
        "notice_id": "202",
        "artist": "AESPA",
        "title": "[공지] aespa “6/7 (일) SBS 인기가요 ‘LEMONADE’ 사전녹화” 참여 안내",
        "text": "등촌동 SBS 공개홀 앞. 팬클럽 본인확인 모임. 티켓 예매는 필요 없습니다.",
        "published_at": "2026-06-05T00:00:00+00:00",
        "url": "https://weverse.io/aespa/notice/202",
    }
    decision = classify_notice(pre_recording)
    assert decision["event_type"] == "fan_event"
    assert decision["matched_signal"] == "사전녹화"
    assert decision["ticketmaster_search"]["should_search"] is False


def test_judge_keeps_popup_label_ahead_of_merchandise():
    popup = {
        "notice_id": "203",
        "artist": "YOASOBI",
        "title": 'YOASOBI "NEVER ENDING STORIES" COMPLEX POP-UP in LA',
        "text": "The pop-up store sells limited merchandise.",
        "published_at": "2026-08-11T00:00:00+00:00",
        "url": "https://weverse.io/yoasobi/notice/203",
    }
    decision = classify_notice(popup)
    assert decision["event_type"] == "popup"
    assert decision["ticketmaster_search"]["should_search"] is False


# --- pipeline --------------------------------------------------------------------


def test_pipeline_runs_notice_then_ticketmaster_and_merges():
    notices = FakeNoticeService(notice_success([TICKETED_NOTICE, POPUP_NOTICE]))
    events = FakeEventClient(events=["MONSTA X 2026 WORLD TOUR"])
    result = plan_offline_attendance("monsta x", notice_service=notices, event_client=events)

    assert result["ok"] is True
    assert result["scanned_count"] == 2
    assert result["matched_count"] == 1
    assert result["notice_matched"] == 2
    assert result["truncated"] is False
    assert [n["event_type"] for n in result["notices"]] == ["ticketed_event", "popup"]
    assert result["ticketmaster_searched"] == ["MONSTA X"]
    assert result["ticketmaster_events"][0]["matched_keyword"] == "MONSTA X"
    # notice call forwarded artist and no query
    assert notices.calls == [{"artist": "monsta x", "query": None, "limit": PLANNING_NOTICE_LIMIT}]
    # exactly one TM search, with the judge's arguments
    assert events.calls == [{"keyword": "MONSTA X", "city": None, "country_code": "US"}]


def test_pipeline_classifies_beyond_the_ten_result_presentation_cap():
    """The judge must see the whole in-window feed, not the search page of ten."""
    feed = [dict(TICKETED_NOTICE, notice_id=str(300 + i)) for i in range(12)]
    payload = notice_success(feed)
    payload["matched_count"] = 86  # what Weverse actually found in-window
    notices = FakeNoticeService(payload)
    result = plan_offline_attendance("aespa", notice_service=notices, event_client=FakeEventClient(events=[]))

    assert notices.calls[0]["limit"] > MAX_RETURNED_RESULTS
    assert result["notice_count"] == 12
    assert result["notice_matched"] == 86


def test_pipeline_popup_only_never_calls_ticketmaster():
    notices = FakeNoticeService(notice_success([POPUP_NOTICE, PLAIN_NOTICE]))
    events = FakeEventClient(events=["should-not-be-fetched"])
    result = plan_offline_attendance("yoasobi", notice_service=notices, event_client=events)

    assert result["ok"] is True
    assert result["ticketmaster_searched"] == []
    assert result["ticketmaster_events"] == []
    assert result["matched_count"] == 0
    assert events.calls == []


def test_notice_carries_evidence_and_the_duplicated_decisions_array_is_gone():
    long_body = "Hello, thank you for your support. " * 30 + "Official merchandise sales open Friday."
    goods = dict(TICKETED_NOTICE, notice_id="400", title="Sales information", text=long_body)
    result = plan_offline_attendance(
        "yoasobi",
        notice_service=FakeNoticeService(notice_success([goods])),
        event_client=FakeEventClient(),
    )
    assert "decisions" not in result, "titles must not be serialized twice"
    entry = result["notices"][0]
    assert entry["event_type"] == "merchandise" and entry["matched_field"] == "body"
    # Centered on the signal: a head excerpt of this body is greeting boilerplate
    # and would not justify the label at all.
    assert "merchandise" in entry["excerpt"]
    assert len(entry["excerpt"]) <= 200


def test_excerpt_for_a_title_match_is_the_head_of_the_body():
    result = plan_offline_attendance(
        "monsta x",
        notice_service=FakeNoticeService(notice_success([TICKETED_NOTICE])),
        event_client=FakeEventClient(),
    )
    assert result["notices"][0]["excerpt"].startswith("The world tour tickets")


def test_envelope_is_smaller_than_the_evidence_free_version_it_replaced():
    """47,387 chars was the measured size of the aespa envelope with no body text.

    The same call now carries an excerpt per notice and must still cost less,
    which is the whole point of dropping the duplicated decisions array.
    """
    feed = [dict(TICKETED_NOTICE, notice_id=str(500 + i), text="body text. " * 400) for i in range(86)]
    result = plan_offline_attendance(
        "aespa",
        notice_service=FakeNoticeService(notice_success(feed)),
        event_client=FakeEventClient(),
    )
    assert len(json.dumps(result, ensure_ascii=False)) < 47_387


def test_pipeline_notice_failure_fails_with_source():
    notices = FakeNoticeService({"ok": False, "error": "authentication_failed", "message": "denied"})
    result = plan_offline_attendance("yoasobi", notice_service=notices, event_client=FakeEventClient())
    assert result["ok"] is False
    assert result["error"] == "authentication_failed"
    assert result["source"] == "weverse"


def test_pipeline_ticketmaster_failure_keeps_the_notices():
    """Quota or an outage must not throw away the official notices we already have."""
    from tools.integrations.ticketmaster.service import RateLimited

    notices = FakeNoticeService(notice_success([TICKETED_NOTICE]))
    events = FakeEventClient(error=RateLimited())
    result = plan_offline_attendance("monsta x", notice_service=notices, event_client=events)

    assert result["ok"] is True
    assert result["matched_count"] == 0
    assert result["ticketmaster_events"] == []
    assert result["ticketmaster_error"]["error"] == "rate_limited"
    assert result["ticketmaster_error"]["source"] == "ticketmaster"
    assert result["notices"], "notice payload survives the Ticketmaster failure"
    assert "message" in result["ticketmaster_error"]


# --- tool wrapper & registry -------------------------------------------------------


def test_tool_wrapper_normalizes_arguments(monkeypatch):
    import tools.offline_planning.tool as tool_module

    notices = FakeNoticeService(notice_success([PLAIN_NOTICE]))
    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return plan_offline_attendance(
            notice_service=notices,
            event_client=FakeEventClient(),
            **kwargs,
        )

    monkeypatch.setattr(tool_module, "plan_offline_attendance", spy)
    result = json.loads(
        plan_offline_attendance_tool("  yoasobi  ", city="  ", country_code="us", notice_query=None)
    )

    assert result["ok"] is True
    assert seen["artist"] == "yoasobi"  # stripped
    assert seen["city"] is None  # blank string normalized to None
    assert seen["country_code"] == "US"  # uppercased


def test_registry_exposes_both_offline_planning_tools():
    names = sorted(tool["function"]["name"] for tool in TOOLS)
    assert names == ["plan_offline_attendance", "read_weverse_notice"]
    assert TOOL_MAP["plan_offline_attendance"] is plan_offline_attendance_tool
    assert TOOL_MAP["read_weverse_notice"] is read_weverse_notice_tool

    plan_schema = TOOLS[0]["function"]["parameters"]
    assert plan_schema["required"] == ["artist"]
    assert set(plan_schema["properties"]) == {"artist", "city", "country_code", "notice_query"}

    read_schema = TOOLS[1]["function"]["parameters"]
    assert read_schema["required"] == ["artist", "notice_id"]
    assert set(read_schema["properties"]) == {"artist", "notice_id", "max_chars"}


def test_read_tool_rejects_missing_arguments_without_raising():
    assert json.loads(run_tool("read_weverse_notice", {}))["error"] == "bad_arguments"
    assert json.loads(run_tool("read_weverse_notice", {"artist": "aespa"}))["error"] == "bad_arguments"


def test_run_tool_envelopes_for_offline_planning(monkeypatch):
    import tools.offline_planning.tool as tool_module

    notices = FakeNoticeService(notice_success([TICKETED_NOTICE]))

    def spy(**kwargs):
        return plan_offline_attendance(
            notice_service=notices,
            event_client=FakeEventClient(events=["MONSTA X 2026 WORLD TOUR"]),
            **kwargs,
        )

    monkeypatch.setattr(tool_module, "plan_offline_attendance", spy)
    result = json.loads(run_tool("plan_offline_attendance", {"artist": "monsta x"}))

    assert result["ok"] is True
    assert result["matched_count"] == 1
    assert "notices" in result and "ticketmaster_events" in result
    assert json.loads(run_tool("plan_offline_attendance", {}))["error"] == "bad_arguments"
