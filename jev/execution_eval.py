"""Execution-based evaluation (EX) of generated SQL against BIRD gold SQL.

SQL results are compared as sets following the BIRD official ``evaluation.py``
(EX = the predicted and gold result sets are equal). Table/field precision and
recall are derived from the tables and columns the database engine actually
reads, extracted with a SQLite authorizer at ``EXPLAIN`` (prepare) time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from .evaluation import _prf, extract_sql_schema

logger = logging.getLogger(__name__)

PROGRESS_HANDLER_INTERVAL = 10000
LEVELS = ["simple", "moderate", "challenging"]


class SqlExecutionTimeout(Exception):
    """A SQL statement exceeded the execution timeout."""


def execute_sql(sql: str, db_path: str | Path, timeout: float) -> tuple[list, float]:
    """Execute one SQL statement, returning (rows, elapsed_seconds).

    A sqlite progress handler interrupts a runaway query so evaluation never hangs.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        deadline = time.perf_counter() + timeout

        def _on_progress() -> int:
            return 1 if time.perf_counter() > deadline else 0

        conn.set_progress_handler(_on_progress, PROGRESS_HANDLER_INTERVAL)
        cursor = conn.cursor()
        started = time.perf_counter()
        try:
            cursor.execute(sql)
            rows = cursor.fetchall()
        except sqlite3.OperationalError as exc:
            if time.perf_counter() >= deadline:
                raise SqlExecutionTimeout(f"Execution exceeded {timeout}s") from exc
            raise
        return rows, time.perf_counter() - started
    finally:
        conn.close()


def _schema_view(sql: str, db_path: str) -> tuple[list[str], list[str]]:
    """Tables and `Table.Column` fields read by a SQL statement (prepare only)."""
    tables, columns, _ = extract_sql_schema(sql, db_path)
    fields = sorted(f"{table}.{column}" for table, column in columns.values())
    return sorted(tables.values()), fields


def _evaluate_one(task: dict[str, Any]) -> dict[str, Any]:
    """Execute and score a single question (safe to run in a subprocess)."""
    result: dict[str, Any] = {
        "order": task["order"],
        "question_id": task["question_id"],
        "db_id": task["db_id"],
        "difficulty": task["difficulty"],
        "status": "ok",
        "ex": 0,
        "error": None,
        "gt_ok": False,
        "pred_sql": task["pred_sql"],
        "gold_sql": task["gt_sql"],
        "gt_tables": [],
        "pred_tables": [],
        "gt_fields": [],
        "pred_fields": [],
        "elapsed_ms": None,
        "num_rows": None,
    }

    # Schema extraction only prepares the statement, so it works even if execution later fails.
    result["gt_tables"], result["gt_fields"] = _schema_view(task["gt_sql"], task["db_path"])
    if task["pred_sql"]:
        result["pred_tables"], result["pred_fields"] = _schema_view(task["pred_sql"], task["db_path"])

    try:
        gt_rows, _ = execute_sql(task["gt_sql"], task["db_path"], task["timeout"])
    except SqlExecutionTimeout as exc:
        result.update(status="gt_timeout", error=str(exc))
        return result
    except Exception as exc:
        result.update(status="gt_error", error=f"{type(exc).__name__}: {exc}")
        return result

    # Gold executed: the question is scored, so a missing/failing prediction counts as wrong.
    result["gt_ok"] = True

    if not task["pred_sql"]:
        result["status"] = "empty_sql"
        return result

    try:
        pred_rows, pred_elapsed = execute_sql(task["pred_sql"], task["db_path"], task["timeout"])
    except SqlExecutionTimeout as exc:
        result.update(status="pred_timeout", error=str(exc))
        return result
    except Exception as exc:
        result.update(status="pred_error", error=f"{type(exc).__name__}: {exc}")
        return result

    result["elapsed_ms"] = pred_elapsed * 1000
    result["num_rows"] = len(pred_rows)
    result["ex"] = int(set(pred_rows) == set(gt_rows))
    return result


