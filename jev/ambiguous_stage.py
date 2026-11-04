"""Step 3b: resolve ambiguous columns with one extra Noul round."""

from __future__ import annotations

import logging
from typing import Any

from .candidates import noul_probability
from .jev_client import call_jev
from .request_builder import ambiguous_question_id, build_ambiguous_request
from .stage_utils import merge_usage

logger = logging.getLogger(__name__)


def empty_record(cascade_config: dict[str, Any]) -> dict[str, Any]:
    return {
        "high_threshold": float(cascade_config.get("column_select_high", 2.5)),
        "low_threshold": float(cascade_config.get("column_select_low", 1.0)),
        "noul_threshold": float(cascade_config.get("ambiguous_noul_threshold", 0.5)),
        "num_selected": 0,
        "num_ambiguous": 0,
        "num_dropped": 0,
        "num_accepted": 0,
        "candidates": [],
        "answers": {},
        "accepted_columns": [],
        "elapsed_seconds": 0.0,
        "usage": None,
    }


def run_ambiguous_stage(
    question: str,
    evidence: str,
    db_id: str,
    mentions: list[dict[str, str]],
    database: dict[str, Any],
    column_states: dict[str, dict[str, Any]],
    selected: list[dict[str, Any]],
    ambiguous: list[dict[str, Any]],
    dropped: list[dict[str, Any]],
    cascade_config: dict[str, Any],
    jev_config: dict[str, Any],
) -> dict[str, Any]:
    """Ask whether each ambiguous column must be added to the already selected set."""
    record = empty_record(cascade_config)
    record["num_selected"] = len(selected)
    record["num_ambiguous"] = len(ambiguous)
    record["num_dropped"] = len(dropped)
    record["candidates"] = [
        {
            "candidate_id": entry["candidate_id"],
            "table": entry["table"],
            "column": entry["column"],
            "dtype": entry["dtype"],
            "best_score": entry["best_score"],
        }
        for entry in ambiguous
    ]
    if not ambiguous:
        logger.info("Stage 2b (ambiguous): no ambiguous columns, skipping")
        return {"record": record, "accepted": []}

    ambiguous_states = {}
    for entry in ambiguous:
        candidate = column_states[entry["candidate_id"]]
        ambiguous_states[entry["candidate_id"]] = {
            "table_name": entry["table"],
            "column_name": entry["column"],
            "dtype": entry["dtype"],
            "sample": candidate.get("sample"),
            "date_format": candidate.get("date_format"),
            "best_score": entry["best_score"],
        }
    selected_columns = [
        {"table": entry["table"], "column": entry["column"], "dtype": entry["dtype"]}
        for entry in selected
    ]

    state, questions = build_ambiguous_request(
        question,
        evidence,
        db_id,
        mentions,
        database,
        selected_columns,
        ambiguous_states,
    )
    logger.info(
        "Stage 2b (ambiguous): %d selected + %d ambiguous columns = %d Noul questions",
        len(selected),
        len(ambiguous),
        len(questions),
    )
    result = call_jev(state, questions, jev_config)

    noul_threshold = record["noul_threshold"]
    accepted = []
    for entry in ambiguous:
        answer = result["answers"].get(ambiguous_question_id(entry["candidate_id"]))
        noul = noul_probability(answer)
        if noul < noul_threshold:
            continue
        accepted.append({**entry, "source": "ambiguous", "noul": round(noul, 4)})

    usage: dict[str, Any] = {}
    merge_usage(usage, result.get("raw", {}).get("usage"))
    record.update(
        {
            "num_accepted": len(accepted),
            "answers": result["answers"],
            "accepted_columns": [
                {
                    "table": entry["table"],
                    "column": entry["column"],
                    "best_score": entry["best_score"],
                    "noul": entry["noul"],
                }
                for entry in accepted
            ],
            "elapsed_seconds": round(result["elapsed_seconds"], 3),
            "usage": usage or None,
        }
    )
    logger.info("Stage 2b accepted %d/%d ambiguous columns", len(accepted), len(ambiguous))
    return {"record": record, "accepted": accepted}
