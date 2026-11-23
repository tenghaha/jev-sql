"""One question -> templates -> JEV slot filling -> SQL, for the step-1 ablation."""

from __future__ import annotations

import json
import os
import sqlite3
import time

from .fill import bind_column_slots, bind_table_slots, fill
from .render import RenderError, to_sql
from .template import generate_templates

__all__ = ["load_db_summary", "load_schema", "run_question"]


def load_db_summary(path: str) -> dict[str, dict]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return {db["db_id"]: db for db in data["databases"]}


def load_schema(path: str) -> dict[str, dict]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return {db["db_id"]: db for db in data["databases"]}


def run_question(record: dict, summaries: dict, schemas: dict, cfg: dict,
                 question_id=None) -> dict:
    """Run the step-1 pipeline for one question and return its record."""
    started = time.perf_counter()
    db_id = record["db_id"]
    question = record["question"]
    evidence = record.get("evidence") or ""
    question_id = record.get("question_id", question_id)

    summary = summaries.get(db_id)
    schema = schemas.get(db_id)
    if summary is None or schema is None:
        raise KeyError(f"database {db_id} missing from summary/schema artifacts")
    cards = summary["tables"]

    generated = generate_templates(cards, question, evidence, cfg)
    candidates = []
    for bundle in generated["templates"]:
        candidates.append(_fill_one(bundle, cards, schema, db_id, question,
                                    evidence, cfg, started))
    return {
        "question_id": question_id,
        "db_id": db_id,
        "question": question,
        "evidence": evidence,
        "prompts": generated.get("prompts"),

        "candidates": candidates,
        "best": _best_index(candidates),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }


def _fill_one(bundle: dict, cards: list[dict], schema: dict, db_id: str,
              question: str, evidence: str, cfg: dict, started: float) -> dict:
    out = {"index": bundle["index"], "kind": bundle.get("kind")}
    if "error" in bundle:
        out.update(status="template_failed", error=bundle["error"], sql=None)
        return out
    template = bundle["template"]
    values = bundle["values"]
    out.update(template=template, values=values, rounds=bundle.get("rounds"))

    tables = bind_table_slots(template, cards, db_id, question, evidence, cfg)
    out["tables"] = tables
    if not tables.get("ok"):
        out.update(sql=None, status="table_fill_failed")
        return out

    column_result = bind_column_slots(template, tables["selected"], schema, cards,
                                      db_id, question, evidence, cfg, values)
    columns = column_result
    out["columns"] = columns
    if not columns.get("ok"):
        out.update(sql=None, status="column_fill_failed")
        return out

    filled, unresolved = fill(template, values, tables["selected"],
                              columns["selected"])
    out["unresolved_sids"] = unresolved
    if unresolved:
        out.update(sql=None, status="unresolved_slots")
        return out

    sql = to_sql(filled)
    out["sql"] = sql
    out["filled_ast"] = filled
    out["exec"] = _execute(sql, cfg, db_id)
    out["status"] = "ok"
    out["fill_seconds"] = round(time.perf_counter() - started, 2)
    return out


def _execute(sql: str, cfg: dict, db_id: str) -> dict:
    """Run the statement read-only to record validity and the row count."""
    db_path = os.path.join(cfg["databases_dir"], db_id, f"{db_id}.sqlite")
    if not os.path.exists(db_path):
        return {"ok": None, "error": "database file not found"}
    limit = int((cfg.get("fill") or {}).get("max_rows", 1000))
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
        try:
            cur = con.execute(sql)
            rows = cur.fetchmany(limit + 1)
        finally:
            con.close()
        return {"ok": True, "rows": min(len(rows), limit),
                "truncated": len(rows) > limit}
    except Exception as exc:  # noqa: BLE001 - any SQL error is a signal here
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


def _best_index(candidates: list[dict]) -> int | None:
    """Prefer a runnable candidate with at least one row, else any runnable one."""
    for want_rows in (True, False):
        for cand in candidates:
            exec_ = cand.get("exec") or {}
            if exec_.get("ok") and (not want_rows or (exec_.get("rows") or 0) > 0):
                return cand["index"]
    return None
