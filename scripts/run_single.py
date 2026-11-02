"""Run the JEV schema-linking pipeline for one BIRD question.

Usage:
    conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev.yaml --question-id 1471
    conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev.yaml --question-id 1471 --no-save
    conda run -n znws python scripts/run_single.py --config configs/pipeline_minidev.yaml --question-id 1471 --show-request

Config fields:
    dataset, split, schema_path, questions_path, output_dir
    mention_llm: model_env / api_key_env / base_url_env
    jev: model / endpoint_env / api_key_env / max_candidates

Output:
    Prints extracted mentions and raw JEV answers.
    Saves the full run record to <output_dir>/single/q<question_id>.json unless --no-save.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev.config import load_config
from jev.pipeline import load_questions, load_schema, run_question


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one BIRD question through the JEV pipeline.")
    parser.add_argument("--config", required=True, help="Path to the experiment YAML config.")
    parser.add_argument("--question-id", type=int, required=True,
                        help="BIRD question_id; falls back to the record index when absent.")
    parser.add_argument("--no-save", action="store_true", help="Only print, do not save the run record.")
    parser.add_argument("--show-request", action="store_true", help="Print the full JEV request JSON.")
    args = parser.parse_args()

    config = load_config(args.config)
    databases = load_schema(config["schema_path"])
    questions = load_questions(config["questions_path"])

    target = next((item for item in questions if item.get("question_id") == args.question_id), None)
    if target is None and 0 <= args.question_id < len(questions):
        target = questions[args.question_id]
    if target is None:
        print(f"Question not found: question_id={args.question_id}")
        return 1

    result = run_question(target, databases, config, question_id=args.question_id)

    print(f">>> {config['dataset']} / {config['split']} | question_id={result['question_id']} "
          f"| db_id={result['db_id']}")
    print(f">>> Question: {result['question']}")

    print(f"\n[mentions] {len(result['mentions'])}")
    for mention in result["mentions"]:
        print(f"  {mention['span_id']}\t{mention['span_type']}\t{mention['text']}")

    print(f"\n[JEV call] elapsed={result['jev_call']['elapsed_seconds']:.2f}s")
    print(f"[JEV request] {len(result['jev_request']['questions'])} questions")
    if args.show_request:
        print(json.dumps(result["jev_request"], ensure_ascii=False, indent=2))

    print("\n[JEV answers]")
    print(json.dumps(result["jev_answers"], ensure_ascii=False, indent=2))

    if not args.no_save:
        output_path = Path(config["output_dir"]) / "single" / f"q{result['question_id']}.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\n[saved] {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
