"""Column selection and the final column set."""

from __future__ import annotations

from typing import Any


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


def add_join_columns(
    union: list[dict[str, Any]],
    chosen_columns: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Add the join columns chosen in step 3, deduplicated by table and column."""
    by_key = {
        (item["table"].casefold(), item["column"].casefold()): item for item in union
    }
    num_added = 0
    for chosen in chosen_columns:
        key = (chosen["table"].casefold(), chosen["column"].casefold())
        item = by_key.get(key)
        if item is not None:
            if "result_join" not in item["sources"]:
                item["sources"].append("result_join")
            continue
        item = {
            "table": chosen["table"],
            "column": chosen["column"],
            "dtype": chosen["dtype"],
            "best_score": None,
            "mentions": [],
            "sources": ["result_join"],
        }
        by_key[key] = item
        union.append(item)
        num_added += 1

    union.sort(
        key=lambda item: (
            item["best_score"] is None,
            -(item["best_score"] or 0.0),
            item["table"],
            item["column"],
        )
    )
    return union, num_added


def finalize_selected(
    mentions: list[dict[str, str]],
    selected_entries: list[dict[str, Any]],
    mention_score_floor: float,
) -> dict[str, Any]:
    """Build the mention-grouped columns and the deduplicated column union."""
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

    return {"linked_columns": linked_columns, "linked_columns_union": union}
