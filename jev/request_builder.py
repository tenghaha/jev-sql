"""Build JEV states and questions for each cascade stage."""

from __future__ import annotations

from typing import Any

from .candidates import schema_summary, stage1_question_id, stage2_question_id

DEFAULT_SCORE_SCALE = ["irrelevant", "weak", "relevant", "strong"]


def _build_state(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    candidate_states: dict[str, dict[str, Any]],
    task: str,
    instruction: str,
    output_schema: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task": task,
        "instruction": instruction,
        "context": {
            "database": db_id,
            "question": question,
            "evidence": evidence,
            "evidence_spans": mentions,
            "schema_summary": schema_summary(database),
        },
        "candidates": candidate_states,
        "output_schema": output_schema,
    }


def build_stage1_request(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    candidate_states: dict[str, dict[str, Any]],
    candidate_ids: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build one Noul question per given column, with mentions in shared context."""
    stage_candidates = (
        candidate_states
        if candidate_ids is None
        else {candidate_id: candidate_states[candidate_id] for candidate_id in candidate_ids}
    )
    questions = {}
    for candidate_id, candidate in stage_candidates.items():
        questions[stage1_question_id(candidate_id)] = {
            "type": "noul",
            "instructions": (
                f"Is the database column {candidate['table_name']}.{candidate['column_name']} "
                f"({candidate['dtype']}) relevant to the question, given its evidence spans? "
                "Use the column name, role, sample value, and neighboring tables as evidence."
            ),
        }

    state = _build_state(
        question,
        evidence,
        db_id,
        mentions,
        database,
        stage_candidates,
        task="schema_linking_stage1",
        instruction=(
            "Decide whether the question refers to each candidate column, using the evidence "
            "spans as its mentions. Each candidate is a column of the database. Answer only "
            "the provided questions."
        ),
        output_schema={"noul": "probability in [0, 1]"},
    )
    return state, questions


def build_stage2_request(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    candidate_states: dict[str, dict[str, Any]],
    selected_ids: list[str],
    score_scale: list[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build Score questions for every mention against the stage-1 candidates."""
    stage_candidates = {
        candidate_id: candidate_states[candidate_id] for candidate_id in selected_ids
    }
    questions = {}
    for mention in mentions:
        for candidate_id, candidate in stage_candidates.items():
            questions[stage2_question_id(mention["span_id"], candidate_id)] = {
                "type": "score",
                "instructions": (
                    f'How strongly does the database column {candidate["table_name"]}.'
                    f'{candidate["column_name"]} match the mention "{mention["text"]}"?'
                ),
                "criteria": list(score_scale),
            }

    state = _build_state(
        question,
        evidence,
        db_id,
        mentions,
        database,
        stage_candidates,
        task="schema_linking_stage2",
        instruction=(
            "Score how strongly each mention matches each candidate column. Only the listed "
            "candidates are under consideration. Answer only the provided questions."
        ),
        output_schema={
            "score": "expected level index",
            "confidence": "probability in [0, 1]",
        },
    )
    return state, questions
