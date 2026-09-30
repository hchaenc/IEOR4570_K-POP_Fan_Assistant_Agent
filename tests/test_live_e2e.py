"""Live tests: the real tools against the real Weverse gateway and Ticketmaster.

    python -m pytest -m "live_weverse or live_ticketmaster" -q -s

These need a configured .env and are deselected by default. They cover what the
mocked tests cannot: that the upstream schemas still match, and that the split
architecture really lets the model compose two sources in one answer.
"""

import json
import os

import pytest

import app
from tools import run_tool


def _require_weverse():
    if not (os.environ.get("WEVERSE_ACCESS_TOKEN") or os.environ.get("WEVERSE_REFRESH_TOKEN")):
        pytest.skip("Weverse tokens not configured")


def _require_ticketmaster():
    if not os.environ.get("TICKETMASTER_API_KEY"):
        pytest.skip("TICKETMASTER_API_KEY not configured")


def search(artist, **kwargs):
    result = json.loads(run_tool("search_weverse_notices", {"artist": artist, **kwargs}))
    assert result["ok"] is True, result
    return result


class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self):
        dumped = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            dumped["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                for call in self.tool_calls
            ]
        return dumped


class _FakeResponse:
    def __init__(self, message):
        self.choices = [type("Choice", (), {"message": message})()]


def scripted_llm(script):
    """A stub model that replays (content, tool_calls) turns and then answers."""
    turns = iter(script)

    def completion(**kwargs):
        turn = next(turns, None)
        if turn is None:
            return _FakeResponse(_FakeMessage(content="Done."))
        content, calls = turn
        return _FakeResponse(
            _FakeMessage(
                content=content,
                # Providers hand back arguments as a JSON string, and the
                # harness parses it, so the stub must match that shape.
                tool_calls=[
                    _FakeToolCall(f"call_{i}", name, json.dumps(args)) for i, (name, args) in enumerate(calls)
                ],
            )
        )

    return completion


# --- single tools ---------------------------------------------------------------


@pytest.mark.live_weverse
def test_live_search_annotates_real_notices():
    _require_weverse()
    result = search("aespa")

    assert result["scanned_count"] > 0
    assert result["notices"], "expected at least one in-window notice"
    assert result["notice_count"] == len(result["notices"])
    assert all({"event_type", "matched_signal", "matched_field", "excerpt", "ticket_relevant"} <= set(n) for n in result["notices"])
    assert all("text" not in n for n in result["notices"]), "the list must stay a digest"
    assert any(n["event_type"] == "ticketed_event" for n in result["notices"])
    print("\nlive aespa notices:", result["notice_count"], "of", result["matched_count"], "in window")
    for notice in result["notices"][:4]:
        print("  -", notice["published_at"][:10], f"[{notice['event_type']}]", notice["title"][:52])


@pytest.mark.live_weverse
def test_live_read_one_notice_in_full():
    _require_weverse()
    notices = search("aespa")["notices"]
    result = json.loads(run_tool("read_weverse_notice", {"artist": "aespa", "notice_id": notices[0]["notice_id"]}))

    assert result["ok"] is True
    assert result["notice_id"] == notices[0]["notice_id"]
    assert result["text"] and "<" not in result["text"]
    assert result["text_chars"] >= len(result["text"])
    print(f"\nlive read: {result['title'][:48]} | {result['text_chars']} chars")


@pytest.mark.live_weverse
def test_live_community_without_a_notice_feed_is_structured():
    _require_weverse()
    result = json.loads(run_tool("search_weverse_notices", {"artist": "MONSTA X"}))
    assert result["ok"] is False
    assert result["error"] == "community_not_joined"
    assert "no notice feed" in result["message"]


@pytest.mark.live_weverse
def test_live_unknown_artist_is_structured():
    _require_weverse()
    result = json.loads(run_tool("search_weverse_notices", {"artist": "definitely-not-a-weverse-community-xyz"}))
    assert result["ok"] is False
    assert result["error"] == "community_not_joined"


@pytest.mark.live_ticketmaster
def test_live_ticketmaster_only_returns_the_artist_own_events():
    _require_ticketmaster()
    result = json.loads(run_tool("search_ticketmaster_events", {"keyword": "aespa", "country_code": "US"}))
    assert result["ok"] is True
    assert result["matched_count"] > 0, "aespa is on sale in the US right now"
    for event in result["events"]:
        assert any("aespa" in name.casefold() for name in event["attractions"]), event["attractions"]
        assert event["venue"] and event["url"]
    print("\nlive ticketmaster:", result["matched_count"], "aespa events")
    for event in result["events"][:3]:
        print("  *", event["date"], event["name"][:46], "|", event["venue"])


# --- the model composing both sources -------------------------------------------


@pytest.mark.live_weverse
@pytest.mark.live_ticketmaster
def test_live_model_composes_notices_then_ticketmaster(monkeypatch):
    """The split architecture's whole point, against both live APIs.

    Notices first, then a Ticketmaster lookup for the ticketed ones, then a
    notice read - three calls the model decides on, none orchestrated in code.
    """
    _require_weverse()
    _require_ticketmaster()

    notices = search("aespa")
    ticketed = next(n for n in notices["notices"] if n["ticket_relevant"])

    completion = scripted_llm(
        [
            (None, [("search_weverse_notices", {"artist": "aespa"})]),
            (None, [("search_ticketmaster_events", {"keyword": "aespa", "country_code": "US"})]),
            (None, [("read_weverse_notice", {"artist": "aespa", "notice_id": ticketed["notice_id"]})]),
            ("Here is the plan.", []),
        ]
    )
    monkeypatch.setattr(app.litellm, "completion", completion)

    text, trace = app.run_agent(
        [
            {"role": "system", "content": app.SYSTEM_PROMPT},
            {"role": "user", "content": "Help me plan attending an aespa show in the US."},
        ]
    )

    assert text == "Here is the plan."
    assert [entry["name"] for entry in trace] == [
        "search_weverse_notices",
        "search_ticketmaster_events",
        "read_weverse_notice",
    ]
    assert all(entry["result"]["ok"] is True for entry in trace), trace
    # the collapsed header never carries bodies, the payload always does
    assert all("notices" not in entry["summary"] and "text" not in entry["summary"] for entry in trace)
    assert trace[0]["result"]["notices"] and trace[2]["result"]["text"]
    assert trace[1]["result"]["matched_count"] > 0
    print("\nlive orchestration ok:", [(e["name"], e["elapsed_ms"]) for e in trace])
