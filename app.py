import argparse
import json
import logging
import os
import time
import uuid
from pathlib import Path

import litellm
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
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

# Which GCP project pays for the Vertex AI calls. Three sources, most explicit
# first, because the answer differs per person: gcloud bills the *quota
# project*, not the account that logged in, so a teammate who reuses this
# project's ID runs up the owner's bill.
GCP_PROJECT_FILE = "gcp-project.txt"


def _read_project_file() -> str | None:
    """A project ID in a plain text file, kept outside the repo the way
    password.txt and ticketmaster-api.txt already are."""
    here = Path(__file__).parent
    for candidate in (here / GCP_PROJECT_FILE, here.parent / GCP_PROJECT_FILE):
        if candidate.is_file():
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                return value.splitlines()[0].strip()
    return None


def resolve_vertex_project(cli_value: str | None = None) -> str | None:
    return (cli_value or os.environ.get("GOOGLE_CLOUD_PROJECT") or _read_project_file() or None)


# Only meaningful for the vertex_ai provider; ignored by LiteLLM elsewhere.
VERTEX_PROJECT = resolve_vertex_project()

SYSTEM_PROMPT = (
    "You are a K-pop fan assistant that helps fans attend real events: "
    "concerts, tours, fan meetings and pop-up stores.\n"
    "\n"
    "Tools (use these names exactly): weverse_notices reads an artist's "
    "official Weverse notices in two modes - without notice_id it returns the "
    "last 365 days as a digest where each notice carries an event_type "
    "(ticketed_event / popup / fan_event / merchandise / online_event / "
    "announcement), the word and field that produced that label, and a short "
    "evidence excerpt; with notice_id it returns that one notice in full. "
    "search_ticketmaster_events returns venues, public on-sale times, presale "
    "windows, ticket limits and purchase links. Ask one concise clarification "
    "question if the artist is missing, and never invent notice content, "
    "dates, availability, prices or source URLs.\n"
    "\n"
    "Combine them yourself, starting from the digest. Treat event_type as a "
    "heuristic pre-label and check it against the excerpt: when they disagree, "
    "trust the excerpt and say which notice you mean. Call weverse_notices "
    "again with a notice_id whenever the answer turns on detail the excerpt "
    "cannot hold - exact times, application windows, membership requirements, "
    "prices - copy the id verbatim, read at most three notices per answer, and "
    "never quote a notice you did not read. Call search_ticketmaster_events "
    "only for notices marked ticket_relevant, passing the artist name exactly "
    "as the user typed it. Pop-up stores, membership-application events and "
    "Korean or Japanese dates are usually not on Ticketmaster at all, so an "
    "empty result is an honest answer and the sale channel named in the notice "
    "is the better pointer.\n"
    "\n"
    "Prices and seats: Ticketmaster reports no live inventory and publishes "
    "price ranges for only some events, so price_min and price_max are "
    "usually null. Point at the event link instead of estimating, and state a "
    "price only when one appears in notice text you actually read.\n"
    "\n"
    "Report tool errors honestly (rate_limited, authentication_failed, "
    "community_not_joined, notice_not_found) with the corrective action the "
    "message suggests. Notice results cover 365 days only, and attribute "
    "every fact to its source URL.\n"
    "\n"
    "Do not claim to use YouTube, iTunes, LRCLIB, Booking.com, or any other "
    "not-yet-integrated service."
)
# The starter sized this for one composite tool call. Now the model composes
# sources itself, so a normal answer spends three rounds (notices ->
# Ticketmaster -> read one notice), and a mis-guessed tool name costs one more
# even though the error message lets the model recover from it.
MAX_TOOL_ROUNDS = 8

# --- The Harness ---

# Scalar summary keys only: `text` and `excerpt` exist precisely so the model
# can audit a label, and they must never reach the visible trace.
TRACE_ALLOWED_KEYS = (
    "ok",
    "error",
    "mode",
    "scanned_count",
    "matched_count",
    "notice_count",
    "truncated",
    "notice_id",
    "text_chars",
)


def safe_trace_result(result: str) -> dict:
    """Summarize a tool result for the collapsed trace header without notice bodies."""
    try:
        parsed = json.loads(result)
    except ValueError:
        return {"ok": None}
    if isinstance(parsed, dict):
        return {key: parsed[key] for key in TRACE_ALLOWED_KEYS if key in parsed}
    return {"ok": None}


def _as_payload(result: str):
    """Parse a tool result for the browser, keeping the raw string if it is not JSON."""
    try:
        return json.loads(result)
    except ValueError:
        return result


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
            started = time.monotonic()
            result = run_tool(call.function.name, args)
            tool_calls += [
                {
                    "name": call.function.name,
                    "args": args,
                    # `summary` is the collapsed header (safe scalars only);
                    # `result` is the full payload, shown expanded on demand so
                    # the grader can see exactly what the model was told.
                    "summary": safe_trace_result(result),
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "result": _as_payload(result),
                }
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


# --- Local tool test bench ---------------------------------------------------
# Not part of the deployed agent: an endpoint that runs tools on demand would
# let any visitor spend the Ticketmaster quota, so it stays behind a flag.

DEBUG_UI_ENABLED = os.environ.get("KPOP_DEBUG_UI", "").strip().lower() in {"1", "true", "yes"}


class ToolRunRequest(BaseModel):
    name: str
    args: dict = {}


def _require_bench() -> None:
    if not DEBUG_UI_ENABLED:
        raise HTTPException(status_code=404, detail="Tool bench is disabled: set KPOP_DEBUG_UI=1 to enable.")


@app.get("/bench")
def bench():
    _require_bench()
    return FileResponse(Path(__file__).parent / "bench.html")


@app.get("/bench/tools")
def bench_tools():
    """Hand the schemas to the page so it builds one form per tool automatically."""
    _require_bench()
    return {"tools": TOOLS}


@app.post("/bench/run")
def bench_run(request: ToolRunRequest):
    """Run one tool directly, bypassing the model, and return the full envelope."""
    _require_bench()
    started = time.monotonic()
    result = run_tool(request.name, request.args)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    try:
        payload = json.loads(result)
    except ValueError:
        payload = result
    return {
        "name": request.name,
        "args": request.args,
        "elapsed_ms": elapsed_ms,
        "chars": len(result),
        "result": payload,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="K-pop fan assistant web server")
    parser.add_argument(
        "--gcp-project",
        help=(
            f"GCP project ID that pays for Vertex AI calls. Overrides "
            f"GOOGLE_CLOUD_PROJECT and {GCP_PROJECT_FILE}."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


if __name__ == "__main__":
    _args = _parse_args()
    if _args.gcp_project:
        VERTEX_PROJECT = resolve_vertex_project(_args.gcp_project)
    # Say it out loud: gcloud bills the quota project, not whoever logged in.
    print(f"Vertex quota project: {VERTEX_PROJECT or 'unset (LiteLLM will fall back to ADC)'}")
    uvicorn.run(app, host=_args.host, port=_args.port)
