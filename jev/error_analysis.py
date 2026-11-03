"""Associate schema-linking errors with pipeline answers and build an analysis report."""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

from jev.evaluation import load_predictions


def _norm(value: Any) -> str:
    return str(value).casefold()


def _kind(fp: int, fn: int) -> str:
    if fp and fn:
        return "mixed"
    if fp:
        return "over"
    if fn:
        return "under"
    return "exact"


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1%}"


def _metric(value: Any) -> str:
    return "N/A" if value is None else f"{value:.4f}"


def _fmt_list(values: list[Any], limit: int = 15) -> str:
    if not values:
        return "-"
    shown = [f"`{value}`" for value in values[:limit]]
    text = ", ".join(shown)
    if len(values) > limit:
        text += f" ... (+{len(values) - limit})"
    return text


def _score_bucket(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score >= 3.0:
        return "3.0"
    if score >= 2.5:
        return "2.5-3.0"
    if score >= 2.0:
        return "2.0-2.5"
    return "<2.0"


def _stage2_column_scores(
    stage2_answers: dict[str, Any],
    candidate_id: str,
    mention_map: dict[str, dict[str, Any]],
    threshold: float,
) -> tuple[list[dict[str, Any]], str, float | None]:
    scores = []
    prefix = "s2_"
    for key, answer in (stage2_answers or {}).items():
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix):].split("_", 1)
        if len(parts) != 2 or parts[1] != candidate_id:
            continue
        score = answer.get("score")
        if not isinstance(score, (int, float)):
            continue
        span_id = parts[0]
        mention = mention_map.get(span_id) or {}
        scores.append(
            {
                "span_id": span_id,
                "text": mention.get("text", ""),
                "span_type": mention.get("span_type"),
                "score": float(score),
                "confidence": answer.get("confidence"),
            }
        )
    scores.sort(key=lambda item: item["score"], reverse=True)
    if not scores:
        return [], "no_answer", None
    best = scores[0]["score"]
    reason = "below_threshold" if best < threshold else "selected_but_missing"
    return scores, reason, best


def _extra_column_diagnostic(
    column: str,
    linked_columns: list[dict[str, Any]],
    linked_union: list[dict[str, Any]],
) -> dict[str, Any]:
    key = _norm(column)
    links = []
    for mention in linked_columns or []:
        for item in mention.get("columns") or []:
            column_key = _norm(f"{item.get('table')}.{item.get('column')}")
            if column_key != key:
                continue
            links.append(
                {
                    "span_id": mention.get("span_id"),
                    "text": mention.get("text"),
                    "span_type": mention.get("span_type"),
                    "score": item.get("score"),
                    "confidence": item.get("confidence"),
                }
            )
    union_item = next(
        (
            item
            for item in linked_union or []
            if _norm(f"{item.get('table')}.{item.get('column')}") == key
        ),
        None,
    )
    scores = [link["score"] for link in links if isinstance(link.get("score"), (int, float))]
    best_score = union_item.get("best_score") if union_item else (max(scores) if scores else None)
    return {
        "column": column,
        "best_score": best_score,
        "score_bucket": _score_bucket(best_score),
        "mentions": links,
        "union_mentions": union_item.get("mentions", []) if union_item else [],
    }


def _columns_for_table(columns: list[str], table: str) -> list[str]:
    prefix = _norm(table) + "."
    return [column for column in columns if _norm(column).startswith(prefix)]


