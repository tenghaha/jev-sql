"""Schema-linking evaluation against BIRD gold SQL."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

try:
    import sqlparse
except ImportError:  # pragma: no cover - sqlparse is available in the project env
    sqlparse = None


def load_questions(path: str | Path) -> list[dict[str, Any]]:
    """Load the BIRD question JSON without importing the inference stack."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def split_sql(sql: str) -> list[str]:
    """Split a SQL string into executable statements."""
    statements = sqlparse.split(sql) if sqlparse is not None else sql.split(";")
    return [statement.strip().rstrip(";").strip() for statement in statements if statement.strip()]


def extract_sql_schema(
    sql: str,
    db_path: str | Path,
) -> tuple[dict[str, str], dict[tuple[str, str], tuple[str, str]], str | None]:
    """Read tables/columns actually used by a SQL statement via SQLite authorizer."""
    path = Path(db_path).resolve()
    if not path.exists():
        return {}, {}, f"Database not found: {path}"

    tables: dict[str, str] = {}
    columns: dict[tuple[str, str], tuple[str, str]] = {}
    errors: list[str] = []

    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)

    def authorizer(action: int, arg1: Any, arg2: Any, dbname: Any, trigger: Any) -> int:
        if action == sqlite3.SQLITE_READ and arg1:
            tables.setdefault(str(arg1).casefold(), str(arg1))
            if arg2:
                columns.setdefault(
                    (str(arg1).casefold(), str(arg2).casefold()),
                    (str(arg1), str(arg2)),
                )
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    try:
        for statement in split_sql(sql):
            try:
                conn.execute("EXPLAIN " + statement).fetchall()
            except sqlite3.Error as exc:
                errors.append(str(exc))
    finally:
        conn.close()

    if not tables and errors:
        return {}, {}, "; ".join(errors)
    return tables, columns, ("; ".join(errors) if errors else None)


def extract_predicted_schema(record: dict[str, Any]) -> tuple[dict[str, str], dict[tuple[str, str], tuple[str, str]]]:
    """Extract predicted table/column sets from a pipeline record."""
    pairs: list[tuple[str, str]] = []
    for item in record.get("linked_columns_union") or []:
        table, column = item.get("table"), item.get("column")
        if table and column:
            pairs.append((str(table), str(column)))
    if not pairs:
        for mention in record.get("linked_columns") or []:
            for item in mention.get("columns") or []:
                table, column = item.get("table"), item.get("column")
                if table and column:
                    pairs.append((str(table), str(column)))

    tables: dict[str, str] = {}
    columns: dict[tuple[str, str], tuple[str, str]] = {}
    for table, column in pairs:
        tables.setdefault(table.casefold(), table)
        columns.setdefault((table.casefold(), column.casefold()), (table, column))
    return tables, columns


def load_predictions(path: str | Path) -> tuple[list[dict[str, Any]], int]:
    """Load JSONL or JSON predictions; return records and line parse errors."""
    path = Path(path)
    if path.suffix.lower() == ".jsonl":
        records = []
        errors = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                errors += 1
        return records, errors

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return [data], 0
    if isinstance(data, list):
        return data, 0
    raise ValueError(f"Unsupported prediction format: {path}")


def _prf(tp: int, fp: int, fn: int) -> tuple[float | None, float | None, float | None]:
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    if precision is None or recall is None or precision + recall == 0:
        f1 = None if precision is None or recall is None else 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def _question_id(record: dict[str, Any], index: int) -> Any:
    return record.get("question_id", index)


