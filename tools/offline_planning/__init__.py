"""Offline-planning tool package.

Exposes the package-level contract consumed by the tools registry:
  SCHEMAS  - OpenAI-style tool schema list (what the model sees)
  HANDLERS - {tool_name: callable(args...) -> json str} (what the harness runs)
"""

from .tool import HANDLER, SCHEMA

SCHEMAS = [SCHEMA]
HANDLERS = {"plan_offline_attendance": HANDLER}
