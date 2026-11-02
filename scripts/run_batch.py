"""Run the JEV schema-linking pipeline over a BIRD split.

Usage:
    conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev.yaml
    conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev.yaml --limit 5
    conda run -n znws python scripts/run_batch.py --config configs/pipeline_minidev.yaml --resume

Config fields:
    dataset, split, schema_path, questions_path, output_dir
    mention_llm: model_env / api_key_env / base_url_env
    jev: model / endpoint_env / api_key_env / max_candidates

Output:
    Appends one JSON record per question to <output_dir>/batch_answers.jsonl.
    Prints mentions and raw JEV answers for every question.
    --resume skips questions whose previous record has status=ok.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev.config import load_config
from jev.pipeline import load_questions, load_schema, run_question


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
    parser = argparse.ArgumentParser(description="Run a BIRD split through the JEV pipeline.")
    parser.add_argument("--config", required=True, help="Path to the experiment YAML config.")
    parser.add_argument("--limit", type=int, default=0, help="Only run the first N questions; 0 means all.")
    parser.add_argument("--resume", action="store_true", help="Skip questions already recorded as ok.")
    args = parser.parse_args()

    config = load_config(args.config)
    databases = load_schema(config["schema_path"])
    questions = load_questions(config["questions_path"])
    if args.limit > 0:
        questions = questions[: args.limit]

    output_path = Path(config["output_dir"]) / "batch_answers.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    done = load_done_question_ids(output_path) if args.resume else set()

    print(f">>> {config['dataset']} / {config['split']} | questions={len(questions)} | output={output_path}")
    if args.resume:
        print(f">>> resume: {len(done)} questions already ok")

    success = 0
    failed = 0
    skipped = 0
    for index, item in enumerate(questions):
        question_id = item.get("question_id")
        if question_id is None:
            question_id = index

        if args.resume and str(question_id) in done:
            skipped += 1
            print(f"[{index + 1}/{len(questions)}] qid={question_id} skipped")
            continue

        try:
            result = run_question(item, databases, config, question_id=question_id)
            result["status"] = "ok"
            success += 1
            print(f"[{index + 1}/{len(questions)}] qid={question_id} db={result['db_id']} status=ok "
                  f"mentions={len(result['mentions'])} "
                  f"questions={len(result['jev_request']['questions'])} "
                  f"elapsed={result['elapsed']['total_seconds']:.1f}s")
            for mention in result["mentions"]:
                print(f"    {mention['span_id']}\t{mention['span_type']}\t{mention['text']}")
            print("    [JEV answers]")
            print("    " + json.dumps(result["jev_answers"], ensure_ascii=False))
        except Exception as exc:
            result = {
                "question_id": question_id,
                "db_id": item.get("db_id"),
                "question": item.get("question", ""),
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            failed += 1
            print(f"[{index + 1}/{len(questions)}] qid={question_id} status=failed "
                  f"reason={result['error']}")

        append_record(output_path, result)

    print(f"\nDone: ok={success} failed={failed} skipped={skipped} | {output_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
