"""Column candidate indexing, cascade selection, and final filtering."""

from __future__ import annotations

from typing import Any

NUMERIC_TYPES = {"integer", "real", "number", "float", "decimal", "double", "numeric"}


def stage1_question_id(candidate_id: str) -> str:
    return f"s1_{candidate_id}"


def stage2_question_id(span_id: str, candidate_id: str) -> str:
    return f"s2_{span_id}_{candidate_id}"


def table_neighbors(table: dict[str, Any]) -> list[dict[str, str]]:
    neighbors = []
    for adjacent in table["adjacent_tables"]:
        for child, parent in adjacent["foreign_keys"]:
            direction = "outgoing" if child.startswith(f"{table['name']}.") else "incoming"
            neighbors.append(
                {
                    "table": adjacent["table"],
                    "via": f"{child} -> {parent}",
                    "direction": direction,
                }
            )
    return neighbors


def flatten_candidates(database: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten every column into a candidate with its one-hop table context."""
    foreign_key_columns = set()
    for table in database["tables"]:
        for adjacent in table["adjacent_tables"]:
            for child, _ in adjacent["foreign_keys"]:
                foreign_key_columns.add(child)

    candidates = []
    for table in database["tables"]:
        primary_keys = set(table["primary_key"])
        neighbors = table_neighbors(table)
        for column in table["columns"]:
            full_name = f"{table['name']}.{column['name']}"
            if column["name"] in primary_keys:
                role = "primary_key"
            elif full_name in foreign_key_columns:
                role = "foreign_key"
            elif column["type"] == "date":
                role = "temporal"
            elif column["type"] in NUMERIC_TYPES:
                role = "measure"
            else:
                role = "attribute"
            candidates.append(
                {
                    "column_name": column["name"],
                    "table_name": table["name"],
                    "dtype": column["type"],
                    "sample": column.get("sample"),
                    "date_format": column.get("date_format"),
                    "table_neighbors": neighbors,
                    "column_role_hint": role,
                }
            )
    return candidates


def assign_candidate_ids(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Assign stable c1..cN ids in schema order."""
    return {f"c{index}": candidate for index, candidate in enumerate(candidates, start=1)}


def schema_summary(database: dict[str, Any]) -> str:
    """Compact one-line schema used as shared context."""
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


def select_stage1_candidates(
    candidate_states: dict[str, dict[str, Any]],
    answers: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    """Rank all columns by their single Noul score and keep the top `limit`."""
    rows = []
    for candidate_id, candidate in candidate_states.items():
        noul = noul_probability(answers.get(stage1_question_id(candidate_id)))
        rows.append(
            {
                "candidate_id": candidate_id,
                "table": candidate["table_name"],
                "column": candidate["column_name"],
                "dtype": candidate["dtype"],
                "noul": round(noul, 4),
            }
        )

    rows.sort(key=lambda row: (-row["noul"], int(row["candidate_id"][1:])))
    return rows[:limit] if limit > 0 else rows


def filter_by_score(
    mentions: list[dict[str, str]],
    selected_candidates: list[dict[str, Any]],
    candidate_states: dict[str, dict[str, Any]],
    answers: dict[str, Any],
    threshold: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep mention-candidate pairs whose Score reaches the threshold."""
    linked_columns = []
    union: dict[tuple[str, str], dict[str, Any]] = {}

    for mention in mentions:
        columns = []
        for selected in selected_candidates:
            candidate_id = selected["candidate_id"]
            answer = answers.get(stage2_question_id(mention["span_id"], candidate_id))
            score = score_level(answer)
            if score < threshold:
                continue
            candidate = candidate_states[candidate_id]
            columns.append(
                {
                    "candidate_id": candidate_id,
                    "table": candidate["table_name"],
                    "column": candidate["column_name"],
                    "dtype": candidate["dtype"],
                    "score": round(score, 4),
                    "confidence": round(answer_confidence(answer), 4),
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
                }
            else:
                union[key]["best_score"] = max(union[key]["best_score"], round(score, 4))
                if mention["span_id"] not in union[key]["mentions"]:
                    union[key]["mentions"].append(mention["span_id"])

        columns.sort(key=lambda item: (-item["score"], item["candidate_id"]))
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
