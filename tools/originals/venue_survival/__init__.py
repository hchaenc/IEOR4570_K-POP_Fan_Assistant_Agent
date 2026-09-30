"""Venue survival kit: one team member's original tool.

`tool.py` exposes the registry contract (SCHEMA + HANDLER) and holds the
ranking and queueing-tip logic; `osm.py` is its internal OpenStreetMap client
(Nominatim + Overpass, no key). Nothing here orchestrates another source.
"""

from .tool import HANDLER, SCHEMA

SCHEMAS = [SCHEMA]
HANDLERS = {"venue_survival_kit": HANDLER}
