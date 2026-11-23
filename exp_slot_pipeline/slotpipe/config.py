"""Experiment config: YAML in / paths resolved, plus ``.env`` loading."""

from __future__ import annotations

import os

import yaml

__all__ = ["load_config", "PROJECT_ROOT"]

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))          # exp_slot_pipeline/ -> repo root

# dataset paths resolve against the repo root, artifacts against the experiment
_DATA_KEYS = ("db_summary_path", "schema_path", "questions_path", "databases_dir")
_ARTIFACT_KEYS = ("output_dir",)


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    exp_dir = os.path.dirname(os.path.dirname(os.path.abspath(path)))
    cfg["exp_dir"] = exp_dir
    cfg["root"] = PROJECT_ROOT
    for key in _DATA_KEYS:
        if cfg.get(key):
            cfg[key] = _abs(PROJECT_ROOT, cfg[key])
    for key in _ARTIFACT_KEYS:
        if cfg.get(key):
            cfg[key] = _abs(exp_dir, cfg[key])
    cfg.setdefault("num_templates", 2)
    cfg.setdefault("fill", {})
    cfg.setdefault("jev", {})
    cfg.setdefault("template_llm", {})
    _load_env(PROJECT_ROOT)
    return cfg


def _abs(base: str, path: str) -> str:
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def _load_env(root: str) -> None:
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
