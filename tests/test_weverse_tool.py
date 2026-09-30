"""Tests for the Weverse original tool: annotation, evidence and envelopes.

The gateway is faked at the HTTP seam, so these exercise the real service plus
the real tool layer without network or credentials.
"""

import json
import time

import tools.originals.weverse.tool as tool_module
from tools import TOOL_MAP, TOOLS, run_tool
from tools.originals.weverse.gateway import WeverseGatewayClient
from tools.originals.weverse.notices import WeverseNoticeService

RECENT_MS = int((time.time() - 10 * 86400) * 1000)
RESOLVER_PATH = "communityIdUrlPathByUrlPathArtistCode"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def no_refresh(*args, **kwargs):
    raise AssertionError("token refresh must not run in this test")


def use_notices(monkeypatch, notices, resolver=None):
    """Point the tool's shared service at a fake gateway for one test."""
    items = [
        {
            "noticeId": notice["id"],
            "title": notice["title"],
            "body": notice.get("body", "short body"),
            "publishAt": notice.get("publishAt", RECENT_MS),
            "shareUrl": f"https://weverse.io/yoasobi/notice/{notice['id']}",
        }
        for notice in notices
    ]
    payload = resolver if resolver is not None else {"communityId": 206, "urlPath": "yoasobi"}

    def getter(url, params=None, headers=None, timeout=None):
        if RESOLVER_PATH in url:
            return FakeResponse(payload)
        return FakeResponse(
            {"content": {"notices": {"data": items, "paging": {"pageNo": 1, "maxPageNo": 1}}}}
        )

    service = WeverseNoticeService(
        client=WeverseGatewayClient(
            getter=getter,
            poster=no_refresh,
            access_token="t",
            refresh_token="r",
            env_path="nonexistent.env",
        )
    )
    monkeypatch.setattr(tool_module, "get_notice_service", lambda: service)
    return service


# --- registry -------------------------------------------------------------------


def test_registry_exposes_each_original_tool_and_the_common_tool():
    names = sorted(TOOL_MAP)
    assert names == ["appraise_kpop_merch", "search_ticketmaster_events", "venue_survival_kit", "weverse_notices"]
    # Neither the old composite nor the two-function split survives the merge.
    for retired in ("plan_offline_attendance", "search_weverse_notices", "read_weverse_notice"):
        assert retired not in TOOL_MAP
    assert sorted(tool["function"]["name"] for tool in TOOLS) == names


def test_empty_packages_are_skipped_by_the_registry():
    """A teammate's bare directory must not put an empty shell in front of the model."""
    assert all(tool["function"]["name"] in TOOL_MAP for tool in TOOLS)
    assert all(callable(handler) for handler in TOOL_MAP.values())


# --- annotation -----------------------------------------------------------------


def test_search_annotates_each_notice_and_drops_the_full_body(monkeypatch):
    use_notices(
        monkeypatch,
        [
            {"id": 1, "title": "[NOTICE] 2026-27 aespa LIVE TOUR Announcement", "body": "x" * 900},
            {"id": 2, "title": "[공지] aespa FANLIGHT EMBLEM ONLINE SALES", "body": "official merchandise sales open Friday"},
        ],
    )
    result = json.loads(run_tool("weverse_notices", {"artist": "aespa"}))

    assert result["ok"] is True
    assert result["notice_count"] == 2
    assert result["scanned_count"] == 2
    assert result["matched_count"] == 2
    first, second = result["notices"]
    assert first["event_type"] == "ticketed_event" and first["ticket_relevant"] is True
    assert second["event_type"] == "merchandise" and second["matched_field"] == "body"
    for notice in result["notices"]:
        assert set(notice) == {
            "notice_id",
            "title",
            "published_at",
            "url",
            "event_type",
            "matched_signal",
            "matched_field",
            "ticket_relevant",
            "excerpt",
        }
        assert "text" not in notice, "the full body belongs to full-text mode, not the digest"


