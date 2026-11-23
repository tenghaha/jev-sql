"""Full column-level schema, in the JSON shape used by ``scripts/build_schema.py``.

The reference builder reads BIRD's ``tables.json`` for column names/types and the
SQLite file for one sample per column, then emits::

    {"dataset", "split", "include_adjacent_tables",
     "databases": [{"db_id", "tables": [{"name", "columns": [...],
                                         "primary_key": [...],
                                         "adjacent_tables": [...]}]}]}

Spider2.0 ships no ``tables.json``, so this module builds the same shape from
PRAGMA metadata: when a ``tables_json`` is configured (BIRD) its column types are
authoritative, exactly as in the reference; otherwise the declared SQLite type is
normalised to ``integer`` / ``real`` / ``date`` / ``text``.

Samples: one bounded ``SELECT * ... LIMIT n`` per table fills most columns; the
few that are still NULL in that window get one targeted
``WHERE col IS NOT NULL LIMIT 1`` query, which is exactly the reference
semantics (first non-null value in scan order) without paying one query per
column.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

__all__ = ["build_database_schema", "infer_date_format", "load_type_lookup",
           "normalize_type", "DATE_FORMATS"]

DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%Y%m%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y-%m",
    "%Y%m",
    "%Y",
)

_MAX_SAMPLE_CHARS = 120


# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------
def normalize_type(declared: str) -> str:
    """Map a declared SQLite type to ``integer`` / ``real`` / ``date`` / ``text``."""
    text = (declared or "").strip().lower()
    if not text:
        return "text"
    if "int" in text:
        return "integer"
    if any(t in text for t in ("real", "floa", "doub", "num", "dec")):
        return "real"
    if any(t in text for t in ("date", "time")):
        return "date"
    return "text"


def load_type_lookup(path: str | None) -> dict[str, dict[tuple[str, str], str]]:
    """``{db_id: {(table, column): type}}`` from a BIRD ``*_tables.json``."""
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        rows = json.load(fh)
    out: dict[str, dict[tuple[str, str], str]] = {}
    for entry in rows:
        tables = entry["table_names_original"]
        index: dict[tuple[str, str], str] = {}
        for i, ((table_id, column), type_) in enumerate(
                zip(entry["column_names_original"], entry["column_types"])):
            if table_id < 0:
                continue
            index[(tables[table_id], column)] = type_
        out[entry["db_id"]] = index
    return out


def infer_date_format(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(text, fmt)
        except ValueError:
            continue
        return fmt
    return None


# ---------------------------------------------------------------------------
# one database
# ---------------------------------------------------------------------------
def build_database_schema(con: sqlite3.Connection, db_id: str,
                          profiles: list[dict], cfg: dict,
                          type_index: dict[tuple[str, str], str] | None = None
                          ) -> dict:
    """One database entry: every table with every column."""
    limit = int(cfg.get("schema_sample_rows", 200))
    samples_by_table = {}
    for profile in profiles:
        samples = _column_samples(con, profile["name"], limit)
        for column in profile["columns"]:
            if column["name"] not in samples:
                value = _first_non_null(con, profile["name"], column["name"])
                if value is not None:
                    samples[column["name"]] = value
        samples_by_table[profile["name"]] = samples
    adjacency = _adjacency(profiles)

    tables = []
    for profile in profiles:
        samples = samples_by_table.get(profile["name"], {})
        columns = []
        for column in profile["columns"]:
            name = column["name"]
            type_ = (type_index or {}).get((profile["name"], name)) \
                or normalize_type(column["type"])
            sample = samples.get(name)
            columns.append({
                "name": name,
                "type": type_,
                "sample": _clip(sample),
                "date_format": infer_date_format(sample) if type_ == "date" else None,
            })
        tables.append({
            "name": profile["name"],
            "columns": columns,
            "primary_key": list(profile["pk"]),
            "adjacent_tables": [
                {"table": neighbor, "foreign_keys": pairs}
                for neighbor, pairs in sorted(adjacency.get(profile["name"], {}).items())
            ],
        })
    return {"db_id": db_id, "tables": tables}


def _first_non_null(con: sqlite3.Connection, table: str, column: str):
    """Targeted fallback for sparse columns: the first non-null value."""
    qcol = '"' + column.replace('"', '""') + '"'
    qtab = '"' + table.replace('"', '""') + '"'
    try:
        row = con.execute(f"SELECT {qcol} FROM {qtab} "
                          f"WHERE {qcol} IS NOT NULL LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _column_samples(con: sqlite3.Connection, table: str, limit: int) -> dict:
    """First non-null value per column, from one bounded ``SELECT *``."""
    try:
        cur = con.execute(f'SELECT * FROM "{table.replace(chr(34), chr(34) * 2)}" '
                          f"LIMIT {max(0, limit)}")
    except sqlite3.Error:
        return {}
    names = [d[0] for d in cur.description]
    found: dict[str, object] = {}
    for row in cur.fetchall():
        for name, value in zip(names, row):
            if name not in found and value is not None:
                found[name] = value
    return found


def _adjacency(profiles: list[dict]) -> dict[str, dict[str, list[list[str]]]]:
    """Symmetric neighbour map, pairs rendered as ``table.column`` (as the reference)."""
    known = {p["name"] for p in profiles}
    adjacency: dict[str, dict[str, list[list[str]]]] = {p["name"]: {} for p in profiles}
    for profile in profiles:
        child = profile["name"]
        for fk in profile["fk_out"]:
            parent = fk["to_table"]
            if parent not in known:
                continue
            for from_col, to_col in fk["pairs"]:
                if to_col is None:      # unresolved target: keep the edge, skip the pair
                    continue
                pair = [f"{child}.{from_col}", f"{parent}.{to_col}"]
                for source, target in ((child, parent), (parent, child)):
                    pairs = adjacency[source].setdefault(target, [])
                    if pair not in pairs:
                        pairs.append(pair)
    return adjacency


def _clip(value):
    """Keep samples small; blobs become a placeholder, long text is truncated.

    Numeric samples stay numeric (the reference builder emits the raw value), so
    a column's sample is ``35`` and not ``"35"``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, bytes):
        return f"<blob {len(value)}B>"
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if len(text) > _MAX_SAMPLE_CHARS:
        return text[: _MAX_SAMPLE_CHARS - 1] + "…"
    return text
