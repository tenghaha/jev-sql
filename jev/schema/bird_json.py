"""Build structured JSON schema from BIRD metadata and SQLite databases."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

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


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load a YAML config; resolve path fields relative to the project root."""
    config_path = Path(config_path).resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    root = config_path.parents[1]
    for key in ("tables_json", "databases_dir", "output_path"):
        path = Path(config[key])
        config[key] = path if path.is_absolute() else root / path
    config.setdefault("db_ids", [])
    return config


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def serialize_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    return value


def infer_date_format(value: Any) -> str | None:
    """Return the first matching strptime format for a date-like sample."""
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


def fetch_sample(conn: sqlite3.Connection, table: str, column: str) -> Any:
    """Fetch the first non-null value of a column from the real database."""
    sql = (
        f"SELECT {quote_identifier(column)} FROM {quote_identifier(table)} "
        f"WHERE {quote_identifier(column)} IS NOT NULL LIMIT 1"
    )
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error as exc:
        print(f"[warn] sample query failed for {table}.{column}: {exc}")
        return None
    return serialize_value(row[0]) if row else None


def build_primary_keys(entry: dict[str, Any]) -> dict[int, list[str]]:
    table_count = len(entry["table_names_original"])
    column_names = entry["column_names_original"]
    primary_keys: dict[int, list[str]] = {i: [] for i in range(table_count)}
    for key in entry["primary_keys"]:
        indices = key if isinstance(key, list) else [key]
        for index in indices:
            table_index, column = column_names[index]
            if table_index >= 0:
                primary_keys[table_index].append(column)
    return primary_keys


def build_adjacency(entry: dict[str, Any]) -> dict[int, dict[int, list[list[str]]]]:
    """Group foreign keys by neighbor table for each table."""
    table_names = entry["table_names_original"]
    column_names = entry["column_names_original"]
    adjacency: dict[int, dict[int, list[list[str]]]] = {i: {} for i in range(len(table_names))}
    for child_index, parent_index in entry["foreign_keys"]:
        child_table, child_column = column_names[child_index]
        parent_table, parent_column = column_names[parent_index]
        if child_table < 0 or parent_table < 0:
            continue
        pair = [
            f"{table_names[child_table]}.{child_column}",
            f"{table_names[parent_table]}.{parent_column}",
        ]
        for source, target in ((child_table, parent_table), (parent_table, child_table)):
            pairs = adjacency[source].setdefault(target, [])
            if pair not in pairs:
                pairs.append(pair)
    return adjacency


def build_database(entry: dict[str, Any], db_path: Path) -> dict[str, Any]:
    table_names = entry["table_names_original"]
    column_names = entry["column_names_original"]
    column_types = entry["column_types"]
    primary_keys = build_primary_keys(entry)
    adjacency = build_adjacency(entry)

    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        tables = []
        for table_index, table_name in enumerate(table_names):
            columns = []
            for column_index, (owner, column_name) in enumerate(column_names):
                if owner != table_index:
                    continue
                column_type = column_types[column_index]
                sample = fetch_sample(conn, table_name, column_name)
                columns.append(
                    {
                        "name": column_name,
                        "type": column_type,
                        "sample": sample,
                        "date_format": infer_date_format(sample) if column_type == "date" else None,
                    }
                )
            adjacent_tables = [
                {"table": table_names[neighbor], "foreign_keys": pairs}
                for neighbor, pairs in sorted(adjacency[table_index].items())
            ]
            tables.append(
                {
                    "name": table_name,
                    "columns": columns,
                    "primary_key": primary_keys[table_index],
                    "adjacent_tables": adjacent_tables,
                }
            )
    finally:
        conn.close()

    return {"db_id": entry["db_id"], "tables": tables}


def build_dataset(config: dict[str, Any]) -> dict[str, Any]:
    entries = json.loads(Path(config["tables_json"]).read_text(encoding="utf-8"))
    selected = set(config["db_ids"])
    databases = []
    for entry in entries:
        db_id = entry["db_id"]
        if selected and db_id not in selected:
            continue
        db_path = Path(config["databases_dir"]) / db_id / f"{db_id}.sqlite"
        if not db_path.exists():
            print(f"[skip] database not found: {db_path}")
            continue
        databases.append(build_database(entry, db_path))
        print(f"[done] {db_id}: {len(databases[-1]['tables'])} tables")
    return {
        "dataset": config["dataset"],
        "split": config["split"],
        "databases": databases,
    }


def write_schema(schema: dict[str, Any], output_path: str | Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
