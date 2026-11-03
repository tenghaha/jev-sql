"""JEV decision-model HTTP client."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from .config import require_env

logger = logging.getLogger(__name__)


def call_jev(
    state: dict[str, Any],
    questions: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Submit one typed decision request to the JEV endpoint."""
    if not questions:
        logger.warning("Skipping JEV call: no questions")
        return {"answers": {}, "elapsed_seconds": 0.0, "raw": {}}

    payload = {"model": config["model"], "state": state, "questions": questions}
    logger.debug("JEV request: %s", json.dumps(payload, ensure_ascii=False))

    started = time.perf_counter()
    response = requests.post(
        require_env(config["endpoint_env"]),
        headers={
            "Authorization": f"Bearer {require_env(config['api_key_env'])}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=config.get("timeout", 120),
    )
    if not response.ok:
        raise RuntimeError(
            f"JEV API error {response.status_code}: {response.text[:300]}"
        )
    data = response.json()
    if not isinstance(data.get("answers"), dict):
        raise RuntimeError("JEV API response has no answers object")

    elapsed = time.perf_counter() - started
    logger.debug("JEV response: %s", json.dumps(data, ensure_ascii=False))
    logger.info(
        "JEV call: %d questions, %.2fs, usage=%s",
        len(questions),
        elapsed,
        data.get("usage"),
    )
    return {
        "answers": data["answers"],
        "elapsed_seconds": elapsed,
        "raw": data,
    }
