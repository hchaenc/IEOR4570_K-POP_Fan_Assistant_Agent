"""Weverse notices: one team member's original tool.

`tool.py` exposes the registry contract (SCHEMAS + HANDLERS); `gateway.py` and
`notices.py` are its internals - a signed gateway client and the notice
service. Nothing here orchestrates another source.
"""

from .tool import HANDLERS, SCHEMAS
