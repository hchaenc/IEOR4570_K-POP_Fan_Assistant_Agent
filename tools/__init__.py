"""Tool registry: auto-discovers tools from the two families.

  tools/originals/<name>/   one package per team member's primary tool
  tools/common/<name>.py    shared services and tools (Ticketmaster today;
                            YouTube, iTunes later)

A module or package is model-callable exactly when it exposes:

  SCHEMAS:  list[dict]  - OpenAI-style function schemas shown to the model
  HANDLERS: dict[str, callable] - tool_name -> function returning a JSON string

Helper modules (tools/common/classify.py) expose neither and stay internal.

Adding a tool never touches this file: create the package or module with those
two attributes and it is registered.
"""

import importlib
import json
import pkgutil
from pathlib import Path

FAMILIES = ("originals", "common")

TOOLS: list[dict] = []
TOOL_MAP: dict[str, callable] = {}

for _family in FAMILIES:
    _family_path = Path(__file__).parent / _family
    for _module_info in pkgutil.iter_modules([str(_family_path)]):
        _module = importlib.import_module(f"{__name__}.{_family}.{_module_info.name}")
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
