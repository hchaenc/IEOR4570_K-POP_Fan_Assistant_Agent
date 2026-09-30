"""Mocked tests for the Weverse notice service and its signed gateway client.

All tests run against fake HTTP responses injected through the client's
getter/poster seams - no network access and no real credentials. The live path
(same imports, real gateway) is covered in test_live_e2e.py.
"""

import base64
import json
import time

import pytest

import tools
import tools.originals.weverse.notices as weverse_notice_module
from tools import TOOL_MAP, TOOLS, run_tool
from tools.originals.weverse.gateway import GatewayError, WeverseGatewayClient, sign_request
from tools.originals.weverse.notices import (
    DEFAULT_NOTICE_LIMIT,
    DEFAULT_READ_CHARS,
    MAX_READ_CHARS,
    WeverseNoticeService,
    html_to_text,
)

RECENT_MS = int((time.time() - 10 * 86400) * 1000)
OLDER_MS = int((time.time() - 40 * 86400) * 1000)
OLD_MS = int((time.time() - 400 * 86400) * 1000)

RESOLVER_PATH = "communityIdUrlPathByUrlPathArtistCode"
NOTICES_PATH = "NOTICE/tabContent"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def make_notice(notice_id, title, body, published_ms):
    return {
        "noticeId": notice_id,
        "title": title,
        "body": body,
        "publishAt": published_ms,
        "shareUrl": f"https://weverse.io/yoasobi/notice/{notice_id}",
    }


def notices_block(items, page_no=1, max_page_no=1):
    return {
        "data": items,
        "paging": {"pageNo": page_no, "maxPageNo": max_page_no, "limit": 10},
        "totalCount": len(items),
    }


