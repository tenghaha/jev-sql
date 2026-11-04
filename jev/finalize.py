"""Steps 3-4: column selection, ambiguity decisions, and forced foreign-key columns."""

from __future__ import annotations

from typing import Any

from .fk_candidates import select_links


def classify_columns(
    scored: dict[str, dict[str, Any]],
    high: float,
    low: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split columns into direct keeps, ambiguous ones, and dropped ones by best Score."""
    selected, ambiguous, dropped = [], [], []
    for entry in scored.values():
        if entry["best_score"] >= high:
            selected.append(entry)
        elif entry["best_score"] < low:
            dropped.append(entry)
        else:
            ambiguous.append(entry)

    def sort_key(item: dict[str, Any]) -> tuple[float, str, str]:
        return (-item["best_score"], item["table"], item["column"])

    selected.sort(key=sort_key)
    ambiguous.sort(key=sort_key)
    dropped.sort(key=sort_key)
    return selected, ambiguous, dropped


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


def finalize_selected(
    mentions: list[dict[str, str]],
    selected_entries: list[dict[str, Any]],
    column_states: dict[str, dict[str, Any]],
    link_states: dict[str, dict[str, Any]],
    link_answers: dict[str, Any],
    link_threshold: float,
    mention_score_floor: float,
) -> dict[str, Any]:
    """Build mention links and the column union from selected entries, then force join columns."""
    selected_links = select_links(link_states, link_answers, link_threshold)

    linked_columns = []
    for mention in mentions:
        columns = []
        for entry in selected_entries:
            span = next(
                (item for item in entry["spans"] if item["span_id"] == mention["span_id"]),
                None,
            )
            if span is None or span["score"] < mention_score_floor:
                continue
            columns.append(
                {
                    "candidate_id": entry["candidate_id"],
                    "table": entry["table"],
                    "column": entry["column"],
                    "dtype": entry["dtype"],
                    "score": span["score"],
                    "confidence": span["confidence"],
                    "source": entry.get("source", "score"),
                }
            )
        columns.sort(key=lambda item: (-item["score"], item["table"], item["column"]))
        linked_columns.append(
            {
                "span_id": mention["span_id"],
                "text": mention["text"],
                "span_type": mention.get("span_type", "other"),
                "columns": columns,
            }
        )

    union = []
    for entry in selected_entries:
        item = {
            "table": entry["table"],
            "column": entry["column"],
            "dtype": entry["dtype"],
            "best_score": entry["best_score"],
            "mentions": [
                span["span_id"] for span in entry["spans"] if span["score"] >= mention_score_floor
            ],
            "sources": [entry.get("source", "score")],
        }
        if entry.get("noul") is not None:
            item["ambiguous_noul"] = entry["noul"]
        union.append(item)
    union.sort(key=lambda item: (-item["best_score"], item["table"], item["column"]))
    union = _force_join_columns(union, selected_links, column_states)

    return {
        "linked_columns": linked_columns,
        "linked_columns_union": union,
        "linked_tables": sorted({item["table"] for item in union}),
        "selected_links": selected_links,
    }
