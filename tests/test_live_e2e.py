"""Live end-to-end tests for the offline-planning tool.

    python -m pytest -m "live_weverse or live_ticketmaster" -q -s

Exercises the real runtime path: run_tool -> tools/offline_planning ->
both live integrations -> merged envelope; plus the full agent chain with a
stubbed LLM. Read-only; prints notice titles and event names.
"""

import json
import os

import pytest

import app
from tools import run_tool


def _require_credentials():
    if not (
        os.environ.get("WEVERSE_ACCESS_TOKEN") or os.environ.get("WEVERSE_REFRESH_TOKEN")
    ):
        pytest.skip("Weverse tokens not configured")
    if not os.environ.get("TICKETMASTER_API_KEY"):
        pytest.skip("TICKETMASTER_API_KEY not configured")


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


@pytest.mark.live_weverse
def test_live_offline_planning_envelope_yoasobi():
    _require_credentials()
    result = json.loads(run_tool("plan_offline_attendance", {"artist": "yoasobi"}))
    assert result["ok"] is True, result
    assert result["scanned_count"] > 0
    assert result["notices"], "expected at least one in-window notice"
    # every notice carries its own classification evidence, inline
    assert all(
        {"event_type", "matched_signal", "matched_field", "excerpt", "ticket_relevant"} <= set(n)
        for n in result["notices"]
    )
    assert "decisions" not in result
    # trace-safe summary keys present for the UI whitelist
    assert {"ok", "scanned_count", "matched_count", "truncated"} <= set(result)
    print("\nlive yoasobi pipeline:")
    for notice in result["notices"][:4]:
        print("  -", notice["published_at"][:10], f"[{notice['event_type']}]", notice["title"][:52])
    print("  ticketmaster searched:", result["ticketmaster_searched"], "| events:", result["matched_count"])
    for event in result["ticketmaster_events"][:3]:
        print("  *", event["date"], event["name"][:48], "|", event.get("city"))


@pytest.mark.live_weverse
def test_live_read_notice_after_planning():
    """The full chain the two tools exist for: plan, then read one notice."""
    _require_credentials()
    plan = json.loads(run_tool("plan_offline_attendance", {"artist": "aespa"}))
    assert plan["ok"] is True
    target = plan["notices"][0]

    read = json.loads(
        run_tool("read_weverse_notice", {"artist": "aespa", "notice_id": target["notice_id"]})
    )
    assert read["ok"] is True, read
    assert read["notice_id"] == target["notice_id"]
    assert read["text"] and "<" not in read["text"]
    # the trace must not carry the body it just fetched
    assert app.safe_trace_result(json.dumps(read)) == {
        "ok": True,
        "truncated": read["truncated"],
        "notice_id": read["notice_id"],
        "text_chars": read["text_chars"],
    }
    print(f"\nlive read notice: {read['title'][:48]} | {read['text_chars']} chars, truncated={read['truncated']}")


@pytest.mark.live_weverse
def test_live_offline_planning_unknown_artist_is_structured():
    _require_credentials()
    result = json.loads(
        run_tool("plan_offline_attendance", {"artist": "definitely-not-a-weverse-community-xyz"})
    )
    assert result["ok"] is False
    assert result["error"] == "community_not_joined"
    assert result["source"] == "weverse"


@pytest.mark.live_weverse
def test_live_full_chain_single_tool_call(monkeypatch):
    """Stub LLM drives app.run_agent; the real single tool hits both live APIs."""
    _require_credentials()
    rounds = {"n": 0}

    def fake_completion(**kwargs):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return _FakeResponse(
                _FakeMessage(
                    tool_calls=[
                        _FakeToolCall(
                            "call_e2e_1",
                            "plan_offline_attendance",
                            json.dumps({"artist": "yoasobi"}),
                        )
                    ]
                )
            )
        return _FakeResponse(
            _FakeMessage(content="Here is the offline plan based on notices and Ticketmaster.")
        )

    monkeypatch.setattr(app.litellm, "completion", fake_completion)

    messages = [
        {"role": "system", "content": app.SYSTEM_PROMPT},
        {"role": "user", "content": "Does YOASOBI have any events or ticket sales coming up?"},
    ]
    text, trace = app.run_agent(messages)

    assert rounds["n"] == 2
    assert text
    assert len(trace) == 1, "the whole slice must be a single tool call"
    entry = trace[0]
    assert entry["name"] == "plan_offline_attendance"
    assert entry["args"] == {"artist": "yoasobi"}
    assert entry["result"]["ok"] is True
    assert entry["result"]["scanned_count"] > 0
    # trace stays summary-safe: no notice/event payloads leak to the UI
    assert "notices" not in entry["result"] and "ticketmaster_events" not in entry["result"]
    print("\nfull chain ok: one tool call -> live notices + ticketmaster; envelope summary:", entry["result"])
