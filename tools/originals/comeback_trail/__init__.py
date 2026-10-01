"""K-pop comeback trail: one team member's original tool.

`tool.py` exposes the registry contract and reconstructs a K-pop comeback era
from public YouTube video metadata provided by `tools/common/youtube.py`.
The YouTube adapter only fetches data; filtering, classification and comeback
sequencing live here.
"""

from .tool import HANDLER, SCHEMA

SCHEMAS = [SCHEMA]
HANDLERS = {"trace_kpop_comeback_era": HANDLER}