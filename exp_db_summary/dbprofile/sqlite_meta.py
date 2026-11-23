"""Deterministic SQLite metadata: tables, columns, keys, foreign keys, samples.

Everything here comes from PRAGMA statements (plus ``SELECT COUNT(*)`` and a
``LIMIT n`` sample), i.e. it is reproducible without any model.
"""

from __future__ import annotations

import sqlite3

__all__ = ["open_db", "list_tables", "table_columns", "foreign_keys",
           "row_count", "sample_rows", "profile_table", "short_value"]

_MAX_VALUE_CHARS = 28


def open_db(path: str) -> sqlite3.Connection:
    # read-only: the datasets must never be modified
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    return con


def _q(name: str) -> str:
    """Quote an identifier for use in a PRAGMA / statement."""
    return '"' + str(name).replace('"', '""') + '"'


def list_tables(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def table_columns(con: sqlite3.Connection, table: str) -> list[dict]:
    out = []
    for cid, name, type_, notnull, default, pk in con.execute(
            f"PRAGMA table_info({_q(table)})"):
        out.append({"name": name, "type": (type_ or "").upper(),
                    "notnull": bool(notnull), "pk": bool(pk)})
    return out


def primary_key_columns(con: sqlite3.Connection, table: str) -> list[str]:
    rows = con.execute(f"PRAGMA table_info({_q(table)})").fetchall()
    return [name for _cid, name, _t, _nn, _d, pk in sorted(rows, key=lambda r: r[5])
            if pk]


def foreign_keys(con: sqlite3.Connection, table: str) -> list[dict]:
    """Group ``PRAGMA foreign_key_list`` rows into one entry per constraint.

    SQLite allows ``REFERENCES parent`` without naming the target column; the FK
    then points at the parent's primary key.  We resolve it so that no
    ``table.None`` pair ever reaches the artifacts.
    """
    rows = con.execute(f"PRAGMA foreign_key_list({_q(table)})").fetchall()
    pk_cache: dict[str, list[str]] = {}
    grouped: dict[tuple[int, str], dict] = {}
    for row in rows:
        fk_id, seq, ref_table, from_col, to_col, on_update, on_delete, _match = row
        if to_col is None:
            if ref_table not in pk_cache:
                pk_cache[ref_table] = primary_key_columns(con, ref_table)
            pks = pk_cache[ref_table]
            to_col = pks[seq] if seq < len(pks) else None
        entry = grouped.setdefault(
            (fk_id, ref_table),
            {"to_table": ref_table, "pairs": [], "on_delete": on_delete,
             "on_update": on_update})
        entry["pairs"].append([from_col, to_col])
    return [grouped[k] for k in sorted(grouped)]


def row_count(con: sqlite3.Connection, table: str,
              cap: int = 200_000) -> tuple[int | None, bool]:
    """Row count bounded by ``cap`` so a huge table cannot stall a full run.

    Returns ``(count, capped)``; ``capped`` means the table has at least ``cap``
    rows (the exact number is not needed for a compressed table card).
    """
    try:
        n = int(con.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {_q(table)} LIMIT {cap + 1})"
        ).fetchone()[0])
    except sqlite3.Error:
        return None, False
    return (cap, True) if n > cap else (n, False)


def sample_rows(con: sqlite3.Connection, table: str, limit: int = 2) -> list[dict]:
    try:
        cur = con.execute(f"SELECT * FROM {_q(table)} LIMIT {max(0, int(limit))}")
    except sqlite3.Error:
        return []
    names = [d[0] for d in cur.description]
    rows = []
    for raw in cur.fetchall():
        rows.append({n: short_value(v) for n, v in zip(names, raw)})
    return rows


def short_value(value) -> str | None:
    """Compact, single-line rendering used for samples in prompts."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return f"<blob {len(value)}B>"
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if len(text) > _MAX_VALUE_CHARS:
        text = text[: _MAX_VALUE_CHARS - 1] + "…"
    return text


def profile_table(con: sqlite3.Connection, table: str, cfg: dict) -> dict:
    """One table card without the summary text (that is added by summarize.py)."""
    columns = table_columns(con, table)
    fks = foreign_keys(con, table)
    rows, capped = row_count(con, table, cfg.get("row_count_cap", 200_000))
    return {
        "name": table,
        "kind": "table",
        "rows": rows,
        "rows_capped": capped,
        "n_columns": len(columns),
        "pk": [c["name"] for c in columns if c["pk"]],
        "fk_out": fks,
        "columns": columns,          # used by the summarizer, dropped later
        "sample_rows": sample_rows(con, table, cfg.get("sample_rows", 2)),
    }
