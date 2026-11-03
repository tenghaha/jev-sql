"""Two-stage cascade: all-column Noul screening, then thresholded scoring."""

from __future__ import annotations

import json
import logging
from typing import Any

from .candidates import (
    assign_candidate_ids,
    filter_by_score,
    flatten_candidates,
    select_stage1_candidates,
)
from .jev_client import call_jev
from .request_builder import (
    DEFAULT_SCORE_SCALE,
    build_stage1_request,
    build_stage2_request,
)

logger = logging.getLogger(__name__)


def _usage(result: dict[str, Any]) -> Any:
    return result.get("raw", {}).get("usage")


def _merge_usage(target: dict[str, Any], usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            target[key] = target.get(key, 0) + value


def _chunk(items: list[str], size: int) -> list[list[str]]:
    if size <= 0 or size >= len(items):
        return [items]
    return [items[index : index + size] for index in range(0, len(items), size)]


def run_cascade(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    cascade_config: dict[str, Any],
    jev_config: dict[str, Any],
) -> dict[str, Any]:
    """Run stage-1 Noul screening, stage-2 scoring, and score filtering."""
    candidate_states = assign_candidate_ids(flatten_candidates(database))
    limit = int(cascade_config.get("stage1_candidates", 12))
    threshold = float(cascade_config.get("stage2_score_threshold", 2.0))
    score_scale = list(cascade_config.get("score_scale", DEFAULT_SCORE_SCALE))
    if limit < 0:
        raise ValueError(f"stage1_candidates must be >= 0, got {limit}")

    stage1: dict[str, Any] = {
        "num_candidates": len(candidate_states),
        "num_questions": 0,
        "num_batches": 0,
        "column_batch": int(cascade_config.get("stage1_column_batch", 15)),
        "selection": {"limit": limit},
        "selected_candidates": [],
        "elapsed_seconds": 0.0,
        "usage": None,
    }
    stage2: dict[str, Any] = {
        "num_candidates": 0,
        "num_questions": 0,
        "score_scale": score_scale,
        "score_threshold": threshold,
        "answers": {},
        "elapsed_seconds": 0.0,
        "usage": None,
    }

    if not mentions:
        logger.warning("No mentions extracted; skipping both JEV stages")
        return {
            "stage1": stage1,
            "stage2": stage2,
            "linked_columns": [],
            "linked_columns_union": [],
        }

    candidate_ids = list(candidate_states)
    groups = _chunk(candidate_ids, stage1["column_batch"])
    stage1["num_batches"] = len(groups)
    stage1_answers: dict[str, Any] = {}
    stage1_usage: dict[str, Any] = {}
    for batch_index, group in enumerate(groups, start=1):
        state1, questions1 = build_stage1_request(
            question,
            evidence,
            db_id,
            mentions,
            database,
            candidate_states,
            group,
        )
        stage1["num_questions"] += len(questions1)
        logger.info(
            "Stage 1 batch %d/%d: %d columns = %d Noul questions",
            batch_index,
            len(groups),
            len(group),
            len(questions1),
        )
        result1 = call_jev(state1, questions1, jev_config)
        stage1_answers.update(result1["answers"])
        stage1["elapsed_seconds"] += result1["elapsed_seconds"]
        _merge_usage(stage1_usage, _usage(result1))
    stage1["usage"] = stage1_usage or None

    selected = select_stage1_candidates(
        candidate_states,
        stage1_answers,
        limit,
    )
    stage1["selected_candidates"] = selected
    logger.info("Stage 1 selected %d/%d columns", len(selected), len(candidate_states))
    logger.debug("Stage 1 selected candidates: %s", json.dumps(selected, ensure_ascii=False))

    selected_ids = [item["candidate_id"] for item in selected]
    stage2["num_candidates"] = len(selected_ids)

    if selected_ids:
        state2, questions2 = build_stage2_request(
            question,
            evidence,
            db_id,
            mentions,
            database,
            candidate_states,
            selected_ids,
            score_scale,
        )
        stage2["num_questions"] = len(questions2)
        logger.info(
            "Stage 2: %d mentions x %d candidates = %d Score questions",
            len(mentions),
            len(selected_ids),
            len(questions2),
        )
        result2 = call_jev(state2, questions2, jev_config)
        stage2["answers"] = result2["answers"]
        stage2["elapsed_seconds"] = result2["elapsed_seconds"]
        stage2["usage"] = _usage(result2)
    else:
        logger.warning("Stage 1 selected no candidates; skipping stage 2")

    linked_columns, union_columns = filter_by_score(
        mentions,
        selected,
        candidate_states,
        stage2["answers"],
        threshold,
    )
    logger.info(
        "Cascade done in %.1fs: %d mention links, %d unique columns",
        stage1["elapsed_seconds"] + stage2["elapsed_seconds"],
        sum(len(item["columns"]) for item in linked_columns),
        len(union_columns),
    )
    return {
        "stage1": stage1,
        "stage2": stage2,
        "linked_columns": linked_columns,
        "linked_columns_union": union_columns,
    }
