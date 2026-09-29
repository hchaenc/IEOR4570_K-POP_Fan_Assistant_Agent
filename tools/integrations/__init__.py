"""Data-source adapters shared by tools.

Subpackages here expose plain functions/classes and never define SCHEMAS or
HANDLERS, so the tools registry skips them: only tools/<tool_name>/ folders
with SCHEMAS + HANDLERS are registered as model-callable tools.
"""
