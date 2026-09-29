import json
import logging
import os
import uuid
from pathlib import Path

import litellm
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tools import TOOLS, run_tool

# Load the local .env before anything reads configuration: LiteLLM needs
# GOOGLE_CLOUD_PROJECT, and gcloud's authorized_user ADC does not report a
# project on its own, so the Vertex call fails with "Could not resolve
# project_id" without this.
load_dotenv()

logger = logging.getLogger("kpop-assistant")

# --- Config ---

MODEL = os.environ.get("KPOP_ASSISTANT_MODEL", "vertex_ai/gemini-3.5-flash-lite")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "global")
# Only meaningful for the vertex_ai provider; ignored by LiteLLM elsewhere.
VERTEX_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT") or None

SYSTEM_PROMPT = (
    "You are a K-pop fan assistant focused on offline event planning.\n"
    "\n"
    "Call plan_offline_attendance for any question about an artist's "
    "concerts, tours, fan meetings, pop-up stores, official notices, or how "
    "to attend an event. Pass city and country_code whenever the question "
    "names a place. The tool fetches official Weverse notices, classifies "
    "them, and searches Ticketmaster for ticketed events in one call. Ask one "
    "concise clarification question if the artist is missing. Never invent "
    "notice content, dates, availability, or source URLs.\n"
    "\n"
    "Interpret the tool envelope: notices[] are official announcements, each "
    "labeled ticketed_event (seats to buy), popup, fan_event (attendance by "
    "membership application rather than purchase), merchandise, online_event "
    "(streamed, so nothing to travel to), or announcement. That label is a "
    "heuristic pre-label: matched_signal and matched_field say which word in "
    "which field produced it, and excerpt shows the text itself. When an "
    "excerpt contradicts its label, trust the excerpt, name the notice, and "
    "answer from the text. ticketmaster_events[] lists venues with "
    "attractions (the headliners), public_on_sale_at, presale_windows, "
    "ticket_limit and a purchase link.\n"
    "\n"
    "Call read_weverse_notice(artist, notice_id) instead of guessing whenever "
    "an answer turns on detail the excerpt cannot hold - exact dates and "
    "times, application windows, membership requirements, or a sale channel. "
    "Copy notice_id verbatim from notices[], read at most three notices per "
    "answer, and never quote the text of a notice you did not read.\n"
    "\n"
    "Prices: price_min and price_max are null for most K-pop events because "
    "Ticketmaster publishes price ranges for only some events. When they are "
    "null, point to the event link instead of stating or estimating a number; "
    "quote a price only if one appears in notice text you actually read. "
    "matched_count 0 means the event is genuinely not on Ticketmaster - say "
    "so honestly and prefer the sale channel named in the notice. A "
    "ticketmaster_error means Ticketmaster was unavailable while the notices "
    "stay valid, so answer from the notices and mention the gap. Notice "
    "results cover the last 365 days, and attribute every fact to its source "
    "URL.\n"
    "\n"
    "Do not claim to use YouTube, iTunes, LRCLIB, Booking.com, or any other "
    "not-yet-integrated service."
)
MAX_TOOL_ROUNDS = 5

# --- The Harness ---

# Scalar summary keys only: `text` and `excerpt` exist precisely so the model
# can audit a label, and they must never reach the visible trace.
TRACE_ALLOWED_KEYS = (
    "ok",
    "error",
    "scanned_count",
    "matched_count",
    "notice_matched",
    "truncated",
    "notice_id",
    "text_chars",
)


def safe_trace_result(result: str) -> dict:
    """Summarize a tool result for the visible trace without leaking notice bodies."""
    try:
        parsed = json.loads(result)
    except ValueError:
        return {"ok": None}
    if isinstance(parsed, dict):
        return {key: parsed[key] for key in TRACE_ALLOWED_KEYS if key in parsed}
    return {"ok": None}


def run_agent(messages: list[dict]) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    Returns the final text and a safe record of every tool call made along the way.
    """
    tool_calls = []

    for _ in range(MAX_TOOL_ROUNDS):
        reply = litellm.completion(
            model=MODEL,
            vertex_location=VERTEX_LOCATION,
            vertex_project=VERTEX_PROJECT,
            messages=messages,
            tools=TOOLS,
        ).choices[0].message

        # Append assistant's reply (text, tool calls, or both) to the context.
        # model_dump() keeps it a plain dict: the raw object carries provider-specific
        # fields that trip Pydantic when LiteLLM re-serializes it next round.
        messages += [reply.model_dump()]

        if not reply.tool_calls:
            return reply.content, tool_calls

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            args = json.loads(call.function.arguments)
            result = run_tool(call.function.name, args)
            tool_calls += [
                {"name": call.function.name, "args": args, "result": safe_trace_result(result)}
            ]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing.", tool_calls


# --- Session Store ---

# session_id -> list of messages. In-memory, single process.
sessions: dict[str, list] = {}

# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Get or create the session
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message}]

    try:
        response, tool_calls = run_agent(sessions[session_id])
    except Exception as e:
        # Auth, billing, a model that is not running: show a safe message, log a category.
        correlation_id = uuid.uuid4().hex[:8]
        logger.error("model_call_failed correlation_id=%s category=%s", correlation_id, type(e).__name__)
        response, tool_calls = (
            f"The model request failed. Check the server configuration and try again. (ref {correlation_id})",
            [],
        )

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
