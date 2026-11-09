"""Step 3 candidates: result-table pairs and their join columns.

Pairs are the unordered combinations of the tables that already hold a selected
column. The join column of each pair is chosen by JEV choice questions from the
two table schemas, so neither the generated schema's inter-table information nor
the gold SQL is used to propose or select a join.
"""

from __future__ import annotations

from typing import Any

CHOICE_ANSWER_KEYS = ("choice", "answer", "selected", "option", "column")
PROBABILITY_KEY = "probabilities"


def pair_id(index: int) -> str:
    return f"p{index}"


def choice_question_id(pair: str, side: str) -> str:
    return f"c3_{pair}_{side}"


def result_tables(columns: list[dict[str, Any]]) -> list[str]:
    """Tables holding at least one selected column, in first-appearance order."""
    tables = []
    seen = set()
    for item in columns:
        key = item["table"].casefold()
        if key in seen:
            continue
        seen.add(key)
        tables.append(item["table"])
    return tables


def build_pairs(tables: list[str]) -> list[dict[str, Any]]:
    """All unordered pairs of result tables."""
    pairs = []
    for index, left in enumerate(tables):
        for right in tables[index + 1 :]:
            pairs.append(
                {
                    "pair_id": pair_id(len(pairs) + 1),
                    "left_table": left,
                    "right_table": right,
                }
            )
    return pairs


def _normalize_choice(value: str) -> str:
    text = value.strip().strip("`\"'")
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.strip().strip("`\"'")


def parse_choice(answer: Any, options: list[str]) -> str | None:
    """Read the chosen column from a choice answer; None when unreadable."""
    if not isinstance(answer, dict):
        return None
    lookup = {option.casefold(): option for option in options}
    for key in CHOICE_ANSWER_KEYS:
        value = answer.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, str):
            chosen = lookup.get(_normalize_choice(value).casefold())
            if chosen is not None:
                return chosen
        elif isinstance(value, (int, float)) and float(value).is_integer():
            index = int(value)
            if 0 <= index < len(options):
                return options[index]
    return _highest_probability(answer.get(PROBABILITY_KEY), lookup)


def _highest_probability(probabilities: Any, lookup: dict[str, str]) -> str | None:
    """Fall back to the most probable option when no explicit choice is given."""
    if not isinstance(probabilities, dict):
        return None
    best_option = None
    best_value = None
    for name, value in probabilities.items():
        option = lookup.get(str(name).casefold())
        if option is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if best_value is None or value > best_value:
            best_option, best_value = option, value
    return best_option


def collect_chosen_columns(
    database: dict[str, Any],
    pairs: list[dict[str, Any]],
    answers: dict[str, Any],
) -> list[dict[str, Any]]:
    """Resolve both directions of every pair into the chosen join columns."""
    table_map = {table["name"].casefold(): table for table in database["tables"]}
    chosen = []
    for pair in pairs:
        for side, table_name in (("a", pair["left_table"]), ("b", pair["right_table"])):
            table = table_map.get(table_name.casefold())
            if table is None:
                continue
            options = [column["name"] for column in table["columns"]]
            column = parse_choice(answers.get(choice_question_id(pair["pair_id"], side)), options)
            if column is None:
                continue
            chosen.append(
                {
                    "pair_id": pair["pair_id"],
                    "table": table["name"],
                    "column": column,
                    "dtype": _column_type(table, column),
                }
            )
    return chosen


def _column_type(table: dict[str, Any], column: str) -> str | None:
    for item in table["columns"]:
        if item["name"].casefold() == column.casefold():
            return item["type"]
    return None
