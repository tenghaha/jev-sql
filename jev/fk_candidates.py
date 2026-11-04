"""Candidate join links taken from the generated gold schema.

Links come from each table's ``adjacent_tables`` in the schema JSON.
The gold SQL is intentionally not used.
"""

from __future__ import annotations

import logging
from typing import Any

from .candidates import noul_probability

logger = logging.getLogger(__name__)


def link_question_id(link_id: str) -> str:
    return f"j_{link_id}"


def _split_pair(pair: str) -> tuple[str, str]:
    table, column = pair.rsplit(".", 1)
    return table, column


def build_link_candidates(
    database: dict[str, Any],
    focus_tables: list[str],
) -> dict[str, dict[str, Any]]:
    """Return foreign-key links between focus tables, canonicalized to schema names."""
    table_map = {table["name"].casefold(): table for table in database["tables"]}
    focus = {name.casefold() for name in focus_tables}
    pairs: dict[tuple[str, str, str, str], tuple[str, str, str, str]] = {}

    for table in database["tables"]:
        if table["name"].casefold() not in focus:
            continue
        for adjacent in table.get("adjacent_tables") or []:
            for child_pair, parent_pair in adjacent.get("foreign_keys") or []:
                child_table, child_column = _split_pair(child_pair)
                parent_table, parent_column = _split_pair(parent_pair)
                key = (
                    child_table.casefold(),
                    child_column.casefold(),
                    parent_table.casefold(),
                    parent_column.casefold(),
                )
                if key in pairs or key[0] == key[2]:
                    continue
                if key[0] not in focus or key[2] not in focus:
                    continue
                if key[0] not in table_map or key[2] not in table_map:
                    continue
                child_canonical = _canonical_column(table_map[key[0]], child_column)
                parent_canonical = _canonical_column(table_map[key[2]], parent_column)
                if child_canonical is None or parent_canonical is None:
                    continue
                pairs[key] = (
                    table_map[key[0]]["name"],
                    child_canonical,
                    table_map[key[2]]["name"],
                    parent_canonical,
                )

    candidates: dict[str, dict[str, Any]] = {}
    for key in sorted(pairs):
        child_table, child_column, parent_table, parent_column = pairs[key]
        link_id = f"l{len(candidates) + 1}"
        candidates[link_id] = {
            "link_id": link_id,
            "child_table": child_table,
            "child_column": child_column,
            "parent_table": parent_table,
            "parent_column": parent_column,
        }

    if candidates:
        logger.info(
            "Join candidates among %d focus tables: %d links", len(focus_tables), len(candidates)
        )
    else:
        logger.info("Join candidates among %d focus tables: none", len(focus_tables))
    return candidates


def _canonical_column(table: dict[str, Any], column: str) -> str | None:
    for item in table["columns"]:
        if item["name"].casefold() == column.casefold():
            return item["name"]
    return None


def links_for_table(
    link_states: dict[str, dict[str, Any]],
    table_name: str,
) -> dict[str, dict[str, Any]]:
    """Links where `table_name` takes part, annotated with the other side."""
    table_key = table_name.casefold()
    rows = {}
    for link_id, link in link_states.items():
        child_key = link["child_table"].casefold()
        parent_key = link["parent_table"].casefold()
        if child_key == table_key:
            other = link["parent_table"]
        elif parent_key == table_key:
            other = link["child_table"]
        else:
            continue
        rows[link_id] = {**link, "other_table": other}
    return rows


def select_links(
    link_states: dict[str, dict[str, Any]],
    answers: dict[str, Any],
    threshold: float,
) -> list[dict[str, Any]]:
    """Keep join links whose Noul probability reaches the threshold."""
    rows = []
    for link_id, link in link_states.items():
        noul = noul_probability(answers.get(link_question_id(link_id)))
        if noul < threshold:
            continue
        rows.append({**link, "noul": round(noul, 4)})
    rows.sort(key=lambda row: (-row["noul"], row["link_id"]))
    return rows
