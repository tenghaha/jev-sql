"""Build JEV states and questions for the JEV cascade."""

from __future__ import annotations

from typing import Any

from .candidates import schema_summary
from .column_candidates import column_question_id
from .fk_candidates import choice_question_id

DEFAULT_SCORE_SCALE = ["irrelevant", "weak", "relevant", "strong"]


def _context(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
) -> dict[str, Any]:
    return {
        "database": db_id,
        "question": question,
        "evidence": evidence,
        "evidence_spans": mentions,
        "schema_summary": schema_summary(database),
    }


def ambiguous_question_id(candidate_id: str) -> str:
    return f"s3_{candidate_id}"


def build_ambiguous_request(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    selected_columns: list[dict[str, Any]],
    ambiguous_states: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build one Noul question per ambiguous column, given the already selected columns."""
    questions = {}
    for candidate_id, candidate in ambiguous_states.items():
        questions[ambiguous_question_id(candidate_id)] = {
            "type": "noul",
            "instructions": (
                f"The column {candidate['table_name']}.{candidate['column_name']} is only weakly "
                "related to the question. Is it necessary to add it to the already selected column "
                f"set in order to answer the question: {question}"
            ),
        }

    state = {
        "task": "ambiguous_column_completion",
        "instruction": (
            "The selected columns are already a candidate column set for the question. For each "
            "ambiguous column, decide whether the question cannot be answered without adding it to "
            "that set. Answer only the provided questions."
        ),
        "context": _context(question, evidence, db_id, mentions, database),
        "selected_columns": selected_columns,
        "candidates": ambiguous_states,
        "output_schema": {"noul": "probability in [0, 1]"},
    }
    return state, questions


def build_result_join_request(
    db_id: str,
    database: dict[str, Any],
    pairs: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build two choice questions per result-table pair, one per direction."""
    table_map = {table["name"].casefold(): table for table in database["tables"]}
    pair_states = {}
    questions = {}
    for pair in pairs:
        left = table_map[pair["left_table"].casefold()]
        right = table_map[pair["right_table"].casefold()]
        pair_states[pair["pair_id"]] = {
            "left_table": _table_schema(left),
            "right_table": _table_schema(right),
        }
        for side, table, other in (
            ("a", left, right),
            ("b", right, left),
        ):
            questions[choice_question_id(pair["pair_id"], side)] = {
                "type": "choice",
                "instructions": (
                    f"In table {table['name']}, which column is most likely the foreign key "
                    f"column that joins it with table {other['name']}?"
                ),
                "criteria": {
                    column["name"]: _column_criterion(table, column)
                    for column in table["columns"]
                },
            }

    state = {
        "task": "result_table_join_key_choice",
        "instruction": (
            "Every pair below holds two result tables with their full column lists. For each pair, "
            "pick from the questioned table the single column that is most likely used to join it "
            "with the other table. Answer only the provided questions."
        ),
        "context": {"database": db_id},
        "pairs": pair_states,
        "output_schema": {"choice": "one column name from the questioned table"},
    }
    return state, questions


def _table_schema(table: dict[str, Any]) -> dict[str, Any]:
    return {
        "table": table["name"],
        "primary_key": list(table["primary_key"]),
        "columns": [
            {"name": column["name"], "type": column["type"]} for column in table["columns"]
        ],
    }


def _column_criterion(table: dict[str, Any], column: dict[str, Any]) -> str:
    """Describe one column as a choice criterion."""
    label = "primary key column" if column["name"] in table["primary_key"] else "column"
    text = f"{table['name']}.{column['name']}, a {column['type']} {label}"
    sample = column.get("sample")
    if sample is not None:
        text += f", values like {str(sample)[:40]}"
    return text


def build_table_detail_request(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    table_id: str,
    table: dict[str, Any],
    column_states: dict[str, dict[str, Any]],
    score_scale: list[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build column Score questions for one focus table."""
    questions = {}
    for mention in mentions:
        for candidate_id, candidate in column_states.items():
            questions[column_question_id(mention["span_id"], candidate_id)] = {
                "type": "score",
                "instructions": (
                    f'How strongly does the database column {candidate["table_name"]}.'
                    f'{candidate["column_name"]} match the evidence span "{mention["text"]}"?'
                ),
                "criteria": list(score_scale),
            }

    state = {
        "task": "table_column_linking",
        "instruction": (
            "Score how strongly each evidence span matches each candidate column of the focus "
            "table. The full database schema is provided as context. Answer only the provided "
            "questions."
        ),
        "context": _context(question, evidence, db_id, mentions, database),
        "focus_table": {
            "table_id": table_id,
            "table_name": table["name"],
            "primary_key": list(table["primary_key"]),
        },
        "candidates": column_states,
        "output_schema": {
            "score": "expected level index",
            "confidence": "probability in [0, 1]",
        },
    }
    return state, questions
