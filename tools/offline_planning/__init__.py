"""Offline-planning tool package.

Exposes the package-level contract consumed by the tools registry:
  SCHEMAS  - OpenAI-style tool schema list (what the model sees)
  HANDLERS - {tool_name: callable(args...) -> json str} (what the harness runs)
"""

from .read_tool import HANDLER as READ_HANDLER
from .read_tool import READ_SCHEMA
from .tool import HANDLER, SCHEMA

SCHEMAS = [SCHEMA, READ_SCHEMA]
HANDLERS = {
    "plan_offline_attendance": HANDLER,
    "read_weverse_notice": READ_HANDLER,
}
