"""End-to-end schema-linking pipeline: question -> mentions -> JEV answers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .jev_client import call_jev
from .mentions import extract_mentions
from .request_builder import build_request


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
    """Run one question through mention extraction and a single JEV call."""
    started = time.perf_counter()
    question = record["question"]
    db_id = record["db_id"]
    evidence = record.get("evidence", "")
    question_id = record.get("question_id", question_id)

    if db_id not in databases:
        raise KeyError(f"Database not found in schema: {db_id}")

    mention_started = time.perf_counter()
    mentions = extract_mentions(question, evidence, config["mention_llm"])
    mention_elapsed = time.perf_counter() - mention_started

    state, questions = build_request(
        question,
        evidence,
        db_id,
        mentions,
        databases[db_id],
        config["jev"],
    )
    jev_result = call_jev(state, questions, config["jev"])

    # Downstream schema-linking decisions will consume jev_answers here.
    raw_usage = jev_result.get("raw", {}).get("usage")
    return {
        "question_id": question_id,
        "db_id": db_id,
        "question": question,
        "evidence": evidence,
        "mentions": mentions,
        "jev_request": {"state": state, "questions": questions},
        "jev_answers": jev_result["answers"],
        "jev_call": {
            "elapsed_seconds": jev_result["elapsed_seconds"],
        },
        "usage": raw_usage,
        "elapsed": {
            "mentions_seconds": mention_elapsed,
            "total_seconds": time.perf_counter() - started,
        },
    }
