"""JEV decision-model HTTP client."""

from __future__ import annotations

import time
from typing import Any

import requests

from .config import require_env


def call_jev(
    state: dict[str, Any],
    questions: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Submit one typed decision request to the JEV endpoint."""
    started = time.perf_counter()
    response = requests.post(
        require_env(config["endpoint_env"]),
        headers={
            "Authorization": f"Bearer {require_env(config['api_key_env'])}",
            "Content-Type": "application/json",
        },
        json={"model": config["model"], "state": state, "questions": questions},
        timeout=config.get("timeout", 120),
    )
    if not response.ok:
        raise RuntimeError(
            f"JEV API error {response.status_code}: {response.text[:300]}"
        )
    data = response.json()
    if not isinstance(data.get("answers"), dict):
        raise RuntimeError("JEV API response has no answers object")
    return {
        "answers": data["answers"],
        "elapsed_seconds": time.perf_counter() - started,
        "raw": data,
    }