def build_cases(
    eval_result: dict[str, Any],
    questions_by_id: dict[str, dict[str, Any]],
    predictions_by_id: dict[str, dict[str, Any]],
    threshold: float,
) -> list[dict[str, Any]]:
    cases = []
    for sample in eval_result.get("samples") or []:
        question_id = str(sample.get("question_id"))
        question = questions_by_id.get(question_id) or {}
        prediction = predictions_by_id.get(question_id) or {}
        case: dict[str, Any] = {
            "question_id": sample.get("question_id"),
            "db_id": sample.get("db_id"),
            "status": sample.get("status"),
            "error": sample.get("error"),
            "prediction_missing": not prediction,
            "question": question.get("question") or prediction.get("question"),
            "evidence": question.get("evidence") or prediction.get("evidence"),
            "gold_sql": question.get("SQL") or question.get("sql"),
        }
        if sample.get("status") != "ok":
            case["error_kind"] = sample.get("status")
            cases.append(case)
            continue

        gt_tables = list(sample.get("gt_tables") or [])
        pred_tables = list(sample.get("pred_tables") or [])
        gt_columns = list(sample.get("gt_columns") or [])
        pred_columns = list(sample.get("pred_columns") or [])
        gt_table_map = {_norm(name): name for name in gt_tables}
        pred_table_map = {_norm(name): name for name in pred_tables}
        gt_column_map = {_norm(name): name for name in gt_columns}
        pred_column_map = {_norm(name): name for name in pred_columns}
        missing_tables = [gt_table_map[key] for key in sorted(set(gt_table_map) - set(pred_table_map))]
        extra_tables = [pred_table_map[key] for key in sorted(set(pred_table_map) - set(gt_table_map))]
        missing_columns = [gt_column_map[key] for key in sorted(set(gt_column_map) - set(pred_column_map))]
        extra_columns = [pred_column_map[key] for key in sorted(set(pred_column_map) - set(gt_column_map))]

        mentions = prediction.get("mentions") or []
        mention_map = {item.get("span_id"): item for item in mentions}
        stage1 = prediction.get("stage1") or {}
        stage2 = prediction.get("stage2") or {}
        selected = stage1.get("selected_candidates") or []
        selected_by_key = {
            _norm(f"{item.get('table')}.{item.get('column')}"): item
            for item in selected
            if item.get("table") and item.get("column")
        }
        stage2_answers = stage2.get("answers") or {}
        linked_columns = prediction.get("linked_columns") or []
        linked_union = prediction.get("linked_columns_union") or []

        missing_column_diagnostics = []
        for column in missing_columns:
            selected_item = selected_by_key.get(_norm(column))
            if selected_item is None:
                diagnostics = {
                    "column": column,
                    "reason": "not_in_stage1",
                    "candidate_id": None,
                    "noul": None,
                    "best_score": None,
                    "scores": [],
                }
            else:
                scores, reason, best_score = _stage2_column_scores(
                    stage2_answers,
                    selected_item.get("candidate_id"),
                    mention_map,
                    threshold,
                )
                diagnostics = {
                    "column": column,
                    "reason": reason,
                    "candidate_id": selected_item.get("candidate_id"),
                    "noul": selected_item.get("noul"),
                    "best_score": best_score,
                    "scores": scores,
                }
            missing_column_diagnostics.append(diagnostics)

        extra_column_diagnostics = [
            _extra_column_diagnostic(column, linked_columns, linked_union)
            for column in extra_columns
        ]

        case.update(
            {
                "error_kind": _kind(
                    int(sample.get("table_fp", 0)) + int(sample.get("column_fp", 0)),
                    int(sample.get("table_fn", 0)) + int(sample.get("column_fn", 0)),
                ),
                "table_error": _kind(int(sample.get("table_fp", 0)), int(sample.get("table_fn", 0))),
                "column_error": _kind(int(sample.get("column_fp", 0)), int(sample.get("column_fn", 0))),
                "gt_tables": gt_tables,
                "pred_tables": pred_tables,
                "missing_tables": missing_tables,
                "extra_tables": extra_tables,
                "gt_columns": gt_columns,
                "pred_columns": pred_columns,
                "missing_columns": missing_columns,
                "extra_columns": extra_columns,
                "table_tp": sample.get("table_tp"),
                "table_fp": sample.get("table_fp"),
                "table_fn": sample.get("table_fn"),
                "column_tp": sample.get("column_tp"),
                "column_fp": sample.get("column_fp"),
                "column_fn": sample.get("column_fn"),
                "table_precision": sample.get("table_precision"),
                "table_recall": sample.get("table_recall"),
                "column_precision": sample.get("column_precision"),
                "column_recall": sample.get("column_recall"),
                "exact_match": sample.get("exact_match"),
                "mentions": mentions,
                "stage1_selected": selected,
                "stage2_answers": stage2_answers,
                "linked_columns": linked_columns,
                "linked_columns_union": linked_union,
                "missing_column_diagnostics": missing_column_diagnostics,
                "extra_column_diagnostics": extra_column_diagnostics,
                "missing_table_diagnostics": [
                    {
                        "table": table,
                        "missing_columns": _columns_for_table(missing_columns, table),
                    }
                    for table in missing_tables
                ],
                "extra_table_diagnostics": [
                    {
                        "table": table,
                        "extra_columns": _columns_for_table(extra_columns, table),
                    }
                    for table in extra_tables
                ],
            }
        )
        cases.append(case)
    return cases


