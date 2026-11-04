"""Step 1: table-level Noul screening."""

from __future__ import annotations

import json
import logging
from typing import Any

from .jev_client import call_jev
from .request_builder import build_table_request
from .stage_utils import merge_usage
from .table_candidates import build_table_candidates, select_tables

logger = logging.getLogger(__name__)


def run_table_stage(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    cascade_config: dict[str, Any],
    jev_config: dict[str, Any],
) -> dict[str, Any]:
    """Ask one Noul question per table and keep the top-k candidate tables."""
    candidate_states = build_table_candidates(database)
    limit = int(cascade_config.get("table_candidates", 5))
    threshold = float(cascade_config.get("table_noul_threshold", 0.5))
    if limit < 0:
        raise ValueError(f"table_candidates must be >= 0, got {limit}")

    state, questions = build_table_request(
        question,
        evidence,
        db_id,
        mentions,
        database,
        candidate_states,
    )
    logger.info("Stage 1 (tables): %d tables = %d Noul questions", len(candidate_states), len(questions))

    result = call_jev(state, questions, jev_config)
    ranked = select_tables(candidate_states, result["answers"], 0)
    selected = select_tables(candidate_states, result["answers"], limit, threshold)
    logger.info("Stage 1 selected %d/%d tables: %s", len(selected), len(ranked), [item["table"] for item in selected])
    logger.debug("Stage 1 table scores: %s", json.dumps(ranked, ensure_ascii=False))

    usage: dict[str, Any] = {}
    merge_usage(usage, result.get("raw", {}).get("usage"))
    record = {
        "num_candidates": len(candidate_states),
        "num_questions": len(questions),
        "selection": {"limit": limit, "noul_threshold": threshold},
        "candidates": ranked,
        "selected_tables": selected,
        "elapsed_seconds": result["elapsed_seconds"],
        "usage": usage or None,
    }
    return {"record": record, "candidate_states": candidate_states, "answers": result["answers"]}
