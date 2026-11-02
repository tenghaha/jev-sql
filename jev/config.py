"""Load experiment configs and project environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH_FIELDS = ("schema_path", "questions_path", "output_dir")


def load_environment() -> None:
    """Load .env from the project root; values there take precedence."""
    load_dotenv(PROJECT_ROOT / ".env", override=True)


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load an experiment YAML and resolve its path fields."""
    load_environment()
    path = Path(config_path).resolve()
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key in CONFIG_PATH_FIELDS:
        if key in config:
            value = Path(config[key])
            config[key] = value if value.is_absolute() else PROJECT_ROOT / value
    return config


def require_env(name: str) -> str:
    """Return an environment variable or fail with its name."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value
