"""Table-first cascade: table Noul, per-table column scoring, column selection, forced FK columns."""

from __future__ import annotations

import json
import logging
from typing import Any

from .ambiguous_stage import empty_record as empty_ambiguous_record
from .ambiguous_stage import run_ambiguous_stage
from .column_candidates import collect_column_scores
from .column_stage import run_column_stage
from .finalize import classify_columns, finalize_selected
from .fk_candidates import build_link_candidates
from .request_builder import DEFAULT_SCORE_SCALE
from .table_stage import run_table_stage

logger = logging.getLogger(__name__)


def _empty_stages(
    cascade_config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    stage_table = {
        "num_candidates": 0,
        "num_questions": 0,
        "selection": {
            "limit": int(cascade_config.get("table_candidates", 5)),
            "noul_threshold": float(cascade_config.get("table_noul_threshold", 0.5)),
        },
        "candidates": [],
        "selected_tables": [],
        "elapsed_seconds": 0.0,
        "usage": None,
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
    stage_join = {
        "threshold": float(cascade_config.get("fk_noul_threshold", 0.5)),
        "num_candidates": 0,
        "candidates": [],
        "answers": {},
        "selected_links": [],
    }
    return stage_table, stage_column, stage_join, empty_ambiguous_record(cascade_config)


def _split_answers(answers: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    column_answers = {key: value for key, value in answers.items() if key.startswith("s2_")}
    join_answers = {key: value for key, value in answers.items() if key.startswith("j_")}
    return column_answers, join_answers


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


def run_cascade(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    cascade_config: dict[str, Any],
    jev_config: dict[str, Any],
) -> dict[str, Any]:
    """Run table screening, per-table column scoring, column selection, and final filtering."""
    stage_table, stage_column, stage_join, stage_ambiguous = _empty_stages(cascade_config)
    column_threshold = stage_column["score_threshold"]
    link_threshold = stage_join["threshold"]

    if not mentions:
        logger.warning("No mentions extracted; skipping all JEV stages")
        return _empty_result(stage_table, stage_column, stage_join, stage_ambiguous)

    table_result = run_table_stage(
        question, evidence, db_id, mentions, database, cascade_config, jev_config
    )
    stage_table = table_result["record"]
    selected_tables = stage_table["selected_tables"]

    if not selected_tables:
        logger.warning("No candidate tables selected; skipping column stage")
        return _empty_result(stage_table, stage_column, stage_join, stage_ambiguous)

    focus_tables = [item["table"] for item in selected_tables]
    link_states = build_link_candidates(database, focus_tables)
    stage_join["num_candidates"] = len(link_states)
    stage_join["candidates"] = list(link_states.values())

    column_result = run_column_stage(
        question,
        evidence,
        db_id,
        mentions,
        database,
        selected_tables,
        link_states,
        cascade_config,
        jev_config,
    )
    column_answers, join_answers = _split_answers(column_result["answers"])
    stage_column = column_result["record"]
    stage_column["answers"] = column_answers
    stage_column["select_mode"] = str(cascade_config.get("column_select_mode", "hard")).lower()
    stage_join["answers"] = join_answers

    scored = collect_column_scores(mentions, column_result["column_states"], column_answers)
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

    final = finalize_selected(
        mentions,
        chosen,
        column_result["column_states"],
        link_states,
        join_answers,
        link_threshold,
        mention_floor,
    )
    stage_join["selected_links"] = final["selected_links"]

    logger.info(
        "Cascade done in %.1fs: %d tables, %d columns (ambiguous %d/%d accepted), %d joins, %d unique columns",
        stage_table["elapsed_seconds"] + stage_column["elapsed_seconds"] + stage_ambiguous["elapsed_seconds"],
        len(final["linked_tables"]),
        sum(len(item["columns"]) for item in final["linked_columns"]),
        stage_ambiguous["num_accepted"],
        stage_ambiguous["num_ambiguous"],
        len(final["selected_links"]),
        len(final["linked_columns_union"]),
    )
    logger.debug("Linked columns: %s", json.dumps(final["linked_columns"], ensure_ascii=False))
    return {
        "stage_table": stage_table,
        "stage_column": stage_column,
        "stage_join": stage_join,
        "stage_ambiguous": stage_ambiguous,
        **final,
    }