def fake_getter(blocks, resolver=None, first_notices_status=None):
    """Serves the slug resolver, then ONE notices block (the upstream ignores
    pagination and we fetch a single large page); a 401 can be simulated for
    the first notices call (token-expiry semantics)."""
    calls = []
    resolver_payload = resolver if resolver is not None else {"communityId": 206, "urlPath": "yoasobi"}
    state = {"notices_calls": 0}

    def getter(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": dict(params or {})})
        if RESOLVER_PATH in url:
            return FakeResponse(resolver_payload)
        if NOTICES_PATH in url:
            state["notices_calls"] += 1
            if state["notices_calls"] == 1 and first_notices_status is not None:
                return FakeResponse(None, status_code=first_notices_status)
            return FakeResponse({"content": {"notices": blocks}})
        return FakeResponse(None, status_code=404)

    getter.calls = calls
    return getter


def no_refresh(*args, **kwargs):
    raise AssertionError("token refresh must not run in this test")


def make_service(blocks, resolver=None, poster=no_refresh):
    client = WeverseGatewayClient(
        getter=fake_getter(blocks, resolver),
        poster=poster,
        access_token="test-token",
        refresh_token="test-refresh",
        env_path="nonexistent.env",
    )
    return WeverseNoticeService(client=client)


# --- error contract ----------------------------------------------------------


def test_missing_credentials_returns_structured_error(monkeypatch):
    monkeypatch.setattr("tools.originals.weverse.gateway.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("WEVERSE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WEVERSE_REFRESH_TOKEN", raising=False)
    client = WeverseGatewayClient(getter=fake_getter([]), env_path="nonexistent.env")
    result = WeverseNoticeService(client=client).search_notices("yoasobi")
    assert result["ok"] is False
    assert result["error"] == "missing_credentials"
    assert isinstance(result["message"], str)


def test_authentication_failure_does_not_leak_secrets(monkeypatch):
    monkeypatch.setattr("tools.originals.weverse.gateway.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("WEVERSE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WEVERSE_REFRESH_TOKEN", raising=False)
    secret = "super-secret-access-abc123"
    refresh_secret = "super-secret-refresh-xyz789"

    def poster(url, json=None, headers=None, timeout=None):
        return FakeResponse(None, status_code=401)

    client = WeverseGatewayClient(
        getter=fake_getter([], first_notices_status=401),
        poster=poster,
        access_token=secret,
        refresh_token=refresh_secret,
        env_path="nonexistent.env",
    )
    result = WeverseNoticeService(client=client).search_notices("yoasobi")
    assert result["ok"] is False
    assert result["error"] == "authentication_failed"
    dumped = json.dumps(result)
    assert secret not in dumped and refresh_secret not in dumped


def test_unknown_artist_returns_community_not_joined():
    service = make_service([], resolver={"status": 404})
    result = service.search_notices("not-a-community")
    assert result["ok"] is False
    assert result["error"] == "community_not_joined"


def test_ambiguous_artist_returns_candidates():
    result = make_service([]).search_notices("   ")
    assert result["ok"] is False
    assert result["error"] == "ambiguous_artist"


def test_spaced_artist_name_falls_back_to_the_despaced_slug():
    """Weverse files MONSTA X under urlPath 'monstax', not the hyphenated guess.

    The returned artist name must stay as typed, because the model reuses it as
    the Ticketmaster keyword and 'MONSTAX' matches no events there.
    """
    keywords = []

    def getter(url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        if RESOLVER_PATH in url:
            keywords.append(params["keyword"])
            found = params["keyword"] == "monstax"
            return FakeResponse({"urlPath": "monstax", "communityId": 105} if found else {})
        return FakeResponse(
            {
                "content": {
                    "notices": notices_block(
                        [{"noticeId": 7, "title": "Tour", "body": "tickets", "publishAt": RECENT_MS}]
                    )
                }
            }
        )

    client = WeverseGatewayClient(
        getter=getter,
        poster=no_refresh,
        access_token="t",
        refresh_token="r",
        env_path="nonexistent.env",
    )
    result = WeverseNoticeService(client=client).search_notices("MONSTA X")

    assert keywords == ["monsta-x", "monstax"]
    assert result["ok"] is True
    assert result["artist"] == "MONSTA X"
    assert result["results"][0]["url"] == "https://weverse.io/monstax/notice/7"


def test_limit_caps_the_feed_and_none_removes_the_cap():
    items = [make_notice(i, f"n{i}", "body", RECENT_MS - i * 3600_000) for i in range(12)]
    service = make_service(notices_block(items))

    capped = service.search_notices("yoasobi", limit=5)
    whole = service.search_notices("yoasobi", limit=None)

    assert len(capped["results"]) == 5
    # matched_count always reports the whole in-window match set, not the page
    assert capped["matched_count"] == 12
    assert len(whole["results"]) == 12
    assert whole["matched_count"] == 12


def test_missing_notice_feed_is_not_reported_as_transient():
    """MONSTA X's community answers HTTP 404 on the NOTICE tab, permanently.

    Retrying cannot help, so the tool must not say "try again later".
    """
    getter = fake_getter(notices_block([]), first_notices_status=404)
    client = WeverseGatewayClient(
        getter=getter,
        poster=no_refresh,
        access_token="t",
        refresh_token="r",
        env_path="nonexistent.env",
    )
    result = WeverseNoticeService(client=client).search_notices("MONSTA X")

    assert result["ok"] is False
    assert result["error"] == "community_not_joined"
    assert "no notice feed" in result["message"]


# --- auto refresh --------------------------------------------------------------


def test_access_token_auto_refreshes_on_401():
    getter = fake_getter(
        notices_block([make_notice(1, "new", "b", RECENT_MS)]), first_notices_status=401
    )

    def poster(url, json=None, headers=None, timeout=None):
        assert json == {"refreshToken": "test-refresh"}
        return FakeResponse({"accessToken": "fresh-access", "refreshToken": "rotated-refresh"})

    client = WeverseGatewayClient(
        getter=getter,
        poster=poster,
        access_token="stale-access",
        refresh_token="test-refresh",
        env_path="nonexistent.env",
    )
    result = WeverseNoticeService(client=client).search_notices("yoasobi")
    assert result["ok"] is True
    assert client._tokens()[0] == "fresh-access"
    assert client._tokens()[1] == "rotated-refresh"
    # resolver 200, notices 401, retry 200
    notices_calls = [c for c in getter.calls if NOTICES_PATH in c["url"]]
    assert len(notices_calls) == 2


def test_refresh_failure_returns_authentication_failed(monkeypatch):
    monkeypatch.setattr("tools.originals.weverse.gateway.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("WEVERSE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WEVERSE_REFRESH_TOKEN", raising=False)

    def always_401(url, params=None, json=None, headers=None, timeout=None):
        return FakeResponse(None, status_code=401)

    client = WeverseGatewayClient(
        getter=always_401,
        poster=always_401,
        access_token="stale",
        refresh_token="stale-refresh",
        env_path="nonexistent.env",
    )
    result = WeverseNoticeService(client=client).search_notices("yoasobi")
    assert result["ok"] is False
    assert result["error"] == "authentication_failed"


def test_rotated_refresh_token_is_persisted(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("WEVERSE_ACCESS_TOKEN=old\nWEVERSE_REFRESH_TOKEN=old-refresh\n", encoding="utf-8")
    getter = fake_getter(
        notices_block([make_notice(1, "n", "b", RECENT_MS)]), first_notices_status=401
    )

    def poster(url, json=None, headers=None, timeout=None):
        return FakeResponse({"accessToken": "a2", "refreshToken": "r2"})

    client = WeverseGatewayClient(
        getter=getter, poster=poster, access_token="a1", refresh_token="r1", env_path=env_file
    )
    result = WeverseNoticeService(client=client).search_notices("yoasobi")
    assert result["ok"] is True
    content = env_file.read_text(encoding="utf-8")
    assert "WEVERSE_REFRESH_TOKEN=r2" in content
    assert "WEVERSE_REFRESH_TOKEN=r1" not in content


# --- happy path ---------------------------------------------------------------


def test_artist_name_matches_joined_community():
    getter_pages = notices_block([make_notice(1, "Hello", "body", RECENT_MS)])
    client = WeverseGatewayClient(
        getter=fake_getter(getter_pages),
        poster=no_refresh,
        access_token="t",
        refresh_token="r",
        env_path="nonexistent.env",
    )
    result = WeverseNoticeService(client=client).search_notices("YOASOBI")
    assert result["ok"] is True
    assert result["artist"] == "YOASOBI"
    assert result["community_id"] == 206
    assert result["results"][0]["url"] == "https://weverse.io/yoasobi/notice/1"


def test_scan_uses_single_request_with_scan_limit():
    client = WeverseGatewayClient(
        getter=fake_getter(notices_block([make_notice(1, "n", "b", RECENT_MS)])),
        poster=no_refresh,
        access_token="t",
        refresh_token="r",
        env_path="x",
    )
    WeverseNoticeService(client=client).search_notices("yoasobi")
    page_calls = [c for c in client._getter.calls if NOTICES_PATH in c["url"]]
    assert len(page_calls) == 1
    fields = page_calls[0]["params"]["fields"]
    assert f"limit({weverse_notice_module.NOTICE_SCAN_LIMIT})" in fields


def test_scan_result_cap_inside_window_sets_truncated(monkeypatch):
    monkeypatch.setattr(weverse_notice_module, "NOTICE_SCAN_LIMIT", 5)
    items = [make_notice(i, f"n{i}", "b", RECENT_MS - i * 86400_000) for i in range(5)]
    client = WeverseGatewayClient(
        getter=fake_getter(notices_block(items)),
        poster=no_refresh,
        access_token="t",
        refresh_token="r",
        env_path="x",
    )
    result = WeverseNoticeService(client=client).search_notices("yoasobi")
    # cap hit and the oldest returned notice is still inside the window:
    # in-window notices may exist upstream, so the scan is honestly truncated
    assert result["ok"] is True
    assert result["truncated"] is True


def test_scan_cap_outside_window_not_truncated(monkeypatch):
    monkeypatch.setattr(weverse_notice_module, "NOTICE_SCAN_LIMIT", 5)
    # 4 in-window + 1 far out-of-window: the window is fully covered
    items = [make_notice(i, f"n{i}", "b", RECENT_MS - i * 86400_000) for i in range(4)]
    items.append(make_notice(99, "old", "b", OLD_MS))
    client = WeverseGatewayClient(
        getter=fake_getter(notices_block(items)),
        poster=no_refresh,
        access_token="t",
        refresh_token="r",
        env_path="x",
    )
    result = WeverseNoticeService(client=client).search_notices("yoasobi")
    assert result["ok"] is True
    assert result["truncated"] is False
    assert result["scanned_count"] == 5


def test_notice_window_filters_old_notices():
    items = [
        make_notice(1, "new", "b", RECENT_MS),
        make_notice(2, "old", "b", OLD_MS),
        make_notice(3, "older", "b", OLD_MS - 86400_000),
    ]
    client = WeverseGatewayClient(
        getter=fake_getter(notices_block(items)),
        poster=no_refresh,
        access_token="t",
        refresh_token="r",
        env_path="x",
    )
    result = WeverseNoticeService(client=client).search_notices("yoasobi")
    assert [r["notice_id"] for r in result["results"]] == ["1"]
    assert result["scanned_count"] == 3


def test_notice_results_are_deduplicated():
    items = [
        make_notice(7, "a", "b", RECENT_MS),
        make_notice(7, "a", "b", RECENT_MS),
        make_notice(8, "c", "d", OLDER_MS),
    ]
    client = WeverseGatewayClient(
        getter=fake_getter(notices_block(items)), poster=no_refresh, access_token="t", refresh_token="r", env_path="x"
    )
    result = WeverseNoticeService(client=client).search_notices("yoasobi")
    assert result["ok"] is True
    assert len(result["results"]) == 2
    assert result["scanned_count"] == 2


def test_notice_results_are_sorted_descending():
    items = [
        make_notice(1, "oldest", "b", OLDER_MS),
        make_notice(2, "newest", "b", RECENT_MS),
        make_notice(3, "middle", "b", OLDER_MS + 5 * 86400_000),
    ]
    client = WeverseGatewayClient(
        getter=fake_getter(notices_block(items)), poster=no_refresh, access_token="t", refresh_token="r", env_path="x"
    )
    result = WeverseNoticeService(client=client).search_notices("yoasobi")
    assert [r["notice_id"] for r in result["results"]] == ["2", "3", "1"]


# --- normalization & filtering -------------------------------------------------


def test_html_content_is_converted_to_plain_text():
    body = '<w:color value="color01"><w:b>Bold news</w:b></w:color>\nplain line'
    assert "Bold news" in html_to_text(body) and "plain line" in html_to_text(body)
    assert "<w:b>" not in html_to_text(body)


def test_images_and_scripts_are_removed():
    text = html_to_text('<img src="https://x/y.png"/>keep<script>evil()</script>text')
    assert "img" not in text and "evil()" not in text and "keep" in text and "text" in text


def test_query_matches_title_and_body():
    items = [
        make_notice(1, "concert in SEOUL", "nothing here", RECENT_MS),
        make_notice(2, "update", "pop-up store opens", RECENT_MS),
        make_notice(3, "unrelated", "nothing", RECENT_MS),
    ]
    client = WeverseGatewayClient(
        getter=fake_getter(notices_block(items)), poster=no_refresh, access_token="t", refresh_token="r", env_path="x"
    )
    service = WeverseNoticeService(client=client)
    assert [r["notice_id"] for r in service.search_notices("yoasobi", query="pop-up")["results"]] == ["2"]
    assert [r["notice_id"] for r in service.search_notices("yoasobi", query="seoul")["results"]] == ["1"]


def test_default_limit_returns_the_whole_window():
    """The model classifies events itself, so it needs the feed, not a page."""
    items = [make_notice(i, f"n{i}", "b", RECENT_MS - i * 3600_000) for i in range(15)]
    client = WeverseGatewayClient(
        getter=fake_getter(notices_block(items)), poster=no_refresh, access_token="t", refresh_token="r", env_path="x"
    )
    result = WeverseNoticeService(client=client).search_notices("yoasobi")
    assert result["matched_count"] == 15
    assert len(result["results"]) == 15
    assert DEFAULT_NOTICE_LIMIT >= 15, "the default must not silently drop notices"


# --- signature & registry -------------------------------------------------------


def test_signed_request_includes_signature_params():
    request = sign_request("/x/y", {"appId": "app"})
    assert request["params"]["wmsgpad"].isdigit()
    assert len(base64.b64decode(request["params"]["wmd"])) == 20  # SHA-1 digest length
    # wire order equals signed order: base params sorted, wmd/wmsgpad appended
    keys = list(request["params"].keys())
    assert keys[-2:] == ["wmd", "wmsgpad"]
    assert keys[:-2] == sorted(keys[:-2])


def test_gateway_internals_are_not_registered_as_tools():
    """Only weverse_notices faces the model; the client and service do not."""
    assert "weverse_notices" in TOOL_MAP
    for internal in ("WeverseGatewayClient", "WeverseNoticeService", "search_notices", "read_notice"):
        assert internal not in TOOL_MAP


# --- progressive disclosure: read one notice in full -----------------------------


def test_read_notice_returns_the_full_text():
    body = "<p>Hello.</p><img src='x.png'><script>track()</script><p>Application opens 6 May, 10:00 KST.</p>"
    items = [make_notice(11, "Presale", body, RECENT_MS), make_notice(12, "Other", "<p>b</p>", RECENT_MS)]
    result = make_service(notices_block(items)).read_notice("yoasobi", "11")

    assert result["ok"] is True
    assert result["notice_id"] == "11" and result["title"] == "Presale"
    assert "Application opens 6 May, 10:00 KST." in result["text"]
    assert "<" not in result["text"] and "track" not in result["text"]
    assert result["url"] == "https://weverse.io/yoasobi/notice/11"
    assert result["truncated"] is False and result["text_chars"] == len(result["text"])


def test_read_notice_truncates_but_reports_the_real_length():
    long_body = "sentence after sentence. " * 200
    service = make_service(notices_block([make_notice(21, "T", long_body, RECENT_MS)]))

    result = service.read_notice("yoasobi", "21", max_chars=500)
    assert len(result["text"]) == 500
    assert result["truncated"] is True
    assert result["text_chars"] > 500

    capped = service.read_notice("yoasobi", "21", max_chars=999_999)
    assert len(capped["text"]) <= MAX_READ_CHARS


def test_read_notice_default_budget_applies_when_max_chars_is_missing():
    long_body = "word " * 2000
    service = make_service(notices_block([make_notice(22, "T", long_body, RECENT_MS)]))

    assert len(service.read_notice("yoasobi", "22", max_chars=None)["text"]) == DEFAULT_READ_CHARS


def test_read_notice_unknown_id_is_a_structured_error():
    service = make_service(notices_block([make_notice(31, "T", "b", RECENT_MS)]))
    result = service.read_notice("yoasobi", "999")

    assert result["ok"] is False and result["error"] == "notice_not_found"
    assert "do not retry" in result["message"]


def test_read_notice_requires_a_notice_id():
    result = make_service(notices_block([])).read_notice("yoasobi", "  ")
    assert result["ok"] is False and result["error"] == "notice_not_found"
