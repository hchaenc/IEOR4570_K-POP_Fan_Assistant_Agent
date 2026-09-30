"""Original tools: one model-callable tool per upstream data slice.

Each subpackage here is a member's primary tool and registers itself via
SCHEMAS + HANDLERS. Original tools return raw (annotated) data and contain
no cross-source orchestration - composition lives in the model's tool loop
and in tools/common.
"""
