"""Step 1 candidates: table-level Noul screening."""

from __future__ import annotations

from typing import Any

from .candidates import noul_probability


def table_question_id(table_id: str) -> str:
    return f"s1_{table_id}"


def build_table_candidates(database: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Assign t1..tN ids in schema order; keep only compact table schema."""
    candidates = {}
    for index, table in enumerate(database["tables"], start=1):
        candidates[f"t{index}"] = {
            "table_name": table["name"],
            "primary_key": list(table["primary_key"]),
            "columns": [{"name": column["name"], "type": column["type"]} for column in table["columns"]],
        }
    return candidates


def table_index(table_id: str) -> int:
    return int(table_id[1:])


def select_tables(
    candidate_states: dict[str, dict[str, Any]],
    answers: dict[str, Any],
    limit: int,
    threshold: float = 0.0,
) -> list[dict[str, Any]]:
    """Keep tables whose Noul reaches the threshold, then the top `limit`."""
    rows = [
        {
            "table_id": table_id,
            "table": candidate["table_name"],
            "noul": round(noul_probability(answers.get(table_question_id(table_id))), 4),
        }
        for table_id, candidate in candidate_states.items()
    ]
    rows.sort(key=lambda row: (-row["noul"], table_index(row["table_id"])))
    if threshold > 0:
        rows = [row for row in rows if row["noul"] >= threshold]
    return rows[:limit] if limit > 0 else rows