def evaluate_schema_linking(
    questions: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    databases_dir: str | Path,
    dataset: str,
    split: str,
    include_missing: bool = False,
    parse_errors: int = 0,
) -> dict[str, Any]:
    """Evaluate predicted tables/columns against gold SQL."""
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
    missing = 0
    matched = 0
    prediction_failures = 0
    gt_errors = 0

    for question_id, (index, question) in questions_by_id.items():
        prediction = predictions_by_id.get(question_id)
        if prediction is None:
            missing += 1
            if not include_missing:
                continue
            prediction = {}
        else:
            matched += 1

        db_id = question.get("db_id")
        sql = question.get("SQL") or question.get("sql")
        db_path = Path(databases_dir) / str(db_id) / f"{db_id}.sqlite"

        if prediction.get("status") == "failed":
            prediction_failures += 1
            samples.append(
                {
                    "question_id": question_id,
                    "db_id": db_id,
                    "status": "prediction_failed",
                    "error": prediction.get("error"),
                }
            )
            continue

        if not sql or not db_id:
            gt_errors += 1
            samples.append(
                {
                    "question_id": question_id,
                    "db_id": db_id,
                    "status": "gt_error",
                    "error": "Missing SQL or db_id",
                }
            )
            continue

        gt_tables, gt_columns, gt_error = extract_sql_schema(sql, db_path)
        if gt_error:
            gt_errors += 1
            samples.append(
                {
                    "question_id": question_id,
                    "db_id": db_id,
                    "status": "gt_error",
                    "error": gt_error,
                }
            )
            continue

        pred_tables, pred_columns = extract_predicted_schema(prediction)
        gt_table_keys = set(gt_tables)
        pred_table_keys = set(pred_tables)
        gt_column_keys = set(gt_columns)
        pred_column_keys = set(pred_columns)

        table_tp = len(gt_table_keys & pred_table_keys)
        table_fp = len(pred_table_keys - gt_table_keys)
        table_fn = len(gt_table_keys - pred_table_keys)
        column_tp = len(gt_column_keys & pred_column_keys)
        column_fp = len(pred_column_keys - gt_column_keys)
        column_fn = len(gt_column_keys - pred_column_keys)

        table_precision, table_recall, table_f1 = _prf(table_tp, table_fp, table_fn)
        column_precision, column_recall, column_f1 = _prf(column_tp, column_fp, column_fn)
        table_em = gt_table_keys == pred_table_keys
        column_em = gt_column_keys == pred_column_keys

        samples.append(
            {
                "question_id": question_id,
                "db_id": db_id,
                "status": "ok",
                "prediction_missing": prediction == {},
                "gt_tables": sorted(gt_tables.values()),
                "pred_tables": sorted(pred_tables.values()),
                "gt_columns": sorted(f"{table}.{column}" for table, column in gt_columns.values()),
                "pred_columns": sorted(f"{table}.{column}" for table, column in pred_columns.values()),
                "table_tp": table_tp,
                "table_fp": table_fp,
                "table_fn": table_fn,
                "table_precision": table_precision,
                "table_recall": table_recall,
                "table_f1": table_f1,
                "column_tp": column_tp,
                "column_fp": column_fp,
                "column_fn": column_fn,
                "column_precision": column_precision,
                "column_recall": column_recall,
                "column_f1": column_f1,
                "table_exact_match": table_em,
                "column_exact_match": column_em,
                "strict_column_recall": int(column_fn == 0),
                "exact_match": table_em and column_em,
            }
        )

    evaluated = [sample for sample in samples if sample["status"] == "ok"]
    table_tp = sum(sample["table_tp"] for sample in evaluated)
    table_fp = sum(sample["table_fp"] for sample in evaluated)
    table_fn = sum(sample["table_fn"] for sample in evaluated)
    column_tp = sum(sample["column_tp"] for sample in evaluated)
    column_fp = sum(sample["column_fp"] for sample in evaluated)
    column_fn = sum(sample["column_fn"] for sample in evaluated)
    table_precision, table_recall, table_f1 = _prf(table_tp, table_fp, table_fn)
    column_precision, column_recall, column_f1 = _prf(column_tp, column_fp, column_fn)

    by_database: dict[str, dict[str, Any]] = {}
    for sample in evaluated:
        entry = by_database.setdefault(
            sample["db_id"],
            {
                "db_id": sample["db_id"],
                "samples": 0,
                "table_tp": 0,
                "table_fp": 0,
                "table_fn": 0,
                "column_tp": 0,
                "column_fp": 0,
                "column_fn": 0,
                "exact_match": 0,
            },
        )
        entry["samples"] += 1
        for key in ("table_tp", "table_fp", "table_fn", "column_tp", "column_fp", "column_fn"):
            entry[key] += sample[key]
        entry["exact_match"] += int(sample["exact_match"])

    database_rows = []
    for entry in sorted(by_database.values(), key=lambda item: item["db_id"]):
        entry["table_precision"], entry["table_recall"], _ = _prf(
            entry["table_tp"], entry["table_fp"], entry["table_fn"]
        )
        entry["column_precision"], entry["column_recall"], _ = _prf(
            entry["column_tp"], entry["column_fp"], entry["column_fn"]
        )
        entry["exact_match_rate"] = entry["exact_match"] / entry["samples"]
        database_rows.append(entry)

    total_questions = len(questions)
    evaluated_count = len(evaluated)
    summary = {
        "dataset": dataset,
        "split": split,
        "questions": total_questions,
        "predictions": len(predictions),
        "matched": matched,
        "missing": missing,
        "prediction_failures": prediction_failures,
        "gt_errors": gt_errors,
        "unmatched_predictions": len(
            [record for record in predictions if str(record.get("question_id")) not in questions_by_id]
        ),
        "duplicate_predictions": duplicate_predictions,
        "parse_errors": parse_errors,
        "evaluated": evaluated_count,
        "coverage": evaluated_count / total_questions if total_questions else None,
        "table": {
            "tp": table_tp,
            "fp": table_fp,
            "fn": table_fn,
            "precision": table_precision,
            "recall": table_recall,
            "f1": table_f1,
        },
        "column": {
            "tp": column_tp,
            "fp": column_fp,
            "fn": column_fn,
            "precision": column_precision,
            "recall": column_recall,
            "f1": column_f1,
        },
        "exact_match": (
            sum(int(sample["exact_match"]) for sample in evaluated) / evaluated_count
            if evaluated_count
            else None
        ),
        "table_exact_match": (
            sum(int(sample["table_exact_match"]) for sample in evaluated) / evaluated_count
            if evaluated_count
            else None
        ),
        "column_exact_match": (
            sum(int(sample["column_exact_match"]) for sample in evaluated) / evaluated_count
            if evaluated_count
            else None
        ),
        "srr": (
            sum(int(sample["strict_column_recall"]) for sample in evaluated) / evaluated_count
            if evaluated_count
            else None
        ),
        "by_database": database_rows,
    }
    return {"summary": summary, "samples": samples}


