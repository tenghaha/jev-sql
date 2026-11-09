"""Step 2: per-table column scoring."""

from __future__ import annotations

import logging
from typing import Any

from .column_candidates import build_column_candidates
from .jev_client import call_jev
from .request_builder import DEFAULT_SCORE_SCALE, build_table_detail_request
from .stage_utils import chunk, merge_noul_answers, merge_usage

logger = logging.getLogger(__name__)


def _column_record(column_states: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": candidate_id,
            "column": candidate["column_name"],
            "dtype": candidate["dtype"],
        }
        for candidate_id, candidate in column_states.items()
    ]


def run_column_stage(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    selected_tables: list[dict[str, Any]],
    cascade_config: dict[str, Any],
    jev_config: dict[str, Any],
) -> dict[str, Any]:
    """Score columns per candidate table; one JEV request per table when possible."""
    table_map = {table["name"].casefold(): table for table in database["tables"]}
    score_scale = list(cascade_config.get("score_scale", DEFAULT_SCORE_SCALE))
    batch_size = int(cascade_config.get("table_column_batch", 0))

    answers: dict[str, Any] = {}
    column_states_all: dict[str, dict[str, Any]] = {}
    tables_record: dict[str, dict[str, Any]] = {}
    total_questions = 0
    total_elapsed = 0.0
    total_usage: dict[str, Any] = {}

    for item in selected_tables:
        table_id, table_name = item["table_id"], item["table"]
        table = table_map[table_name.casefold()]
        column_states = build_column_candidates(table_id, table)
        column_states_all.update(column_states)
        groups = chunk(list(column_states), batch_size)

        table_usage: dict[str, Any] = {}
        table_questions = 0
        table_elapsed = 0.0
        for batch_index, group in enumerate(groups, start=1):
            group_states = {candidate_id: column_states[candidate_id] for candidate_id in group}
            state, questions = build_table_detail_request(
                question,
                evidence,
                db_id,
                mentions,
                database,
                table_id,
                table,
                group_states,
                score_scale,
            )
            logger.info(
                "Stage 2 table %s batch %d/%d: %d spans x %d columns = %d questions",
                table_name,
                batch_index,
                len(groups),
                len(mentions),
                len(group_states),
                len(questions),
            )
            result = call_jev(state, questions, jev_config)
            merge_noul_answers(answers, result["answers"])
            table_questions += len(questions)
            table_elapsed += result["elapsed_seconds"]
            merge_usage(table_usage, result.get("raw", {}).get("usage"))

        total_questions += table_questions
        total_elapsed += table_elapsed
        merge_usage(total_usage, table_usage)
        tables_record[table_id] = {
            "table": table_name,
            "num_columns": len(column_states),
            "num_batches": len(groups),
            "num_questions": table_questions,
            "columns": _column_record(column_states),
            "elapsed_seconds": round(table_elapsed, 3),
            "usage": table_usage or None,
        }

    record = {
        "score_scale": score_scale,
        "score_threshold": float(cascade_config.get("column_score_threshold", 2.0)),
        "num_tables": len(selected_tables),
        "num_questions": total_questions,
        "tables": tables_record,
        "answers": answers,
        "elapsed_seconds": round(total_elapsed, 3),
        "usage": total_usage or None,
    }
    return {"record": record, "column_states": column_states_all, "answers": answers}
