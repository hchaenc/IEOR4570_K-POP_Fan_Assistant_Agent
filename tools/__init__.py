"""Tool registry: auto-discovers every tools/<name>/ subpackage.

Each tool lives in its own subpackage and exposes two package attributes:

  SCHEMAS:  list[dict]  - OpenAI-style function schemas shown to the model
  HANDLERS: dict[str, callable] - tool_name -> function returning a JSON string

Adding a tool never touches this file or anyone else's files: create
tools/<your_tool>/ with those two attributes and it is registered.
"""

import importlib
import json
import pkgutil

TOOLS: list[dict] = []
TOOL_MAP: dict[str, callable] = {}

for _module_info in pkgutil.iter_modules(__path__):
    if not _module_info.ispkg:
        continue
    _module = importlib.import_module(f"{__name__}.{_module_info.name}")
    TOOLS.extend(getattr(_module, "SCHEMAS", []))
    TOOL_MAP.update(getattr(_module, "HANDLERS", {}))


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return json.dumps(
            {"ok": False, "error": "unknown_tool", "message": f"Unknown tool '{name}'. Available: {sorted(TOOL_MAP)}"}
        )
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return json.dumps({"ok": False, "error": "bad_arguments", "message": f"Bad arguments for {name}: {e}"})
