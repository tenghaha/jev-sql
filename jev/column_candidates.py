"""Step 2 candidates: per-table column scoring and score filtering."""

from __future__ import annotations

from typing import Any

from .candidates import NUMERIC_TYPES, answer_confidence, score_level


def column_question_id(span_id: str, candidate_id: str) -> str:
    return f"s2_{span_id}_{candidate_id}"


def _column_role(column: dict[str, Any], primary_keys: set[str]) -> str:
    if column["name"] in primary_keys:
        return "primary_key"
    if column.get("date_format") or column["type"] == "date":
        return "temporal"
    if column["type"] in NUMERIC_TYPES:
        return "measure"
    return "attribute"


def build_column_candidates(
    table_id: str,
    table: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Assign <table_id>_c1..cM ids to the columns of one table."""
    primary_keys = set(table["primary_key"])
    candidates = {}
    for index, column in enumerate(table["columns"], start=1):
        candidates[f"{table_id}_c{index}"] = {
            "column_name": column["name"],
            "table_name": table["name"],
            "dtype": column["type"],
            "sample": column.get("sample"),
            "date_format": column.get("date_format"),
            "column_role_hint": _column_role(column, primary_keys),
        }
    return candidates


def _candidate_sort_key(candidate_id: str) -> tuple[int, int]:
    table_part, column_part = candidate_id.split("_", 1)
    return int(table_part[1:]), int(column_part[1:])


def filter_columns(
    mentions: list[dict[str, str]],
    column_states: dict[str, dict[str, Any]],
    answers: dict[str, Any],
    threshold: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep mention-column pairs whose Score reaches the threshold."""
    linked_columns = []
    union: dict[tuple[str, str], dict[str, Any]] = {}

    for mention in mentions:
        columns = []
        for candidate_id, candidate in column_states.items():
            answer = answers.get(column_question_id(mention["span_id"], candidate_id))
            score = score_level(answer)
            if score < threshold:
                continue
            columns.append(
                {
                    "candidate_id": candidate_id,
                    "table": candidate["table_name"],
                    "column": candidate["column_name"],
                    "dtype": candidate["dtype"],
                    "score": round(score, 4),
                    "confidence": round(answer_confidence(answer), 4),
                    "source": "score",
                }
            )

            key = (candidate["table_name"], candidate["column_name"])
            if key not in union:
                union[key] = {
                    "table": candidate["table_name"],
                    "column": candidate["column_name"],
                    "dtype": candidate["dtype"],
                    "best_score": round(score, 4),
                    "mentions": [mention["span_id"]],
                    "sources": ["score"],
                }
            else:
                union[key]["best_score"] = max(union[key]["best_score"], round(score, 4))
                if mention["span_id"] not in union[key]["mentions"]:
                    union[key]["mentions"].append(mention["span_id"])

        columns.sort(key=lambda item: (-item["score"], _candidate_sort_key(item["candidate_id"])))
        linked_columns.append(
            {
                "span_id": mention["span_id"],
                "text": mention["text"],
                "span_type": mention.get("span_type", "other"),
                "columns": columns,
            }
        )

    union_columns = sorted(
        union.values(),
        key=lambda item: (-item["best_score"], item["table"], item["column"]),
    )
    return linked_columns, union_columns
