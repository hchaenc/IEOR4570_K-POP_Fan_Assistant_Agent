"""read_weverse_notice: on-demand full text for one notice.

Progressive disclosure, the shape the course slides recommend for search-style
tools ("return titles + summaries to start... combine with a 'read page' tool
for deeper dives"). `plan_offline_attendance` returns an excerpt per notice;
this tool fetches the whole body when a question needs detail that does not
fit in 180 characters - exact application windows, membership requirements,
sale channels.
"""

import json

from tools.integrations.weverse.notices import (
    DEFAULT_READ_CHARS,
    MAX_READ_CHARS,
    get_notice_service,
)


def read_weverse_notice_tool(
    artist: str,
    notice_id: str,
    max_chars: int | None = DEFAULT_READ_CHARS,
) -> str:
    """Fetch one notice's full text by the id the previous call reported."""
    result = get_notice_service().read_notice(
        artist=(artist.strip() if isinstance(artist, str) else ""),
        notice_id=(str(notice_id).strip() if notice_id is not None else ""),
        max_chars=max_chars,
    )
    return json.dumps(result, ensure_ascii=False)


READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_weverse_notice",
        "description": (
            "Read the full text of one official Weverse notice. Use it after "
            "plan_offline_attendance whenever the answer needs detail the "
            "excerpt cannot hold: exact dates and times, venue, sale channel, "
            "membership or application requirements, or a quotation. Pass the "
            "artist and the notice_id exactly as they appeared in that call's "
            "notices[] list. Read only the notices you need - at most three "
            "per answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artist": {
                    "type": "string",
                    "description": (
                        "The same artist value used in the plan_offline_attendance "
                        "call, for example 'aespa'."
                    ),
                },
                "notice_id": {
                    "type": "string",
                    "description": (
                        "notice_id copied verbatim from notices[]. Do not invent "
                        "or modify one."
                    ),
                },
                "max_chars": {
                    "type": ["integer", "null"],
                    "description": (
                        f"Optional character budget for the returned text. "
                        f"Defaults to {DEFAULT_READ_CHARS} and cannot exceed "
                        f"{MAX_READ_CHARS}; the response reports the real length "
                        "in text_chars."
                    ),
                },
            },
            "required": ["artist", "notice_id"],
        },
    },
}

HANDLER = read_weverse_notice_tool