def _format_metric(value: Any) -> str:
    return "N/A" if value is None else f"{value:.4f}"


def format_summary(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        f"Dataset: {summary['dataset']} / {summary['split']}",
        f"Questions: {summary['questions']} | Matched: {summary['matched']} | "
        f"Missing: {summary['missing']} | Evaluated: {summary['evaluated']}",
        f"Evaluated coverage: {_format_metric(summary['coverage'])}",
        "",
        f"Table  P/R/F1: {_format_metric(summary['table']['precision'])} / "
        f"{_format_metric(summary['table']['recall'])} / {_format_metric(summary['table']['f1'])}",
        f"Column P/R/F1: {_format_metric(summary['column']['precision'])} / "
        f"{_format_metric(summary['column']['recall'])} / {_format_metric(summary['column']['f1'])}",
        f"EM: {_format_metric(summary['exact_match'])}",
        f"Table EM: {_format_metric(summary['table_exact_match'])} | "
        f"Column EM: {_format_metric(summary['column_exact_match'])}",
        f"Column SRR: {_format_metric(summary['srr'])}",
    ]
    return "\n".join(lines)


def write_report(
    result: dict[str, Any],
    report_path: str | Path,
    details_path: str | Path,
) -> tuple[Path, Path]:
    """Write a Markdown summary and a JSON details file."""
    summary = result["summary"]
    report_path = Path(report_path)
    details_path = Path(details_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    details_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Schema Linking Evaluation",
        "",
        f"- Dataset: {summary['dataset']} / {summary['split']}",
        f"- Questions: {summary['questions']}",
        f"- Predictions: {summary['predictions']}",
        f"- Matched predictions: {summary['matched']}",
        f"- Missing predictions: {summary['missing']}",
        f"- Prediction failures: {summary['prediction_failures']}",
        f"- GT extraction errors: {summary['gt_errors']}",
        f"- Unmatched predictions: {summary['unmatched_predictions']}",
        f"- Duplicate predictions: {summary['duplicate_predictions']}",
        f"- JSONL parse errors: {summary['parse_errors']}",
        f"- Evaluated samples: {summary['evaluated']}",
        f"- Evaluated coverage: {_format_metric(summary['coverage'])}",
        "",
        "## Main Metrics (micro)",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Table Precision | {_format_metric(summary['table']['precision'])} |",
        f"| Table Recall | {_format_metric(summary['table']['recall'])} |",
        f"| Table F1 | {_format_metric(summary['table']['f1'])} |",
        f"| Column Precision | {_format_metric(summary['column']['precision'])} |",
        f"| Column Recall | {_format_metric(summary['column']['recall'])} |",
        f"| Column F1 | {_format_metric(summary['column']['f1'])} |",
        f"| EM (table + column exact) | {_format_metric(summary['exact_match'])} |",
        f"| Table EM | {_format_metric(summary['table_exact_match'])} |",
        f"| Column EM | {_format_metric(summary['column_exact_match'])} |",
        f"| SRR (strict column recall) | {_format_metric(summary['srr'])} |",
        "",
        "## Definitions",
        "",
        "- Table P/R: set of tables read by the gold SQL vs predicted tables.",
        "- Column P/R: table-qualified columns read by the gold SQL vs predicted columns.",
        "- EM: 1 only when both the table set and the column set exactly match the gold SQL.",
        "- SRR (strict column recall): 1 when every gold column is recalled (extra predicted columns allowed), else 0; averaged over evaluated samples.",
        "- Micro precision/recall aggregate TP/FP/FN over all evaluated samples.",
        "",
        "## Per Database",
        "",
        "| db_id | samples | table P | table R | column P | column R | EM |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary["by_database"]:
        lines.append(
            f"| {row['db_id']} | {row['samples']} | "
            f"{_format_metric(row['table_precision'])} | "
            f"{_format_metric(row['table_recall'])} | "
            f"{_format_metric(row['column_precision'])} | "
            f"{_format_metric(row['column_recall'])} | "
            f"{_format_metric(row['exact_match_rate'])} |"
        )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    details_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path, details_path
