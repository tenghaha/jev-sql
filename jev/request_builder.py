"""Build flattened candidate states and JEV questions for schema linking."""

from __future__ import annotations

import re
from typing import Any

NUMERIC_TYPES = {"integer", "real", "number", "float", "decimal", "double", "numeric"}
SCORE_SCALE = ["irrelevant", "weak", "relevant", "strong"]


def camel_split(name: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)


def tokenize(text: str) -> set[str]:
    """Lowercase tokens with a light plural stem for lexical matching."""
    tokens = set(re.findall(r"[a-z0-9]+", camel_split(text).lower()))
    return {token[:-1] if len(token) > 3 and token.endswith("s") else token for token in tokens}


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


def score_candidate(mention: dict[str, str], candidate: dict[str, Any]) -> float:
    """Lexical and type-based relevance used only to shortlist candidates."""
    mention_text = mention["text"]
    mention_lower = mention_text.lower()
    mention_tokens = tokenize(mention_text)
    column_tokens = tokenize(candidate["column_name"])
    table_tokens = tokenize(candidate["table_name"])

    score = float(len(mention_tokens & (column_tokens | table_tokens)))
    if candidate["column_name"].lower() in mention_lower:
        score += 5.0
    if candidate["table_name"].lower() in mention_lower:
        score += 3.0
    if column_tokens and column_tokens <= mention_tokens:
        score += 2.0

    sample = candidate.get("sample")
    if isinstance(sample, str) and len(sample) >= 2 and sample.lower() in mention_lower:
        score += 3.0

    span_type = mention.get("span_type", "")
    if span_type == "temporal_filter" and candidate["dtype"] == "date":
        score += 1.5
    if span_type == "numeric_filter" and candidate["dtype"] in NUMERIC_TYPES:
        score += 1.5
    if candidate["column_role_hint"] == "primary_key":
        score += 0.5
    elif candidate["column_role_hint"] == "foreign_key":
        score += 0.3
    return score


def rank_candidates(
    mention: dict[str, str],
    candidates: list[dict[str, Any]],
    max_candidates: int,
) -> list[dict[str, Any]]:
    ranked = sorted(
        enumerate(candidates),
        key=lambda item: (-score_candidate(mention, item[1]), item[0]),
    )
    if max_candidates > 0:
        ranked = ranked[:max_candidates]
    return [candidate for _, candidate in ranked]


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


def candidate_description(candidate: dict[str, Any]) -> str:
    return (
        f"{candidate['table_name']}.{candidate['column_name']} "
        f"({candidate['dtype']}, {candidate['column_role_hint']})"
    )


def build_questions(
    mentions: list[dict[str, str]],
    mention_refs: dict[str, list[str]],
    candidate_states: dict[str, dict[str, Any]],
    question_types: list[str],
    score_scale: list[str],
) -> dict[str, dict[str, Any]]:
    questions: dict[str, dict[str, Any]] = {}
    for mention in mentions:
        span_id = mention["span_id"]
        text = mention["text"]
        refs = mention_refs.get(span_id, [])
        if not refs:
            continue

        if "noul" in question_types:
            for candidate_id in refs:
                candidate = candidate_states[candidate_id]
                questions[f"{span_id}_noul_{candidate_id}"] = {
                    "type": "noul",
                    "instructions": (
                        f'Does the mention "{text}" from the question refer to the database '
                        f"column {candidate['table_name']}.{candidate['column_name']} "
                        f"({candidate['dtype']})? Use the column name, role, sample value, "
                        "and neighboring tables as evidence."
                    ),
                }

        if "score" in question_types:
            for candidate_id in refs:
                candidate = candidate_states[candidate_id]
                questions[f"{span_id}_score_{candidate_id}"] = {
                    "type": "score",
                    "instructions": (
                        f'How strongly does the database column {candidate["table_name"]}.'
                        f'{candidate["column_name"]} match the mention "{text}"?'
                    ),
                    "criteria": list(score_scale),
                }

        if "choice" in question_types and len(refs) >= 2:
            options = list(refs) + ["none"]
            criteria = {
                candidate_id: candidate_description(candidate_states[candidate_id])
                for candidate_id in refs
            }
            criteria["none"] = "None of the candidate columns matches the mention."
            questions[f"{span_id}_choice"] = {
                "type": "choice",
                "instructions": f'Which candidate column best matches the mention "{text}"?',
                "options": options,
                "criteria": criteria,
            }
    return questions


def build_request(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return the shared JEV state and its typed questions."""
    flat_candidates = flatten_candidates(database)
    max_candidates = int(config.get("max_candidates", 12))
    question_types = list(config.get("question_types", ["noul", "choice", "score"]))
    invalid = set(question_types) - {"noul", "choice", "score"}
    if invalid:
        raise ValueError(f"Unsupported question types: {sorted(invalid)}")
    score_scale = list(config.get("score_scale", SCORE_SCALE))

    candidate_states: dict[str, dict[str, Any]] = {}
    candidate_ids: dict[tuple[str, str], str] = {}
    mention_refs: dict[str, list[str]] = {}

    for mention in mentions:
        refs = []
        for candidate in rank_candidates(mention, flat_candidates, max_candidates):
            key = (candidate["table_name"], candidate["column_name"])
            if key not in candidate_ids:
                candidate_id = f"c{len(candidate_ids) + 1}"
                candidate_ids[key] = candidate_id
                candidate_states[candidate_id] = candidate
            refs.append(candidate_ids[key])
        mention_refs[mention["span_id"]] = refs

    state = {
        "task": "schema_linking",
        "instruction": (
            "Answer each question using only the shared question, evidence spans, schema "
            "summary, and candidate states. Do not generate free-form text."
        ),
        "context": {
            "database": db_id,
            "question": question,
            "evidence": evidence,
            "evidence_spans": mentions,
            "schema_summary": schema_summary(database),
        },
        "candidates": candidate_states,
        "output_schema": {
            "noul": {"noul": "probability in [0, 1]"},
            "choice": {
                "choice": "one option label",
                "confidence": "probability in [0, 1]",
            },
            "score": {
                "score": "expected level index",
                "confidence": "probability in [0, 1]",
            },
        },
    }
    questions = build_questions(
        mentions,
        mention_refs,
        candidate_states,
        question_types,
        score_scale,
    )
    return state, questions
