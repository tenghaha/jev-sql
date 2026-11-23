"""Dataset adapters: turn each gold-SQL split into a uniform record stream.

A *split* is one gold-SQL collection with a name, a loader kind and a default
dialect.  Every record produced here has the same shape, so the parser and the
slot annotator never need dataset-specific branching::

    {"id", "split", "db_id", "dialect", "sql", "question", "evidence", "difficulty"}

Supported loader kinds
----------------------
``bird_json``      BIRD ``dev.json`` / ``train.json`` / ``mini_dev_*.json``
                   (fields: question_id, db_id, question, evidence, SQL)
``spider2_dir``    Spider2 gold directory of ``<instance_id>.sql`` files plus a
                   JSONL task file that supplies the question text
``sql_lines``      a ``.sql`` file with one statement per line (legacy BIRD
                   ``dev.sql`` format: ``SQL<TAB>db_id``)

Dialect per instance
--------------------
``spider2_lite`` mixes dialects; the instance-id prefix decides which one
(``bq``/``ga`` -> bigquery, ``sf`` -> snowflake, ``lo``/``local`` -> sqlite).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

__all__ = ["Split", "load_splits", "iter_records", "dialect_for_instance"]


@dataclass
class Split:
    name: str
    kind: str
    dialect: str | None = None
    path: str | None = None
    sql_dir: str | None = None
    questions: str | None = None
    question_key: str = "question"
    dialect_by_prefix: dict[str, str] | None = None
    tables: str | None = None
    limit: int | None = None


def _join(root: str, p: str) -> str:
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(root, p))


def dialect_for_instance(instance_id: str, split: Split) -> str:
    if split.dialect_by_prefix:
        m = split.dialect_by_prefix
        # longest prefix wins so "local" beats "lo"
        for prefix in sorted(m, key=len, reverse=True):
            if instance_id.startswith(prefix):
                return m[prefix]
    return split.dialect or "sqlite"


def load_splits(config_path: str) -> tuple[list[Split], dict]:
    import yaml  # local import: PyYAML is only needed to read configs

    with open(config_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    exp_dir = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
    root = os.path.normpath(_join(exp_dir, cfg.get("repo_root", "..")))
    splits = []
    for raw in cfg["splits"]:
        splits.append(Split(
            name=raw["name"],
            kind=raw["kind"],
            dialect=raw.get("dialect"),
            path=_join(root, raw["path"]) if raw.get("path") else None,
            sql_dir=_join(root, raw["sql_dir"]) if raw.get("sql_dir") else None,
            questions=_join(root, raw["questions"]) if raw.get("questions") else None,
            question_key=raw.get("question_key", "question"),
            dialect_by_prefix=raw.get("dialect_by_prefix"),
            tables=_join(root, raw["tables"]) if raw.get("tables") else None,
            limit=raw.get("limit"),
        ))
    meta = {"repo_root": root, "exp_dir": exp_dir,
            "out_dir": _join(exp_dir, cfg.get("out_dir", "outputs")),
            "schema": cfg.get("schema", {})}
    return splits, meta


def iter_records(split: Split):
    """Yield uniform record dicts for one split."""
    if split.kind == "bird_json":
        for rec in _iter_bird_json(split):
            yield rec
    elif split.kind == "spider2_dir":
        for rec in _iter_spider2_dir(split):
            yield rec
    elif split.kind == "sql_lines":
        for rec in _iter_sql_lines(split):
            yield rec
    else:  # pragma: no cover - configuration error
        raise SystemExit(f"unknown split kind {split.kind!r}")


def _iter_bird_json(split: Split):
    with open(split.path, "r", encoding="utf-8") as fh:
        rows = json.load(fh)
    for i, row in enumerate(rows):
        if split.limit is not None and i >= split.limit:
            break
        yield {
            "id": str(row.get("question_id", i)),
            "split": split.name,
            "db_id": row.get("db_id"),
            "dialect": split.dialect or "sqlite",
            "sql": row["SQL"],
            "question": row.get("question"),
            "evidence": row.get("evidence"),
            "difficulty": row.get("difficulty"),
        }


def _iter_spider2_dir(split: Split):
    questions: dict[str, dict] = {}
    if split.questions and os.path.exists(split.questions):
        with open(split.questions, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                questions[row["instance_id"]] = row
    names = sorted(n for n in os.listdir(split.sql_dir) if n.endswith(".sql"))
    for i, name in enumerate(names):
        if split.limit is not None and i >= split.limit:
            break
        instance_id = name[:-4]
        with open(os.path.join(split.sql_dir, name), "r", encoding="utf-8") as fh:
            sql = fh.read()
        q = questions.get(instance_id, {})
        yield {
            "id": instance_id,
            "split": split.name,
            "db_id": q.get("db") or q.get("db_id"),
            "dialect": dialect_for_instance(instance_id, split),
            "sql": sql,
            "question": q.get(split.question_key),
            "evidence": q.get("external_knowledge"),
            "difficulty": None,
        }


def _iter_sql_lines(split: Split):
    with open(split.path, "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            sql, _, db_id = line.partition("\t")
            if split.limit is not None and i >= split.limit:
                break
            yield {
                "id": str(i),
                "split": split.name,
                "db_id": db_id or None,
                "dialect": split.dialect or "sqlite",
                "sql": sql,
                "question": None,
                "evidence": None,
                "difficulty": None,
            }
