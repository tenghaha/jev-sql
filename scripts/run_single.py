"""Run the two-stage JEV schema-linking pipeline for one BIRD question.

Usage:
    conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev.yaml --question-id 1471
    conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev.yaml --question-id 1471 --debug
    conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev.yaml --question-id 1471 --no-save

Config fields:
    dataset, split, schema_path, questions_path, output_dir
    mention_llm: model_env / api_key_env / base_url_env
    cascade: stage1_candidates / stage1_column_batch
             score_scale / stage2_score_threshold
    jev: model / endpoint_env / api_key_env / timeout

Output:
    Prints the final linked columns as JSON.
    Saves the full run record to <output_dir>/single/q<question_id>.json unless --no-save.
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

logger = logging.getLogger("run_single")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one BIRD question through the JEV cascade.")
    parser.add_argument("--config", required=True, help="Path to the experiment YAML config.")
    parser.add_argument("--question-id", type=int, required=True,
                        help="BIRD question_id; falls back to the record index when absent.")
    parser.add_argument("--no-save", action="store_true", help="Only print, do not save the run record.")
    parser.add_argument("--debug", action="store_true",
                        help="Emit debug logs, including full JEV questions and responses.")
    args = parser.parse_args()

    setup_logging(args.debug)
    config = load_config(args.config)
    databases = load_schema(config["schema_path"])
    questions = load_questions(config["questions_path"])

    target = next((item for item in questions if item.get("question_id") == args.question_id), None)
    if target is None and 0 <= args.question_id < len(questions):
        target = questions[args.question_id]
    if target is None:
        logger.error("Question not found: question_id=%s", args.question_id)
        return 1

    result = run_question(target, databases, config, question_id=args.question_id)

    for mention in result["mentions"]:
        logger.info("  %s [%s] %s", mention["span_id"], mention["span_type"], mention["text"])
    logger.info(
        "Stage 1: %d/%d columns kept; Stage 2: %d score questions; linked columns: %d unique",
        len(result["stage1"]["selected_candidates"]),
        result["stage1"]["num_candidates"],
        result["stage2"]["num_questions"],
        len(result["linked_columns_union"]),
    )
    logger.debug("Linked columns: %s", json.dumps(result["linked_columns"], ensure_ascii=False))

    print(json.dumps(
        {
            "question_id": result["question_id"],
            "db_id": result["db_id"],
            "linked_columns": result["linked_columns"],
            "linked_columns_union": result["linked_columns_union"],
            "elapsed_seconds": round(result["elapsed"]["total_seconds"], 2),
        },
        ensure_ascii=False,
        indent=2,
    ))

    if not args.no_save:
        output_path = Path(config["output_dir"]) / "single" / f"q{result['question_id']}.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("Saved run record: %s", output_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
