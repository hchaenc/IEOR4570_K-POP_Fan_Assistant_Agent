"""The harness loop with a stubbed model (no network, no Vertex)."""

import json

from fastapi.testclient import TestClient

import app


class _Function:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, call_id, name, args):
        self.id = call_id
        self.function = _Function(name, json.dumps(args))


class _Message:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self):
        dumped = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            dumped["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in self.tool_calls
            ]
        return dumped


def scripted_model(monkeypatch, *turns):
    """Replay assistant messages in order; record the messages each call saw."""
    replies = iter(turns)
    seen = []

    def completion(**kwargs):
        seen.append([dict(m) for m in kwargs["messages"]])
        return type("Response", (), {"choices": [type("Choice", (), {"message": next(replies)})()]})()

    monkeypatch.setattr(app.litellm, "completion", completion)
    return seen


def test_empty_reply_after_tool_results_is_retried(monkeypatch):
    """Real case: 'I want to sofi stadium to see BTS' ended in a turn with no text,
    ChatResponse rejected None, and the page showed a JSON parse error."""
    seen = scripted_model(
        monkeypatch,
        _Message(tool_calls=[_ToolCall("c1", "no_such_tool", {})]),
        _Message(content=None),
        _Message(content="Here is your plan."),
    )
    response, calls = app.run_agent([{"role": "user", "content": "hi"}])

    assert response == "Here is your plan."
    assert [c["name"] for c in calls] == ["no_such_tool"]
    assert all(m.get("content") is not None or m.get("tool_calls") or m["role"] != "assistant" for m in seen[2]), (
        "the empty assistant turn must not be sent back to the model"
    )


def test_repeated_empty_replies_end_with_a_readable_message(monkeypatch):
    scripted_model(monkeypatch, _Message(content=""), _Message(content=None))
    response, _ = app.run_agent([{"role": "user", "content": "hi"}])
    assert response == app.EMPTY_REPLY_FALLBACK


def test_chat_endpoint_never_returns_500_for_an_empty_reply(monkeypatch):
    scripted_model(monkeypatch, _Message(content=None), _Message(content=None))
    res = TestClient(app.app).post("/chat", json={"message": "I want to sofi stadium to see BTS"})
    assert res.status_code == 200
    assert res.json()["response"] == app.EMPTY_REPLY_FALLBACK


def test_song_release_result_can_feed_the_comeback_trail(monkeypatch):
    calls = []

    def run_tool(name, args):
        calls.append((name, args))
        if name == "resolve_song_release":
            return json.dumps(
                {
                    "ok": True,
                    "artist": "aespa",
                    "song": "Licorice",
                    "release_title": "Armageddon",
                }
            )
        assert name == "trace_kpop_comeback_era"
        return json.dumps({"ok": True, "matched_count": 4})

    monkeypatch.setattr(app, "run_tool", run_tool)
    seen = scripted_model(
        monkeypatch,
        _Message(
            tool_calls=[
                _ToolCall(
                    "resolve_1",
                    "resolve_song_release",
                    {"artist": "aespa", "song": "Licorice"},
                )
            ]
        ),
        _Message(
            tool_calls=[
                _ToolCall(
                    "trace_1",
                    "trace_kpop_comeback_era",
                    {
                        "artist": "aespa",
                        "release_title": "Armageddon",
                        "anchor_song": "Licorice",
                    },
                )
            ]
        ),
        _Message(content="Here is the comeback trail."),
    )

    response, trace = app.run_agent(
        [
            {"role": "system", "content": app.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Show me aespa's Licorice comeback era.",
            },
        ]
    )

    assert response == "Here is the comeback trail."
    assert [entry[0] for entry in calls] == [
        "resolve_song_release",
        "trace_kpop_comeback_era",
    ]
    assert calls[1][1] == {
        "artist": "aespa",
        "release_title": "Armageddon",
        "anchor_song": "Licorice",
    }
    assert json.loads(seen[1][-1]["content"])["release_title"] == "Armageddon"
    assert all(entry["result"]["ok"] is True for entry in trace)