def summarize_cases(
    cases: list[dict[str, Any]],
    eval_summary: dict[str, Any],
    sample_size: int,
    seed: int,
) -> dict[str, Any]:
    evaluated = [case for case in cases if case.get("status") == "ok"]
    table_counts = Counter(case.get("table_error") for case in evaluated)
    column_counts = Counter(case.get("column_error") for case in evaluated)
    overall_counts = Counter(case.get("error_kind") for case in evaluated)
    missing_attribution = Counter(
        diagnostic.get("reason")
        for case in evaluated
        for diagnostic in case.get("missing_column_diagnostics") or []
    )
    extra_buckets = Counter(
        diagnostic.get("score_bucket")
        for case in evaluated
        for diagnostic in case.get("extra_column_diagnostics") or []
    )
    top_missing = Counter(
        column for case in evaluated for column in case.get("missing_columns") or []
    ).most_common(10)
    top_extra = Counter(
        column for case in evaluated for column in case.get("extra_columns") or []
    ).most_common(10)

    per_database: dict[str, dict[str, Any]] = {}
    for case in evaluated:
        db_id = case.get("db_id")
        entry = per_database.setdefault(
            db_id,
            {
                "db_id": db_id,
                "samples": 0,
                "table_fp": 0,
                "table_fn": 0,
                "column_fp": 0,
                "column_fn": 0,
                "table_error_counts": Counter(),
                "column_error_counts": Counter(),
                "exact": 0,
            },
        )
        entry["samples"] += 1
        entry["table_fp"] += int(case.get("table_fp", 0))
        entry["table_fn"] += int(case.get("table_fn", 0))
        entry["column_fp"] += int(case.get("column_fp", 0))
        entry["column_fn"] += int(case.get("column_fn", 0))
        entry["table_error_counts"][case.get("table_error")] += 1
        entry["column_error_counts"][case.get("column_error")] += 1
        entry["exact"] += int(bool(case.get("exact_match")))

    metrics_by_db = {row.get("db_id"): row for row in eval_summary.get("by_database") or []}
    database_rows = []
    for db_id, entry in sorted(per_database.items(), key=lambda item: str(item[0])):
        metrics = metrics_by_db.get(db_id) or {}
        entry = dict(entry)
        entry["table_error_counts"] = dict(entry["table_error_counts"])
        entry["column_error_counts"] = dict(entry["column_error_counts"])
        entry["table_precision"] = metrics.get("table_precision")
        entry["table_recall"] = metrics.get("table_recall")
        entry["column_precision"] = metrics.get("column_precision")
        entry["column_recall"] = metrics.get("column_recall")
        entry["exact_match_rate"] = metrics.get("exact_match_rate")
        entry["avg_table_fp"] = entry["table_fp"] / entry["samples"]
        entry["avg_table_fn"] = entry["table_fn"] / entry["samples"]
        entry["avg_column_fp"] = entry["column_fp"] / entry["samples"]
        entry["avg_column_fn"] = entry["column_fn"] / entry["samples"]
        database_rows.append(entry)

    return {
        "dataset": eval_summary.get("dataset"),
        "split": eval_summary.get("split"),
        "evaluated": len(evaluated),
        "errors": sum(1 for case in evaluated if case.get("error_kind") != "exact"),
        "table_error_counts": dict(table_counts),
        "column_error_counts": dict(column_counts),
        "overall_error_counts": dict(overall_counts),
        "avg_table_fp": _mean([float(case.get("table_fp", 0)) for case in evaluated]),
        "avg_table_fn": _mean([float(case.get("table_fn", 0)) for case in evaluated]),
        "avg_column_fp": _mean([float(case.get("column_fp", 0)) for case in evaluated]),
        "avg_column_fn": _mean([float(case.get("column_fn", 0)) for case in evaluated]),
        "missing_column_attribution": dict(missing_attribution),
        "extra_column_score_buckets": dict(extra_buckets),
        "top_missing_columns": top_missing,
        "top_extra_columns": top_extra,
        "per_database": database_rows,
        "skipped": dict(Counter(case.get("status") for case in cases if case.get("status") != "ok")),
        "sample_size": sample_size,
        "seed": seed,
    }


