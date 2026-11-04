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


def collect_column_scores(
    mentions: list[dict[str, str]],
    column_states: dict[str, dict[str, Any]],
    answers: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Collect per-span Scores and the best Score for every scored column."""
    results = {}
    for candidate_id, candidate in column_states.items():
        spans = []
        for mention in mentions:
            answer = answers.get(column_question_id(mention["span_id"], candidate_id))
            spans.append(
                {
                    "span_id": mention["span_id"],
                    "text": mention.get("text", ""),
                    "span_type": mention.get("span_type", "other"),
                    "score": round(score_level(answer), 4),
                    "confidence": round(answer_confidence(answer), 4),
                }
            )
        best = max(spans, key=lambda item: item["score"]) if spans else {"score": 0.0}
        results[candidate_id] = {
            "candidate_id": candidate_id,
            "table": candidate["table_name"],
            "column": candidate["column_name"],
            "dtype": candidate["dtype"],
            "best_score": round(float(best["score"]), 4),
            "spans": spans,
        }
    return results
