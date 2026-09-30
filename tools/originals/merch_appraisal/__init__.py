"""Merch appraisal: one team member's original tool.

`tool.py` exposes the registry contract (SCHEMA + HANDLER) and holds the
appraisal logic. The listings come from the shared `tools/common/ebay.py`
client; nothing here talks to eBay directly or orchestrates another source.
"""

from .tool import HANDLER, SCHEMA

SCHEMAS = [SCHEMA]
HANDLERS = {"appraise_kpop_merch": HANDLER}