def sample_error_cases(cases: list[dict[str, Any]], sample_size: int, seed: int) -> list[dict[str, Any]]:
    errors = [
        case
        for case in cases
        if case.get("status") == "ok" and case.get("error_kind") != "exact"
    ]
    if not errors or sample_size <= 0:
        return []

    rng = random.Random(seed)
    pools = {
        kind: [case for case in errors if case.get("error_kind") == kind]
        for kind in ("mixed", "under", "over")
    }
    kinds = [kind for kind in ("mixed", "under", "over") if pools[kind]]
    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[Any, Any]] = set()
    base = sample_size // max(1, len(kinds))
    for kind in kinds:
        take = min(base, len(pools[kind]))
        for case in rng.sample(pools[kind], take):
            selected.append(case)
            selected_keys.add((case.get("question_id"), case.get("db_id")))

    remaining = sample_size - len(selected)
    if remaining > 0:
        rest = [
            case
            for case in errors
            if (case.get("question_id"), case.get("db_id")) not in selected_keys
        ]
        selected.extend(rng.sample(rest, min(remaining, len(rest))))

    selected.sort(
        key=lambda case: (
            int(case.get("table_fp", 0))
            + int(case.get("table_fn", 0))
            + int(case.get("column_fp", 0))
            + int(case.get("column_fn", 0)),
            str(case.get("question_id")),
        ),
        reverse=True,
    )
    return selected


