"""Generate one SQL per question from the JEV-linked schema and evaluate EX.

The script reads a schema-linking run (`batch_answers.jsonl`), turns each
question's linked tables/columns into a compact schema, asks the generation LLM
for a single SQL statement, executes it on the BIRD database, and computes
Execution Accuracy plus table/field precision & recall against the gold SQL.

Usage:
    conda run -n znws python scripts/evaluate_ex.py --config configs/pipeline_minidev_table_first_result_join.yaml
    conda run -n znws python scripts/evaluate_ex.py --config <config> --limit 20
    conda run -n znws python scripts/evaluate_ex.py --config <config> --resume
    conda run -n znws python scripts/evaluate_ex.py --config <config> --no-generate

Config fields (added on top of a pipeline config):
    sql_generation  model_env / api_key_env / base_url_env / temperature / timeout / max_retries
    ex.input_file          schema-linking records, relative to output_dir
    ex.predictions_file    generated SQL, relative to output_dir
    ex.results_file        execution results JSON, relative to output_dir
    ex.report_file         Markdown report, relative to output_dir/reports
    ex.execution_timeout   per-SQL execution timeout in seconds
    ex.include_missing     score missing predictions as empty SQL
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev.config import load_config
from jev.evaluation import load_predictions
from jev.execution_eval import evaluate_execution, format_summary, write_report, write_results
from jev.pipeline import load_questions, load_schema
from jev.sql_generation import generate_sql, render_linked_schema

logger = logging.getLogger("evaluate_ex")


def load_cache(path: Path) -> dict[str, dict[str, Any]]:
    """Load previously generated predictions from a JSONL cache, keyed by question id."""
    cache: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return cache
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        cache[str(record.get("question_id"))] = record
    return cache


def write_cache(path: Path, records: list[dict[str, Any]]) -> None:
    """Atomically write the generation cache as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
    tmp.write_text(payload + ("\n" if payload else ""), encoding="utf-8")
    os.replace(tmp, path)


def generate_one(
    question: dict[str, Any],
    linking: dict[str, Any] | None,
    databases: dict[str, dict[str, Any]],
    config: dict[str, Any],
    fallback_id: Any,
) -> dict[str, Any]:
    """Generate a single SQL for one question from its linked schema."""
    question_id = question.get("question_id", fallback_id)
    db_id = question.get("db_id")
    record: dict[str, Any] = {
        "question_id": question_id,
        "db_id": db_id,
        "question": question.get("question", ""),
        "evidence": question.get("evidence", ""),
        "pred_sql": "",
        "status": "ok",
        "error": None,
        "num_linked_columns": 0,
        "elapsed_seconds": 0.0,
        "usage": None,
    }

    if linking is None:
        record.update(status="failed", error="No schema-linking record")
        return record
    linked = linking.get("linked_columns_union") or []
    record["num_linked_columns"] = len(linked)
    if not linked:
        record.update(status="failed", error="Empty linked schema")
        return record
    if db_id not in databases:
        record.update(status="failed", error=f"Database not in schema: {db_id}")
        return record

    schema_text = render_linked_schema(databases[db_id], linked)
    started = time.perf_counter()
    try:
        output = generate_sql(
            question.get("question", ""),
            question.get("evidence", ""),
            schema_text,
            config,
        )
    except Exception as exc:  # noqa: BLE001 - one failed question must not abort the run
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        return record

    record["pred_sql"] = output.get("sql") or ""
    record["usage"] = output.get("usage")
    record["elapsed_seconds"] = time.perf_counter() - started
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate SQL from the linked schema and evaluate EX.")
    parser.add_argument("--config", required=True, help="Path to the experiment YAML config.")
    parser.add_argument("--limit", type=int, default=0, help="Only run the first N questions; 0 means all.")
    parser.add_argument("--resume", action="store_true", help="Reuse already generated SQL from the cache.")
    parser.add_argument("--no-generate", action="store_true", help="Skip generation; evaluate the existing cache.")
    parser.add_argument("--workers", type=int, default=None, help="Parallel processes for execution (default: auto).")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    config = load_config(args.config)
    ex_config = config.get("ex") or {}
    sql_config = config.get("sql_generation") or {}
    output_dir = Path(config["output_dir"])
    datasets = load_schema(config["schema_path"])
    questions = load_questions(config["questions_path"])
    if args.limit > 0:
        questions = questions[: args.limit]

    linking_path = output_dir / ex_config.get("input_file", "batch_answers.jsonl")
    predictions_path = output_dir / ex_config.get("predictions_file", "ex_predictions.jsonl")
    results_path = output_dir / ex_config.get("results_file", "ex_results.json")
    report_path = output_dir / "reports" / ex_config.get("report_file", "execution.md")
    execution_timeout = float(ex_config.get("execution_timeout", 30.0))

    if not linking_path.exists():
        logger.error("Schema-linking records not found: %s", linking_path)
        return 1
    linking_records, _ = load_predictions(linking_path)
    linking_by_id = {str(record.get("question_id")): record for record in linking_records}

    logger.info(
        "%s / %s | questions=%d | linking=%d | output=%s",
        config["dataset"],
        config["split"],
        len(questions),
        len(linking_records),
        predictions_path,
    )

    if args.no_generate:
        records = list(load_cache(predictions_path).values())
        if not records:
            logger.error("No cached predictions to evaluate: %s", predictions_path)
            return 1
    else:
        cache = load_cache(predictions_path) if args.resume else {}
        records = []
        reused = 0
        for index, question in enumerate(questions):
            key = str(question.get("question_id", index))
            cached = cache.get(key)
            if args.resume and cached and cached.get("pred_sql"):
                records.append(cached)
                reused += 1
                continue
            record = generate_one(question, linking_by_id.get(key), datasets, sql_config, index)
            records.append(record)
            logger.info(
                "[%d/%d] qid=%s linked=%d status=%s elapsed=%.1fs",
                index + 1,
                len(questions),
                record["question_id"],
                record["num_linked_columns"],
                record["status"],
                record["elapsed_seconds"],
            )
        write_cache(predictions_path, records)
        logger.info("Generation done: generated=%d reused=%d", len(records) - reused, reused)

    workers = args.workers
    if workers is None:
        workers = min(os.cpu_count() or 1, 16)
    workers = max(1, min(workers, len(questions) or 1))

    result = evaluate_execution(
        questions,
        records,
        databases_dir=config["databases_dir"],
        dataset=config["dataset"],
        split=config["split"],
        timeout=execution_timeout,
        include_missing=bool(ex_config.get("include_missing", False)),
        workers=workers,
    )

    write_results(result, results_path)
    write_report(result, report_path)
    print("\n" + format_summary(result))
    print(f"\nResults: {results_path}")
    print(f"Report: {report_path}")
    return 0 if result["summary"]["scored"] else 1


if __name__ == "__main__":
    sys.exit(main())
