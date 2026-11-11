"""Shared JEV answer parsing and compact schema rendering.

Stage-specific candidate logic lives in column_candidates.py and fk_candidates.py.
"""

from __future__ import annotations

from typing import Any

NUMERIC_TYPES = {"integer", "real", "number", "float", "decimal", "double", "numeric"}


def schema_summary(database: dict[str, Any]) -> str:
    """Compact one-line schema of all tables, used as shared context."""
    tables = []
    for table in database["tables"]:
        primary_keys = set(table["primary_key"])
        columns = []
        for column in table["columns"]:
            marker = " PK" if column["name"] in primary_keys else ""
            columns.append(f"{column['name']} {column['type']}{marker}")
        tables.append(f"{table['name']}({', '.join(columns)})")
    return "; ".join(tables)


def noul_probability(answer: Any) -> float:
    """Read the positive probability from a Noul answer."""
    if not isinstance(answer, dict):
        return 0.0
    for key in ("noul", "yes"):
        value = answer.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def score_level(answer: Any) -> float:
    """Read the expected level from a Score answer."""
    if not isinstance(answer, dict):
        return 0.0
    for key in ("score", "expected_level"):
        value = answer.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def answer_confidence(answer: Any) -> float:
    if not isinstance(answer, dict):
        return 0.0
    value = answer.get("confidence")
    return float(value) if isinstance(value, (int, float)) else 0.0