def write_cases_jsonl(cases: list[dict[str, Any]], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    return path


def _render_scores(diagnostic: dict[str, Any], limit: int = 5) -> str:
    scores = diagnostic.get("scores") or []
    if not scores:
        return ""
    shown = ", ".join(
        f"`{item.get('span_id')}` {item.get('score'):.2f}" for item in scores[:limit]
    )
    if len(scores) > limit:
        shown += f" ... (+{len(scores) - limit})"
    return f"; scores: {shown}"


def _render_case(index: int, case: dict[str, Any]) -> list[str]:
    question_id = str(case.get("question_id"))
    question_label = question_id if question_id.startswith("q") else f"q{question_id}"
    mentions_text = _fmt_list(
        [
            f"{item.get('span_id')} {item.get('text')} ({item.get('span_type')})"
            for item in case.get("mentions") or []
        ]
    )
    selected_text = _fmt_list(
        [
            f"{item.get('candidate_id')} {item.get('table')}.{item.get('column')} ({item.get('noul')})"
            for item in case.get("stage1_selected") or []
        ],
        limit=12,
    )
    lines = [
        f"### Case {index}: {question_label} | {case.get('db_id')}",
        "",
        f"- 错误类型: `{case.get('error_kind')}`（表: `{case.get('table_error')}`，列: `{case.get('column_error')}`）",
        f"- 问题: {case.get('question') or '-'}",
        f"- Evidence: {case.get('evidence') or '-'}",
        f"- Table P/R: {_metric(case.get('table_precision'))} / {_metric(case.get('table_recall'))}；"
        f"Column P/R: {_metric(case.get('column_precision'))} / {_metric(case.get('column_recall'))}",
        f"- GT tables: {_fmt_list(case.get('gt_tables') or [])}",
        f"- Pred tables: {_fmt_list(case.get('pred_tables') or [])}",
        f"- 多选表（FP）: {_fmt_list(case.get('extra_tables') or [])}",
        f"- 少选表（FN）: {_fmt_list(case.get('missing_tables') or [])}",
        f"- GT columns: {_fmt_list(case.get('gt_columns') or [])}",
        f"- Pred columns: {_fmt_list(case.get('pred_columns') or [])}",
        f"- 多选列（FP）: {_fmt_list(case.get('extra_columns') or [])}",
        f"- 少选列（FN）: {_fmt_list(case.get('missing_columns') or [])}",
        f"- Mentions: {mentions_text}",
        f"- Stage 1 selected: {selected_text}",
    ]

    missing_diagnostics = case.get("missing_column_diagnostics") or []
    if missing_diagnostics:
        lines.extend(["", "**少选列归因**", ""])
        for diagnostic in missing_diagnostics[:10]:
            reason = diagnostic.get("reason")
            candidate = diagnostic.get("candidate_id")
            best_score = diagnostic.get("best_score")
            detail = f"- `{diagnostic.get('column')}`: `{reason}`"
            if candidate:
                detail += f"，candidate `{candidate}`"
            if best_score is not None:
                detail += f"，best score {best_score:.2f}"
            detail += _render_scores(diagnostic)
            lines.append(detail)
        if len(missing_diagnostics) > 10:
            lines.append(f"- ... (+{len(missing_diagnostics) - 10})")

    extra_diagnostics = case.get("extra_column_diagnostics") or []
    if extra_diagnostics:
        lines.extend(["", "**多选列归因**", ""])
        for diagnostic in extra_diagnostics[:10]:
            best_score = diagnostic.get("best_score")
            detail = f"- `{diagnostic.get('column')}`"
            if best_score is not None:
                detail += f": best score {best_score:.2f}"
            mentions = diagnostic.get("mentions") or []
            if mentions:
                shown = ", ".join(
                    f"`{item.get('span_id')}` {item.get('score'):.2f}"
                    for item in mentions[:5]
                    if isinstance(item.get("score"), (int, float))
                )
                if shown:
                    detail += f"；linked by {shown}"
            lines.append(detail)
        if len(extra_diagnostics) > 10:
            lines.append(f"- ... (+{len(extra_diagnostics) - 10})")

    lines.extend(
        [
            "",
            "**Gold SQL**",
            "",
            "```sql",
            str(case.get("gold_sql") or "").strip(),
            "```",
            "",
        ]
    )
    return lines


def write_markdown_report(
    summary: dict[str, Any],
    sampled: list[dict[str, Any]],
    eval_summary: dict[str, Any],
    report_path: str | Path,
    cases_path: str | Path,
) -> Path:
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    skipped = summary.get("skipped") or {}
    skipped_text = ", ".join(f"{key}: {value}" for key, value in skipped.items()) or "无"
    lines = [
        "# Schema Linking 错误分析",
        "",
        f"- 数据集: {summary.get('dataset')} / {summary.get('split')}",
        f"- 可评测样本: {summary.get('evaluated')}",
        f"- 错例样本: {summary.get('errors')}",
        f"- 跳过样本: {skipped_text}",
        f"- 抽样数量: {len(sampled)}（sample_size={summary.get('sample_size')}, seed={summary.get('seed')}）",
        f"- 中间关联文件: `{cases_path}`",
        "",
        "- 多选（FP）：预测包含 GT 没有的表/列。",
        "- 少选（FN）：GT 包含但预测缺失的表/列。",
        "",
        "## 总体指标",
        "",
        "| 指标 | 值 |",
        "|---|---:|",
        f"| Table Precision | {_metric(eval_summary.get('table', {}).get('precision'))} |",
        f"| Table Recall | {_metric(eval_summary.get('table', {}).get('recall'))} |",
        f"| Column Precision | {_metric(eval_summary.get('column', {}).get('precision'))} |",
        f"| Column Recall | {_metric(eval_summary.get('column', {}).get('recall'))} |",
        f"| EM | {_metric(eval_summary.get('exact_match'))} |",
        "",
        "## 错误类型分布",
        "",
        "| 空间 | 完全匹配 | 多选（FP） | 少选（FN） | 多选+少选 |",
        "|---|---:|---:|---:|---:|",
    ]

    evaluated = max(1, int(summary.get("evaluated") or 0))

    def count_text(counts: dict[str, int], key: str) -> str:
        value = int(counts.get(key, 0))
        return f"{value} ({value / evaluated:.1%})"

    for label, counts in (
        ("表", summary.get("table_error_counts") or {}),
        ("列", summary.get("column_error_counts") or {}),
        ("总体（表+列）", summary.get("overall_error_counts") or {}),
    ):
        lines.append(
            f"| {label} | {count_text(counts, 'exact')} | {count_text(counts, 'over')} | "
            f"{count_text(counts, 'under')} | {count_text(counts, 'mixed')} |"
        )

    lines.extend(
        [
            "",
            "## 平均误差数量",
            "",
            "| 空间 | 平均 FP | 平均 FN |",
            "|---|---:|---:|",
            f"| 表 | {summary.get('avg_table_fp', 0.0):.2f} | {summary.get('avg_table_fn', 0.0):.2f} |",
            f"| 列 | {summary.get('avg_column_fp', 0.0):.2f} | {summary.get('avg_column_fn', 0.0):.2f} |",
            "",
            "## 少选列归因",
            "",
            "| 归因 | 数量 | 占少选列 |",
            "|---|---:|---:|",
        ]
    )
    attribution = summary.get("missing_column_attribution") or {}
    attribution_total = max(1, sum(attribution.values()))
    reason_labels = {
        "not_in_stage1": "未进入 Stage 1 top-a",
        "below_threshold": "Stage 2 Score 低于阈值",
        "no_answer": "缺少 Stage 2 答案",
        "selected_but_missing": "已选中且过阈值但未进入最终结果",
    }
    for reason, label in reason_labels.items():
        value = int(attribution.get(reason, 0))
        lines.append(f"| {label} | {value} | {value / attribution_total:.1%} |")

    lines.extend(
        [
            "",
            "## 多选列 Score 分布",
            "",
            "| best score | 数量 |",
            "|---|---:|",
        ]
    )
    buckets = summary.get("extra_column_score_buckets") or {}
    for bucket in ("3.0", "2.5-3.0", "2.0-2.5", "<2.0", "unknown"):
        lines.append(f"| {bucket} | {int(buckets.get(bucket, 0))} |")

    lines.extend(["", "## 高频错误列", "", "| 类型 | 列 | 次数 |", "|---|---|---:|"])
    for column, count in summary.get("top_missing_columns") or []:
        lines.append(f"| 少选 | `{column}` | {count} |")
    for column, count in summary.get("top_extra_columns") or []:
        lines.append(f"| 多选 | `{column}` | {count} |")

    lines.extend(
        [
            "",
            "## 按数据库",
            "",
            "表/列 O/U/M 分别表示多选/少选/多选+少选。",
            "",
            "| db_id | samples | Table P | Table R | Column P | Column R | EM | 表 O/U/M | 列 O/U/M |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in summary.get("per_database") or []:
        table_counts = row.get("table_error_counts") or {}
        column_counts = row.get("column_error_counts") or {}
        lines.append(
            f"| {row.get('db_id')} | {row.get('samples')} | "
            f"{_metric(row.get('table_precision'))} | {_metric(row.get('table_recall'))} | "
            f"{_metric(row.get('column_precision'))} | {_metric(row.get('column_recall'))} | "
            f"{_metric(row.get('exact_match_rate'))} | "
            f"{table_counts.get('over', 0)}/{table_counts.get('under', 0)}/{table_counts.get('mixed', 0)} | "
            f"{column_counts.get('over', 0)}/{column_counts.get('under', 0)}/{column_counts.get('mixed', 0)} |"
        )

    table_counts = summary.get("table_error_counts") or {}
    column_counts = summary.get("column_error_counts") or {}
    lines.extend(
        [
            "",
            "## 总体分析",
            "",
            f"- 可评测样本 {summary.get('evaluated')} 个，EM {_metric(eval_summary.get('exact_match'))}，"
            f"表完全匹配 {count_text(table_counts, 'exact')}，列完全匹配 {count_text(column_counts, 'exact')}。",
            f"- 表错误：多选 {table_counts.get('over', 0)}、少选 {table_counts.get('under', 0)}、"
            f"多选+少选 {table_counts.get('mixed', 0)}；列错误：多选 {column_counts.get('over', 0)}、"
            f"少选 {column_counts.get('under', 0)}、多选+少选 {column_counts.get('mixed', 0)}。",
        ]
    )
    if sum(attribution.values()) > 0:
        lines.append(
            f"- 少选列共 {sum(attribution.values())} 个，其中未进入 Stage 1 top-a "
            f"{attribution.get('not_in_stage1', 0)} 个，Score 低于阈值 {attribution.get('below_threshold', 0)} 个，"
            f"缺少 Stage 2 答案 {attribution.get('no_answer', 0)} 个。"
        )
    extra_total = sum(buckets.values())
    if extra_total:
        high_conf = int(buckets.get("3.0", 0)) + int(buckets.get("2.5-3.0", 0))
        lines.append(
            f"- 多选列共 {extra_total} 个，其中 best score ≥ 2.5 的 {high_conf} 个"
            f"（{high_conf / extra_total:.1%}），说明部分 FP 来自高分误判而非低阈值噪声。"
        )

    db_rows = sorted(
        [row for row in summary.get("per_database") or [] if row.get("samples", 0) >= 5],
        key=lambda row: (row.get("exact_match_rate") is None, row.get("exact_match_rate") or 0.0),
    )
    if db_rows:
        worst = ", ".join(
            f"`{row.get('db_id')}` {_pct(row.get('exact_match_rate'))} ({row.get('samples')})"
            for row in db_rows[:3]
        )
        lines.append(f"- EM 最低的数据库（样本数 ≥ 5）：{worst}。")

    if summary.get("top_missing_columns"):
        top_missing = ", ".join(
            f"`{column}` ×{count}" for column, count in summary.get("top_missing_columns")[:5]
        )
        lines.append(f"- 高频少选列：{top_missing}。")

    lines.extend(["", "## 错例抽样详细分析", ""])
    if not sampled:
        lines.append("没有可抽样的错例。")
    else:
        for index, case in enumerate(sampled, start=1):
            lines.extend(_render_case(index, case))
            lines.append("---")
            lines.append("")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path
