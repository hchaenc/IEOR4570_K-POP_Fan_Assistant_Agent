"""Offline-planning tool: schema (what the model sees) + handler (what runs)."""

import json

from .pipeline import plan_offline_attendance


def plan_offline_attendance_tool(
    artist: str,
    city: str | None = None,
    country_code: str = "US",
    notice_query: str | None = None,
) -> str:
    """One call: notices + classification + conditional Ticketmaster search."""
    result = plan_offline_attendance(
        artist=(artist.strip() if isinstance(artist, str) else ""),
        city=(city.strip() if isinstance(city, str) and city.strip() else None),
        country_code=(country_code.strip().upper() if isinstance(country_code, str) and country_code.strip() else "US"),
        notice_query=(notice_query.strip() if isinstance(notice_query, str) and notice_query.strip() else None),
    )
    return json.dumps(result, ensure_ascii=False)


SCHEMA = {
    "type": "function",
    "function": {
        "name": "plan_offline_attendance",
        "description": (
            "Plan attending an artist's offline events. Fetches the artist's "
            "official Weverse notices (last 365 days), classifies each one "
            "(ticketed_event / popup / fan_event / merchandise / online_event / "
            "announcement), and searches Ticketmaster for the ticketed ones - "
            "returning notice summaries, per-notice classification, and matching "
            "Ticketmaster events with venue, public on-sale time, presale "
            "windows, ticket limit and purchase link. Use for questions about "
            "concerts, tours, fan meetings, pop-up stores and attending plans."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artist": {
                    "type": "string",
                    "description": (
                        "Artist or community name, for example 'yoasobi', 'MONSTA X' or 'ten'."
                    ),
                },
                "city": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional city to restrict the Ticketmaster search, e.g. 'New York'. "
                        "Pass it whenever the question names a place."
                    ),
                },
                "country_code": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional ISO country code for the Ticketmaster search, default 'US'. "
                        "Set it to the country the question is about."
                    ),
                },
                "notice_query": {
                    "type": ["string", "null"],
                    "description": "Optional keyword filter for Weverse notices, e.g. 'ticket'.",
                },
            },
            "required": ["artist"],
        },
    },
}

HANDLER = plan_offline_attendance_tool
