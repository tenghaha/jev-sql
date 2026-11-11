"""All-tables cascade: per-table column scoring for every table, column selection, result joins."""

from __future__ import annotations

import json
import logging
from typing import Any

from .ambiguous_stage import empty_record as empty_ambiguous_record
from .ambiguous_stage import run_ambiguous_stage
from .column_candidates import collect_column_scores
from .column_stage import run_column_stage
from .finalize import add_join_columns, classify_columns, finalize_selected
from .request_builder import DEFAULT_SCORE_SCALE
from .result_join_stage import empty_record as empty_join_record
from .result_join_stage import run_result_join_stage

logger = logging.getLogger(__name__)


def _empty_stages(
    cascade_config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    stage_table = {
        "num_tables": 0,
        "selected_tables": [],
    }
    stage_column = {
        "score_scale": list(cascade_config.get("score_scale", DEFAULT_SCORE_SCALE)),
        "score_threshold": float(cascade_config.get("column_score_threshold", 2.0)),
        "select_mode": str(cascade_config.get("column_select_mode", "hard")),
        "num_tables": 0,
        "num_questions": 0,
        "tables": {},
        "answers": {},
        "elapsed_seconds": 0.0,
        "usage": None,
    }
    return stage_table, stage_column, empty_join_record(), empty_ambiguous_record(cascade_config)


def _empty_result(
    stage_table: dict[str, Any],
    stage_column: dict[str, Any],
    stage_join: dict[str, Any],
    stage_ambiguous: dict[str, Any],
) -> dict[str, Any]:
    return {
        "stage_table": stage_table,
        "stage_column": stage_column,
        "stage_join": stage_join,
        "stage_ambiguous": stage_ambiguous,
        "linked_columns": [],
        "linked_columns_union": [],
        "linked_tables": [],
    }


def _all_tables(database: dict[str, Any]) -> list[dict[str, Any]]:
    """Every table becomes a scored table; ids follow schema order (t1..tN)."""
    return [
        {"table_id": f"t{index}", "table": table["name"]}
        for index, table in enumerate(database["tables"], start=1)
    ]


def run_cascade(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    cascade_config: dict[str, Any],
    jev_config: dict[str, Any],
) -> dict[str, Any]:
    """Score columns for every table, select columns, and complete result joins."""
    stage_table, stage_column, stage_join, stage_ambiguous = _empty_stages(cascade_config)
    column_threshold = stage_column["score_threshold"]

    if not mentions:
        logger.warning("No mentions extracted; skipping all JEV stages")
        return _empty_result(stage_table, stage_column, stage_join, stage_ambiguous)

    selected_tables = _all_tables(database)
    stage_table = {"num_tables": len(selected_tables), "selected_tables": selected_tables}
    logger.info(
        "Stage 2 (columns): scoring all %d tables: %s",
        len(selected_tables),
        [item["table"] for item in selected_tables],
    )

    column_result = run_column_stage(
        question,
        evidence,
        db_id,
        mentions,
        database,
        selected_tables,
        cascade_config,
        jev_config,
    )
    stage_column = column_result["record"]
    stage_column["select_mode"] = str(cascade_config.get("column_select_mode", "hard")).lower()

    scored = collect_column_scores(mentions, column_result["column_states"], column_result["answers"])
    mode = stage_column["select_mode"]
    if mode == "ambiguous":
        high = float(cascade_config.get("column_select_high", 2.5))
        low = float(cascade_config.get("column_select_low", 1.0))
        selected, ambiguous, dropped = classify_columns(scored, high, low)
        ambiguous_result = run_ambiguous_stage(
            question,
            evidence,
            db_id,
            mentions,
            database,
            column_result["column_states"],
            selected,
            ambiguous,
            dropped,
            cascade_config,
            jev_config,
        )
        stage_ambiguous = ambiguous_result["record"]
        chosen = selected + ambiguous_result["accepted"]
        mention_floor = low
    else:
        selected = [
            entry for entry in scored.values() if entry["best_score"] >= column_threshold
        ]
        selected.sort(key=lambda item: (-item["best_score"], item["table"], item["column"]))
        chosen = selected
        mention_floor = column_threshold

    final = finalize_selected(mentions, chosen, mention_floor)

    join_result = run_result_join_stage(
        db_id, database, final["linked_columns_union"], jev_config
    )
    stage_join = join_result["record"]
    union, num_added = add_join_columns(final["linked_columns_union"], join_result["chosen"])
    stage_join["num_added"] = num_added

    linked_tables = sorted({item["table"] for item in union})
    logger.info(
        "Cascade done in %.1fs: %d tables, %d columns (ambiguous %d/%d accepted), "
        "%d pairs, %d join columns added, %d unique columns",
        stage_column["elapsed_seconds"]
        + stage_ambiguous["elapsed_seconds"]
        + stage_join["elapsed_seconds"],
        len(linked_tables),
        sum(len(item["columns"]) for item in final["linked_columns"]),
        stage_ambiguous["num_accepted"],
        stage_ambiguous["num_ambiguous"],
        stage_join["num_pairs"],
        num_added,
        len(union),
    )
    logger.debug("Linked columns: %s", json.dumps(final["linked_columns"], ensure_ascii=False))
    return {
        "stage_table": stage_table,
        "stage_column": stage_column,
        "stage_join": stage_join,
        "stage_ambiguous": stage_ambiguous,
        "linked_columns": final["linked_columns"],
        "linked_columns_union": union,
        "linked_tables": linked_tables,
    }
