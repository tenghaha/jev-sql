"""Run the table-first JEV schema-linking pipeline over a BIRD split.

Usage:
    conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first.yaml
    conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first.yaml --limit 5
    conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first.yaml --resume
    conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev_table_first.yaml --limit 5 --debug

Config fields:
    dataset, split, schema_path, questions_path, databases_dir, output_dir
    mentions: enabled
    mention_llm: model_env / api_key_env / base_url_env
    cascade: table_candidates / table_noul_threshold / table_column_batch
             score_scale / column_select_mode / column_score_threshold
    jev: model / endpoint_env / api_key_env / timeout

Output:
    Appends one JSON record per question to <output_dir>/batch_answers.jsonl.
    --resume skips questions whose previous record has status=ok.
    --debug emits full JEV requests and responses through logging.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev.config import load_config
from jev.logging_utils import setup_logging
from jev.pipeline import load_questions, load_schema, run_question

logger = logging.getLogger("run_batch")


def load_done_question_ids(output_path: Path) -> set[str]:
    done = set()
    if not output_path.exists():
        return done
    for line in output_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("status") == "ok":
            done.add(str(record.get("question_id")))
    return done


def append_record(output_path: Path, record: dict) -> None:
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a BIRD split through the JEV cascade.")
    parser.add_argument("--config", required=True, help="Path to the experiment YAML config.")
    parser.add_argument("--limit", type=int, default=0, help="Only run the first N questions; 0 means all.")
    parser.add_argument("--resume", action="store_true", help="Skip questions already recorded as ok.")
    parser.add_argument("--debug", action="store_true",
                        help="Emit debug logs, including full JEV questions and responses.")
    args = parser.parse_args()

    setup_logging(args.debug)
    config = load_config(args.config)
    databases = load_schema(config["schema_path"])
    questions = load_questions(config["questions_path"])
    if args.limit > 0:
        questions = questions[: args.limit]

    output_path = Path(config["output_dir"]) / "batch_answers.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    done = load_done_question_ids(output_path) if args.resume else set()

    logger.info(
        "%s / %s | questions=%d | output=%s",
        config["dataset"],
        config["split"],
        len(questions),
        output_path,
    )
    if args.resume:
        logger.info("Resume: %d questions already ok", len(done))

    success = 0
    failed = 0
    skipped = 0
    for index, item in enumerate(questions):
        question_id = item.get("question_id")
        if question_id is None:
            question_id = index

        if args.resume and str(question_id) in done:
            skipped += 1
            logger.info("[%d/%d] qid=%s skipped", index + 1, len(questions), question_id)
            continue

        try:
            result = run_question(item, databases, config, question_id=question_id)
            result["status"] = "ok"
            success += 1
            logger.info(
                "[%d/%d] qid=%s db=%s status=ok spans=%d tables=%d/%d columns_q=%d "
                "pairs=%d join_cols=%d linked=%d elapsed=%.1fs",
                index + 1,
                len(questions),
                question_id,
                result["db_id"],
                len(result["mentions"]),
                len(result["stage_table"]["selected_tables"]),
                result["stage_table"]["num_candidates"],
                result["stage_column"]["num_questions"],
                result["stage_join"]["num_pairs"],
                len(result["stage_join"]["chosen_columns"]),
                len(result["linked_columns_union"]),
                result["elapsed"]["total_seconds"],
            )
            logger.debug("Linked columns: %s", json.dumps(result["linked_columns"], ensure_ascii=False))
        except Exception as exc:
            result = {
                "question_id": question_id,
                "db_id": item.get("db_id"),
                "question": item.get("question", ""),
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            failed += 1
            logger.error("[%d/%d] qid=%s status=failed reason=%s",
                         index + 1, len(questions), question_id, result["error"])

        append_record(output_path, result)

    logger.info("Done: ok=%d failed=%d skipped=%d | %s",
                success, failed, skipped, output_path)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
