"""Build JEV states and questions for the table-first cascade."""

from __future__ import annotations

from typing import Any

from .candidates import schema_summary
from .column_candidates import column_question_id
from .fk_candidates import link_question_id
from .table_candidates import table_question_id

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


def build_table_request(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    candidate_states: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build one Noul question per table, with mentions in shared context."""
    questions = {}
    for table_id, candidate in candidate_states.items():
        questions[table_question_id(table_id)] = {
            "type": "noul",
            "instructions": (
                f"Is the database table {candidate['table_name']} needed to answer the question, "
                "given its evidence spans and column list?"
            ),
        }

    state = {
        "task": "table_linking",
        "instruction": (
            "Decide whether each candidate table is needed to answer the question. Each candidate "
            "is a database table with its primary key and columns. Use the evidence spans as the "
            "natural-language mentions. Answer only the provided questions."
        ),
        "context": _context(question, evidence, db_id, mentions, database),
        "candidates": candidate_states,
        "output_schema": {"noul": "probability in [0, 1]"},
    }
    return state, questions


def build_table_detail_request(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    table_id: str,
    table: dict[str, Any],
    column_states: dict[str, dict[str, Any]],
    link_states: dict[str, dict[str, Any]],
    score_scale: list[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build column Score questions and join Noul questions for one focus table."""
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
    for link_id, link in link_states.items():
        questions[link_question_id(link_id)] = {
            "type": "noul",
            "instructions": (
                f'Is the join {link["child_table"]}.{link["child_column"]} = '
                f'{link["parent_table"]}.{link["parent_column"]} needed to connect the focus table '
                f'{table["name"]} with {link["other_table"]} for this question?'
            ),
        }

    state = {
        "task": "table_column_linking",
        "instruction": (
            "Score how strongly each evidence span matches each candidate column of the focus "
            "table. Also decide whether each listed join candidate is needed to connect the focus "
            "table with another candidate table. The full database schema is provided as context. "
            "Answer only the provided questions."
        ),
        "context": _context(question, evidence, db_id, mentions, database),
        "focus_table": {
            "table_id": table_id,
            "table_name": table["name"],
            "primary_key": list(table["primary_key"]),
        },
        "candidates": column_states,
        "join_candidates": link_states,
        "output_schema": {
            "score": "expected level index",
            "noul": "probability in [0, 1]",
            "confidence": "probability in [0, 1]",
        },
    }
    return state, questions