def _report_progress(done: int, total: int, enabled: bool) -> None:
    if not enabled or total < 20:
        return
    step = max(1, total // 10)
    if done % step == 0 or done == total:
        print(f">>> evaluation progress {done}/{total}")


def _run_tasks(tasks: list[dict[str, Any]], workers: int, progress: bool) -> list[dict[str, Any]]:
    if not tasks:
        return []
    results: list[dict[str, Any]] = []
    if workers <= 1:
        for index, task in enumerate(tasks, 1):
            results.append(_evaluate_one(task))
            _report_progress(index, len(tasks), progress)
        return results
    chunksize = max(1, len(tasks) // (workers * 4))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for index, item in enumerate(pool.map(_evaluate_one, tasks, chunksize=chunksize), 1):
            results.append(item)
            _report_progress(index, len(tasks), progress)
    return results


def _aggregate(items: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """Micro table/field precision and recall over items with a parseable gold schema."""
    gt_key, pred_key = f"gt_{kind}", f"pred_{kind}"
    tp = fp = fn = 0
    for item in items:
        gt, pred = set(item[gt_key]), set(item[pred_key])
        tp += len(gt & pred)
        fp += len(pred - gt)
        fn += len(gt - pred)
    precision, recall, f1 = _prf(tp, fp, fn)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def _group_metrics(group: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [item for item in group if item["gt_ok"]]
    schema_items = [item for item in group if item["gt_tables"]]
    table = _aggregate(schema_items, "tables")
    field = _aggregate(schema_items, "fields")
    elapsed = [item["elapsed_ms"] for item in group if item["elapsed_ms"] is not None]
    ex_correct = sum(item["ex"] for item in scored)
    return {
        "count": len(group),
        "scored": len(scored),
        "ex_count": ex_correct,
        "ex": ex_correct / len(scored) * 100 if scored else None,
        "schema_count": len(schema_items),
        "table_precision": table["precision"],
        "table_recall": table["recall"],
        "table_f1": table["f1"],
        "field_precision": field["precision"],
        "field_recall": field["recall"],
        "field_f1": field["f1"],
        "avg_elapsed_ms": sum(elapsed) / len(elapsed) if elapsed else None,
    }


def _question_id(record: dict[str, Any], index: int) -> Any:
    return record.get("question_id", index)


def evaluate_execution(
    questions: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    databases_dir: str | Path,
    dataset: str,
    split: str,
    timeout: float = 30.0,
    include_missing: bool = False,
    parse_errors: int = 0,
    workers: int = 1,
    progress: bool = True,
) -> dict[str, Any]:
    """Execute generated SQL and score EX plus table/field P/R against gold SQL."""
    questions_by_id = {
        str(_question_id(question, index)): (index, question)
        for index, question in enumerate(questions)
    }
    predictions_by_id: dict[str, dict[str, Any]] = {}
    duplicate_predictions = 0
    for record in predictions:
        key = str(record.get("question_id"))
        if key in predictions_by_id:
            duplicate_predictions += 1
        predictions_by_id[key] = record

    samples: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    missing = matched = generation_failures = gt_errors = db_errors = 0

    for order, (key, (index, question)) in enumerate(questions_by_id.items()):
        prediction = predictions_by_id.get(key)
        if prediction is None:
            missing += 1
            if not include_missing:
                continue
            prediction = {}
        else:
            matched += 1

        db_id = question.get("db_id") or prediction.get("db_id")
        gold_sql = (question.get("SQL") or question.get("sql") or "").strip()
        record = {
            "order": order,
            "question_id": question.get("question_id", index),
            "db_id": db_id,
            "difficulty": question.get("difficulty") or "unknown",
            "ex": 0,
            "error": None,
            "gt_ok": False,
            "gold_sql": gold_sql,
            "pred_sql": (prediction.get("pred_sql") or "").strip(),
            "gt_tables": [],
            "pred_tables": [],
            "gt_fields": [],
            "pred_fields": [],
            "elapsed_ms": None,
            "num_rows": None,
        }

        if prediction.get("status") == "failed":
            generation_failures += 1
            samples.append({**record, "status": "generation_failed", "error": prediction.get("error")})
            continue
        if not gold_sql or not db_id:
            gt_errors += 1
            samples.append({**record, "status": "missing_gt"})
            continue
        db_path = Path(databases_dir) / str(db_id) / f"{db_id}.sqlite"
        if not db_path.exists():
            db_errors += 1
            samples.append({**record, "status": "db_not_found"})
            continue
        tasks.append({**record, "gt_sql": gold_sql, "db_path": str(db_path), "timeout": timeout})

    details = sorted(samples + _run_tasks(tasks, workers, progress), key=lambda item: item["order"])
    for item in details:
        item.pop("order", None)

    difficulties = list(LEVELS)
    if any(item["difficulty"] not in LEVELS for item in details):
        difficulties.append("unknown")

    metrics: dict[str, Any] = {}
    for level in difficulties + ["total"]:
        group = details if level == "total" else [item for item in details if item["difficulty"] == level]
        if group:
            metrics[level] = _group_metrics(group)

    status_counts: dict[str, int] = {}
    for item in details:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1

    by_database: dict[str, list[dict[str, Any]]] = {}
    for item in details:
        by_database.setdefault(str(item["db_id"]), []).append(item)
    database_rows = []
    for db_id in sorted(by_database):
        entry = _group_metrics(by_database[db_id])
        entry["db_id"] = db_id
        database_rows.append(entry)

    total = metrics.get("total", {})
    summary = {
        "dataset": dataset,
        "split": split,
        "questions": len(questions),
        "predictions": len(predictions),
        "matched": matched,
        "missing": missing,
        "generation_failures": generation_failures,
        "gt_errors": gt_errors,
        "db_errors": db_errors,
        "duplicate_predictions": duplicate_predictions,
        "parse_errors": parse_errors,
        "scored": total.get("scored", 0),
        "coverage": total.get("scored", 0) / len(questions) if questions else None,
        "ex": total.get("ex"),
        "ex_count": total.get("ex_count", 0),
        "table": {
            "precision": total.get("table_precision"),
            "recall": total.get("table_recall"),
            "f1": total.get("table_f1"),
        },
        "field": {
            "precision": total.get("field_precision"),
            "recall": total.get("field_recall"),
            "f1": total.get("field_f1"),
        },
        "status_counts": status_counts,
        "by_database": database_rows,
    }
    return {"summary": summary, "metrics": metrics, "samples": details}


def _format_metric(value: Any) -> str:
    return "N/A" if value is None else f"{value:.4f}"


def _format_pct(value: Any) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def _format_rate(value: Any) -> str:
    """Format an already-percentage value (e.g. EX) with two decimals."""
    return "N/A" if value is None else f"{value:.2f}%"


def format_summary(result: dict[str, Any]) -> str:
    summary = result["summary"]
    return "\n".join(
        [
            f"Dataset: {summary['dataset']} / {summary['split']}",
            f"Questions: {summary['questions']} | Matched: {summary['matched']} | "
            f"Missing: {summary['missing']} | Scored: {summary['scored']}",
            f"Scored coverage: {_format_metric(summary['coverage'])}",
            "",
            f"EX: {_format_rate(summary['ex'])} ({summary['ex_count']}/{summary['scored']})",
            f"Table P/R/F1: {_format_metric(summary['table']['precision'])} / "
            f"{_format_metric(summary['table']['recall'])} / {_format_metric(summary['table']['f1'])}",
            f"Field P/R/F1: {_format_metric(summary['field']['precision'])} / "
            f"{_format_metric(summary['field']['recall'])} / {_format_metric(summary['field']['f1'])}",
        ]
    )


def write_report(result: dict[str, Any], report_path: str | Path) -> Path:
    """Write a Markdown report that mirrors the execution results JSON."""
    summary = result["summary"]
    metrics = result["metrics"]
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Execution Evaluation",
        "",
        f"- Dataset: {summary['dataset']} / {summary['split']}",
        f"- Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Questions: {summary['questions']}",
        f"- Predictions: {summary['predictions']}",
        f"- Matched predictions: {summary['matched']}",
        f"- Missing predictions: {summary['missing']}",
        f"- Generation failures: {summary['generation_failures']}",
        f"- GT extraction errors: {summary['gt_errors']}",
        f"- Missing databases: {summary['db_errors']}",
        f"- Scored samples: {summary['scored']}",
        f"- Scored coverage: {_format_metric(summary['coverage'])}",
        "",
        "## Main Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| EX (Execution Accuracy) | {_format_rate(summary['ex'])} |",
        f"| Table Precision (micro) | {_format_metric(summary['table']['precision'])} |",
        f"| Table Recall (micro) | {_format_metric(summary['table']['recall'])} |",
        f"| Table F1 (micro) | {_format_metric(summary['table']['f1'])} |",
        f"| Field Precision (micro) | {_format_metric(summary['field']['precision'])} |",
        f"| Field Recall (micro) | {_format_metric(summary['field']['recall'])} |",
        f"| Field F1 (micro) | {_format_metric(summary['field']['f1'])} |",
        "",
        "## By Difficulty",
        "",
        "| Difficulty | Questions | Scored | EX (%) | EX Count | Table P | Table R | Field P | Field R |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for level in LEVELS + ["unknown", "total"]:
        metric = metrics.get(level)
        if not metric:
            continue
        lines.append(
            f"| {level} | {metric['count']} | {metric['scored']} | "
            f"{_format_rate(metric['ex'])} | {metric['ex_count']} | "
            f"{_format_metric(metric['table_precision'])} | "
            f"{_format_metric(metric['table_recall'])} | "
            f"{_format_metric(metric['field_precision'])} | "
            f"{_format_metric(metric['field_recall'])} |"
        )

    lines += [
        "",
        "## Status Counts",
        "",
        "| Status | Questions |",
        "|---|---:|",
    ]
    for status, count in sorted(summary["status_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {status} | {count} |")

    lines += [
        "",
        "## Per Database",
        "",
        "| db_id | Questions | EX (%) | Table P | Table R | Field P | Field R |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary["by_database"]:
        lines.append(
            f"| {row['db_id']} | {row['count']} | {_format_rate(row['ex'])} | "
            f"{_format_metric(row['table_precision'])} | "
            f"{_format_metric(row['table_recall'])} | "
            f"{_format_metric(row['field_precision'])} | "
            f"{_format_metric(row['field_recall'])} |"
        )

    abnormal = [item for item in result["samples"] if item["status"] != "ok"]
    if abnormal:
        lines += [
            "",
            "## Abnormal Samples (first 20)",
            "",
            "| question_id | db_id | status | error |",
            "|---:|---|---|---|",
        ]
        for item in abnormal[:20]:
            error = (item.get("error") or "").replace("|", "\\|")
            lines.append(f"| {item['question_id']} | {item['db_id']} | {item['status']} | {error} |")

    lines += [
        "",
        "## Definitions",
        "",
        "- EX: 1 when the predicted and gold result sets are equal (BIRD official `evaluation.py`).",
        "- Table/Field P/R: tables and `Table.Column` fields actually read by the SQL, extracted with a",
        "  SQLite authorizer at `EXPLAIN` (prepare) time. Micro aggregates TP/FP/FN over all scored samples.",
        "- Scored samples: gold SQL executed successfully; a missing or failing prediction counts as wrong.",
        "- Abnormal samples (missing GT, unnamed database, gold execution error) are excluded from EX.",
        "",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def write_results(result: dict[str, Any], results_path: str | Path) -> Path:
    """Write the full execution results (summary, per-difficulty metrics, per-sample)."""
    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return results_path
