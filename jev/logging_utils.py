"""Logging setup for inference scripts."""

from __future__ import annotations

import logging


def setup_logging(debug: bool = False) -> None:
    """Configure root logging; debug also emits full JEV requests and responses."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
