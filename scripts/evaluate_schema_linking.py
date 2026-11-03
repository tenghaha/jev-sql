"""Evaluate schema-linking predictions against BIRD gold SQL.

Usage:
    conda run -n znws python scripts/evaluate_schema_linking.py --config configs/pipeline_minidev.yaml
    conda run -n znws python scripts/evaluate_schema_linking.py --config configs/pipeline_minidev.yaml --limit 20
    conda run -n znws python scripts/evaluate_schema_linking.py --config configs/pipeline_minidev.yaml \
        --preds outputs/runs/jev_minidev_cascade/batch_answers.jsonl

Config fields:
    dataset, split
    questions_path  BIRD question file containing the gold SQL
    databases_dir   SQLite database root directory
    output_dir      experiment output root
    eval.preds_file     predictions, relative to output_dir
    eval.report_file    Markdown report, relative to output_dir/reports
    eval.details_file   JSON details, relative to output_dir/reports
    eval.include_missing true: evaluate missing predictions as empty sets

Gold tables/columns are extracted with a SQLite authorizer, so aliases, CTEs,
subqueries, and quoted identifiers are resolved by SQLite rather than by
literal string matching. Metrics are micro-aggregated over evaluated samples.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev.config import load_config
from jev.evaluation import (
    evaluate_schema_linking,
    format_summary,
    load_predictions,
    load_questions,
    write_report,
)

logger = logging.getLogger("evaluate_schema_linking")


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate schema-linking predictions with gold SQL.")
    parser.add_argument("--config", default="configs/pipeline_minidev.yaml",
                        help="Path to the pipeline YAML config.")
    parser.add_argument("--preds", default=None, help="Override the predictions path.")
    parser.add_argument("--report", default=None, help="Override the Markdown report path.")
    parser.add_argument("--details", default=None, help="Override the JSON details path.")
    parser.add_argument("--limit", type=int, default=0, help="Evaluate only the first N questions.")
    parser.add_argument("--include-missing", action="store_true",
                        help="Treat questions without predictions as empty predictions.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    config = load_config(args.config)
    eval_config = config.get("eval") or {}
    output_dir = Path(config["output_dir"])
    report_dir = output_dir / "reports"

    preds_path = (
        Path(args.preds)
        if args.preds
        else output_dir / eval_config.get("preds_file", "batch_answers.jsonl")
    )
    report_path = (
        Path(args.report)
        if args.report
        else report_dir / eval_config.get("report_file", "schema_linking.md")
    )
    details_path = (
        Path(args.details)
        if args.details
        else report_dir / eval_config.get("details_file", "schema_linking.json")
    )

    if not preds_path.exists():
        logger.error("Predictions not found: %s", preds_path)
        return 1

    questions = load_questions(config["questions_path"])
    if args.limit > 0:
        questions = questions[: args.limit]
    predictions, parse_errors = load_predictions(preds_path)

    logger.info(
        "%s / %s | questions=%d | predictions=%d | parse_errors=%d",
        config["dataset"],
        config["split"],
        len(questions),
        len(predictions),
        parse_errors,
    )

    result = evaluate_schema_linking(
        questions,
        predictions,
        databases_dir=config["databases_dir"],
        dataset=config["dataset"],
        split=config["split"],
        include_missing=args.include_missing or bool(eval_config.get("include_missing", False)),
        parse_errors=parse_errors,
    )

    report_file, details_file = write_report(result, report_path, details_path)
    print("\n" + format_summary(result))
    print(f"\nReport: {report_file}")
    print(f"Details: {details_file}")
    return 0 if result["summary"]["evaluated"] else 1


if __name__ == "__main__":
    sys.exit(main())
