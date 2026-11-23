"""Run the step-1 ablation: LLM templates + JEV slot filling + rendered SQL.

Usage
-----
    python scripts/run_step1.py --config configs/step1_bird_dev.yaml --limit 5
    python scripts/run_step1.py --config configs/step1_bird_dev.yaml --limit 20 --offset 100
    python scripts/run_step1.py --config configs/step1_bird_dev.yaml --resume

Config fields (relative paths resolve from the repo root; artifacts from the
experiment directory):
    dataset, split, db_summary_path, schema_path, questions_path, databases_dir
    output_dir, num_templates
    template_llm: model_env / base_url_env / api_key_env / temperature / max_tokens
    jev:         model / endpoint_env / api_key_env / timeout
    fill:        max_rows

Output:
    <output_dir>/step1_records.jsonl   one record per question (appended)
    <output_dir>/step1_prompts.jsonl   the exact template-generation prompt per question
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slotpipe.config import load_config  # noqa: E402
from slotpipe.pipeline import load_db_summary, load_schema, run_question  # noqa: E402


def load_questions(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _done_ids(path: Path) -> set[str]:
    done = set()
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("status") == "ok":
            done.add(str(record.get("question_id")))
    return done


def _append(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Step-1 ablation runner.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--limit", type=int, default=0, help="first N questions")
    parser.add_argument("--offset", type=int, default=0, help="skip the first N")
    parser.add_argument("--resume", action="store_true", help="skip questions already ok")
    args = parser.parse_args()

    cfg = load_config(args.config)
    summaries = load_db_summary(cfg["db_summary_path"])
    schemas = load_schema(cfg["schema_path"])
    questions = load_questions(cfg["questions_path"])
    if args.offset:
        questions = questions[args.offset:]
    if args.limit > 0:
        questions = questions[:args.limit]

    out_dir = Path(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    records_path = out_dir / "step1_records.jsonl"
    prompts_path = out_dir / "step1_prompts.jsonl"
    done = _done_ids(records_path) if args.resume else set()

    ok = failed = skipped = 0
    for index, item in enumerate(questions):
        question_id = item.get("question_id", index)
        if args.resume and str(question_id) in done:
            skipped += 1
            continue
        try:
            result = run_question(item, summaries, schemas, cfg, question_id)
            result["status"] = "ok"
            _append(prompts_path, {"question_id": question_id,
                                   "db_id": result["db_id"],
                                   "prompts": result.pop("prompts", [])})
            ok += 1
            best = next((c for c in result["candidates"] if c["index"] == result["best"]), None)
            print(f"[{index + 1}/{len(questions)}] qid={question_id} db={result['db_id']} "
                  f"best={result['best']} exec={(best or {}).get('exec')} "
                  f"{result['elapsed_seconds']}s", flush=True)
            if best and best.get("sql"):
                print(f"    SQL: {best['sql'][:220]}", flush=True)
        except Exception as exc:  # noqa: BLE001 - keep the batch alive
            result = {"question_id": question_id, "db_id": item.get("db_id"),
                      "question": item.get("question", ""), "status": "failed",
                      "error": f"{type(exc).__name__}: {exc}"}
            failed += 1
            print(f"[{index + 1}/{len(questions)}] qid={question_id} FAILED "
                  f"{result['error'][:200]}", flush=True)
        _append(records_path, result)

    print(f"done: ok={ok} failed={failed} skipped={skipped} -> {records_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