def test_excerpt_for_a_body_label_is_centered_on_the_signal(monkeypatch):
    body = "Hello, thank you for your support. " * 30 + "Official merchandise sales open Friday."
    use_notices(monkeypatch, [{"id": 5, "title": "Sales information", "body": body}])

    notice = json.loads(run_tool("weverse_notices", {"artist": "aespa"}))["notices"][0]
    assert notice["matched_field"] == "body"
    assert "merchandise" in notice["excerpt"], "a head excerpt would show only greetings"
    assert len(notice["excerpt"]) <= 200


def test_excerpt_for_a_title_label_is_the_head_of_the_body(monkeypatch):
    use_notices(
        monkeypatch,
        [{"id": 6, "title": "Ticket Reservation & Admission", "body": "Doors open at 6 PM at the arena."}],
    )
    notice = json.loads(run_tool("weverse_notices", {"artist": "aespa"}))["notices"][0]
    assert notice["excerpt"].startswith("Doors open at 6 PM")


def test_feed_stays_smaller_than_the_old_composite_envelope(monkeypatch):
    """47,387 chars was the measured composite size for aespa's 86 in-window notices.

    The split tool must not regress that while carrying evidence per notice.
    """
    use_notices(
        monkeypatch,
        [
            {"id": 100 + i, "title": f"[Notice] update {i}", "body": "body text. " * 400}
            for i in range(86)
        ],
    )
    payload = run_tool("weverse_notices", {"artist": "aespa"})
    assert len(payload) < 47_387


def test_query_filter_and_limit_pass_through(monkeypatch):
    use_notices(
        monkeypatch,
        [
            {"id": 1, "title": "Ticket Reservation", "body": "b"},
            {"id": 2, "title": "Merchandise", "body": "b"},
            {"id": 3, "title": "Another ticket notice", "body": "b"},
        ],
    )
    result = json.loads(run_tool("weverse_notices", {"artist": "aespa", "query": "ticket", "limit": 1}))
    assert result["matched_count"] == 2, "matched_count reports the whole match set"
    assert [n["title"] for n in result["notices"]] == ["Another ticket notice"] or len(result["notices"]) == 1


# --- errors ---------------------------------------------------------------------


def test_unknown_artist_returns_a_structured_error_string(monkeypatch):
    use_notices(monkeypatch, [], resolver={})
    result = json.loads(run_tool("weverse_notices", {"artist": "not-a-community"}))
    assert result["ok"] is False
    assert result["error"] == "community_not_joined"
    assert isinstance(result["message"], str) and result["message"]


def test_missing_required_argument_does_not_crash_the_loop():
    assert json.loads(run_tool("weverse_notices", {}))["error"] == "bad_arguments"


def test_mode_names_which_shape_came_back(monkeypatch):
    """One function, two response shapes: the mode field tells them apart."""
    use_notices(monkeypatch, [{"id": 9, "title": "Tour", "body": "Ticket sales open soon."}])

    digest = json.loads(run_tool("weverse_notices", {"artist": "aespa"}))
    assert digest["mode"] == "digest" and "notices" in digest and "text" not in digest

    full = json.loads(run_tool("weverse_notices", {"artist": "aespa", "notice_id": "9"}))
    assert full["mode"] == "full_text" and "text" in full and "notices" not in full


# --- read -----------------------------------------------------------------------


def test_read_returns_the_full_text_and_reports_truncation(monkeypatch):
    long_body = "<p>" + ("Application window opens 6 May, 10:00 KST. " * 120) + "</p>"
    use_notices(monkeypatch, [{"id": 42, "title": "Presale", "body": long_body}])

    result = json.loads(run_tool("weverse_notices", {"artist": "aespa", "notice_id": "42"}))
    assert result["ok"] is True
    assert result["notice_id"] == "42"
    assert result["text"].startswith("Application window opens")
    assert "<p>" not in result["text"]
    assert result["truncated"] is True
    assert result["text_chars"] > len(result["text"])


def test_read_reuses_the_searched_artist_name(monkeypatch):
    use_notices(monkeypatch, [{"id": 43, "title": "Tour", "body": "Ticket sales open soon."}])
    result = json.loads(run_tool("weverse_notices", {"artist": "MONSTA X", "notice_id": "43"}))
    assert result["ok"] is True
    assert result["artist"] == "MONSTA X"
