"""Small helpers shared by the cascade stages."""

from __future__ import annotations

from typing import Any

from .candidates import noul_probability


def merge_usage(target: dict[str, Any], usage: Any) -> None:
    """Accumulate numeric token usage fields."""
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            target[key] = target.get(key, 0) + value


def merge_noul_answers(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Merge answers; for duplicate question ids keep the higher Noul probability."""
    for key, answer in incoming.items():
        if key not in target or noul_probability(answer) > noul_probability(target[key]):
            target[key] = answer


def chunk(items: list[str], size: int) -> list[list[str]]:
    """Split ids into batches; size <= 0 or >= len(items) means a single batch."""
    if size <= 0 or size >= len(items):
        return [items]
    return [items[index : index + size] for index in range(0, len(items), size)]
