"""Step 3: add the join columns that connect the result tables."""

from __future__ import annotations

import logging
from typing import Any

from .fk_candidates import build_pairs, collect_chosen_columns, result_tables
from .jev_client import call_jev
from .request_builder import build_result_join_request
from .stage_utils import merge_usage

logger = logging.getLogger(__name__)


def empty_record() -> dict[str, Any]:
    return {
        "num_result_tables": 0,
        "num_pairs": 0,
        "num_questions": 0,
        "pairs": [],
        "answers": {},
        "chosen_columns": [],
        "elapsed_seconds": 0.0,
        "usage": None,
    }


def run_result_join_stage(
    db_id: str,
    database: dict[str, Any],
    selected_columns: list[dict[str, Any]],
    jev_config: dict[str, Any],
) -> dict[str, Any]:
    """Ask one choice question per direction of every result-table pair."""
    record = empty_record()
    tables = result_tables(selected_columns)
    pairs = build_pairs(tables)
    record["num_result_tables"] = len(tables)
    record["num_pairs"] = len(pairs)
    record["pairs"] = pairs
    if not pairs:
        logger.info("Stage 3 (result joins): %d result tables, skipping", len(tables))
        return {"record": record, "chosen": []}

    state, questions = build_result_join_request(db_id, database, pairs)
    logger.info(
        "Stage 3 (result joins): %d result tables, %d pairs = %d choice questions",
        len(tables),
        len(pairs),
        len(questions),
    )
    result = call_jev(state, questions, jev_config)
    chosen = collect_chosen_columns(database, pairs, result["answers"])

    usage: dict[str, Any] = {}
    merge_usage(usage, result.get("raw", {}).get("usage"))
    record.update(
        {
            "num_questions": len(questions),
            "answers": result["answers"],
            "chosen_columns": chosen,
            "elapsed_seconds": round(result["elapsed_seconds"], 3),
            "usage": usage or None,
        }
    )
    logger.info("Stage 3 chose %d/%d join columns", len(chosen), len(questions))
    return {"record": record, "chosen": chosen}
