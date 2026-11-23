"""Offline database summary: table-level cards + a foreign-key connectivity graph.

This package is stage 1 of the understanding pipeline.  It reads a SQLite
database with PRAGMA statements (deterministic metadata: columns, primary keys,
foreign keys) plus one or two sample rows, and writes one JSON document per
dataset that contains

* one **table card** per table: row count, column count, primary key, outgoing
  foreign keys, a length-budgeted one-line summary, and a few sample values;
* one **connectivity graph**: nodes = tables, edges = foreign keys (with the
  column pairs), undirected adjacency, connected components, isolated tables.

The cards deliberately do not enumerate every column: they are a compressed view
that tells the model what each table roughly is, so that later stages can spend
tokens on the columns that matter.

Entry point: ``scripts/build_db_summary.py`` (see ``docs/01-db-summary.md``).
"""

__all__ = ["build", "config", "graph", "llm", "sqlite_meta", "summarize"]
