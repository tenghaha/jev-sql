"""AST JSON serialization helpers.

The dump format is a plain JSON object per statement with a ``type`` field on
every node.  ``None``-valued fields are dropped so that files stay compact and
the schema reads as "present iff meaningful".
"""

from __future__ import annotations

import json

__all__ = ["prune", "dumps", "dump"]


def prune(obj):
    """Recursively drop ``None`` values from dicts (keeps ``False``/``0``)."""
    if isinstance(obj, dict):
        return {k: prune(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [prune(v) for v in obj]
    return obj


def dumps(ast: dict, indent: int | None = None) -> str:
    return json.dumps(prune(ast), ensure_ascii=False, indent=indent,
                      separators=(",", ":") if indent is None else None)


def dump(ast: dict, path, indent: int | None = None) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dumps(ast, indent=indent))
        fh.write("\n")
