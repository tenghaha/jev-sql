"""End-to-end schema-linking pipeline: question -> mentions -> table-first cascade -> columns."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .cascade import run_cascade
from .mentions import extract_mentions

logger = logging.getLogger(__name__)


def load_schema(path: str | Path) -> dict[str, dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {database["db_id"]: database for database in data["databases"]}


def load_questions(path: str | Path) -> list[dict[str, Any]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_question(
    record: dict[str, Any],
    databases: dict[str, dict[str, Any]],
    config: dict[str, Any],
    question_id: Any = None,
) -> dict[str, Any]:
    """Run one question through mention extraction and the table-first cascade."""
    started = time.perf_counter()
    question = record["question"]
    db_id = record["db_id"]
    evidence = record.get("evidence", "")
    question_id = record.get("question_id", question_id)

    if db_id not in databases:
        raise KeyError(f"Database not found in schema: {db_id}")

    logger.info("Question %s (%s): %s", question_id, db_id, question)
    mention_started = time.perf_counter()
    mentions = extract_mentions(question, evidence, config["mention_llm"])
    mention_elapsed = time.perf_counter() - mention_started
    logger.info("Extracted %d mentions", len(mentions))
    logger.debug("Mentions: %s", json.dumps(mentions, ensure_ascii=False))

    cascade_result = run_cascade(
        question,
        evidence,
        db_id,
        mentions,
        databases[db_id],
        config["cascade"],
        config["jev"],
    )

    return {
        "question_id": question_id,
        "db_id": db_id,
        "question": question,
        "evidence": evidence,
        "mentions": mentions,
        "stage_table": cascade_result["stage_table"],
        "stage_column": cascade_result["stage_column"],
        "stage_join": cascade_result["stage_join"],
        "linked_columns": cascade_result["linked_columns"],
        "linked_columns_union": cascade_result["linked_columns_union"],
        "linked_tables": cascade_result["linked_tables"],
        "usage": {
            "table": cascade_result["stage_table"].get("usage"),
            "column": cascade_result["stage_column"].get("usage"),
        },
        "elapsed": {
            "mentions_seconds": mention_elapsed,
            "table_seconds": cascade_result["stage_table"]["elapsed_seconds"],
            "column_seconds": cascade_result["stage_column"]["elapsed_seconds"],
            "total_seconds": time.perf_counter() - started,
        },
    }
