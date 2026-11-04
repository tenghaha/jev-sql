"""Analyze schema-linking errors and write a Markdown report with sampled cases.

Usage:
    conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev.yaml
    conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev.yaml --sample-size 20
    conda run -n znws python scripts/analyze_errors.py --config configs/pipeline_minidev.yaml \
        --cases outputs/runs/jev_minidev_cascade/analysis/error_cases.jsonl \
        --report outputs/runs/jev_minidev_cascade/reports/error_analysis.md

Inputs:
    <output_dir>/reports/<eval.details_file>   per-sample eval details
    <output_dir>/<eval.preds_file>             original pipeline answers
    questions_path                             question text, evidence, gold SQL

Outputs:
    <output_dir>/analysis/<analysis.error_cases_file>   associated error cases (JSONL)
    <output_dir>/reports/<analysis.report_file>         Markdown analysis report

Error kinds:
    over    predicted extras, no missing
    under   missing gold items, no extras
    mixed   both extras and missing items
    exact   no extras and no missing items
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev.config import load_config
from jev.error_analysis import (
    build_cases,
    sample_error_cases,
    summarize_cases,
    write_cases_jsonl,
    write_markdown_report,
)
from jev.evaluation import load_predictions, load_questions

logger = logging.getLogger("analyze_errors")


def _questions_by_id(questions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(question.get("question_id", index)): question
        for index, question in enumerate(questions)
    }


def _predictions_by_id(predictions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(prediction.get("question_id")): prediction
        for prediction in predictions
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze schema-linking errors.")
    parser.add_argument("--config", default="configs/pipeline_minidev.yaml",
                        help="Path to the pipeline YAML config.")
    parser.add_argument("--eval-details", default=None, help="Override the eval details JSON path.")
    parser.add_argument("--preds", default=None, help="Override the pipeline predictions path.")
    parser.add_argument("--cases", default=None, help="Override the output JSONL cases path.")
    parser.add_argument("--report", default=None, help="Override the output Markdown report path.")
    parser.add_argument("--sample-size", type=int, default=None, help="Number of sampled error cases.")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for sampling.")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Stage 2 score threshold used for attribution.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    config = load_config(args.config)
    eval_config = config.get("eval") or {}
    analysis_config = config.get("analysis") or {}
    output_dir = Path(config["output_dir"])

    eval_details_path = (
        Path(args.eval_details)
        if args.eval_details
        else output_dir / "reports" / eval_config.get("details_file", "schema_linking.json")
    )
    cases_path = (
        Path(args.cases)
        if args.cases
        else output_dir / "analysis" / analysis_config.get("error_cases_file", "error_cases.jsonl")
    )
    report_path = (
        Path(args.report)
        if args.report
        else output_dir / "reports" / analysis_config.get("report_file", "error_analysis.md")
    )
    preds_path = (
        Path(args.preds)
        if args.preds
        else output_dir / eval_config.get("preds_file", "batch_answers.jsonl")
    )
    sample_size = (
        args.sample_size
        if args.sample_size is not None
        else int(analysis_config.get("sample_size", 12))
    )
    seed = args.seed if args.seed is not None else int(analysis_config.get("seed", 42))
    threshold = (
        args.threshold
        if args.threshold is not None
        else float((config.get("cascade") or {}).get("column_score_threshold", 2.0))
    )

    if not eval_details_path.exists():
        logger.error("Eval details not found: %s", eval_details_path)
        logger.error("Run scripts/evaluate_schema_linking.py first.")
        return 1
    if not preds_path.exists():
        logger.error("Predictions not found: %s", preds_path)
        return 1

    eval_result = json.loads(eval_details_path.read_text(encoding="utf-8"))
    eval_summary = eval_result.get("summary") or {}
    questions = load_questions(config["questions_path"])
    predictions, parse_errors = load_predictions(preds_path)
    if parse_errors:
        logger.warning("Skipped %d malformed prediction lines.", parse_errors)

    cases = build_cases(
        eval_result,
        _questions_by_id(questions),
        _predictions_by_id(predictions),
        threshold,
    )
    summary = summarize_cases(cases, eval_summary, sample_size, seed)
    sampled = sample_error_cases(cases, sample_size, seed)

    cases_file = write_cases_jsonl(cases, cases_path)
    report_file = write_markdown_report(
        summary,
        sampled,
        eval_summary,
        report_path,
        cases_file,
    )

    print(f"Evaluated: {summary['evaluated']} | Errors: {summary['errors']} | Sampled: {len(sampled)}")
    print(f"Cases: {cases_file}")
    print(f"Report: {report_file}")
    return 0 if summary["evaluated"] else 1


if __name__ == "__main__":
    sys.exit(main())
