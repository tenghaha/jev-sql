"""Steps 3-4: score filtering and forced foreign-key columns."""

from __future__ import annotations

from typing import Any

from .column_candidates import filter_columns
from .fk_candidates import select_links


def _dtype(column_states: dict[str, dict[str, Any]], table: str, column: str) -> str | None:
    for candidate in column_states.values():
        if (
            candidate["table_name"].casefold() == table.casefold()
            and candidate["column_name"].casefold() == column.casefold()
        ):
            return candidate["dtype"]
    return None


def _force_join_columns(
    union: list[dict[str, Any]],
    selected_links: list[dict[str, Any]],
    column_states: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep both sides of every selected join even if their Score is below threshold."""
    by_key = {
        (item["table"].casefold(), item["column"].casefold()): item for item in union
    }
    for link in selected_links:
        for table, column in (
            (link["child_table"], link["child_column"]),
            (link["parent_table"], link["parent_column"]),
        ):
            key = (table.casefold(), column.casefold())
            item = by_key.get(key)
            if item is not None:
                if "forced_foreign_key" not in item["sources"]:
                    item["sources"].append("forced_foreign_key")
                continue
            entry = {
                "table": table,
                "column": column,
                "dtype": _dtype(column_states, table, column),
                "best_score": None,
                "mentions": [],
                "sources": ["forced_foreign_key"],
            }
            by_key[key] = entry
            union.append(entry)

    union.sort(
        key=lambda item: (
            item["best_score"] is None,
            -(item["best_score"] or 0.0),
            item["table"],
            item["column"],
        )
    )
    return union


def finalize_results(
    mentions: list[dict[str, str]],
    column_states: dict[str, dict[str, Any]],
    column_answers: dict[str, Any],
    link_states: dict[str, dict[str, Any]],
    link_answers: dict[str, Any],
    column_threshold: float,
    link_threshold: float,
) -> dict[str, Any]:
    """Apply thresholds, then force foreign-key columns of selected joins."""
    linked_columns, union = filter_columns(mentions, column_states, column_answers, column_threshold)
    selected_links = select_links(link_states, link_answers, link_threshold)
    union = _force_join_columns(union, selected_links, column_states)
    return {
        "linked_columns": linked_columns,
        "linked_columns_union": union,
        "linked_tables": sorted({item["table"] for item in union}),
        "selected_links": selected_links,
    }
