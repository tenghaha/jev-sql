"""Experiment config loading.

One YAML file == one experiment (one dataset split), following the repository
convention.  Relative paths are resolved against the repository root, which is
the parent of this experiment directory.
"""

from __future__ import annotations

import os

import yaml

__all__ = ["load_config"]

_REQUIRED = ("dataset", "split", "output_path", "summarizer")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    missing = [k for k in _REQUIRED if k not in cfg]
    if missing:
        raise SystemExit(f"config {path} is missing required fields: {missing}")

    exp_dir = os.path.dirname(os.path.dirname(os.path.abspath(path)))  # configs/ -> exp dir
    root = os.path.normpath(os.path.join(exp_dir, cfg.get("repo_root", "..")))
    cfg["root"] = root
    cfg["exp_dir"] = exp_dir
    # dataset paths are relative to the repository, artifacts to this experiment
    for key in ("databases_dir", "db_zip", "tables_json"):
        if cfg.get(key):
            cfg[key] = _abs(root, cfg[key])
    for key in ("cache_dir", "output_path", "schema_output_path"):
        if cfg.get(key):
            cfg[key] = _abs(exp_dir, cfg[key])
    if not cfg.get("schema_output_path"):
        raise SystemExit("config is missing `schema_output_path`")
    cfg.setdefault("summary_max_chars", 160)
    cfg.setdefault("summary_max_words", 18)
    cfg.setdefault("sample_rows", 2)
    cfg.setdefault("sample_values", 3)
    cfg.setdefault("db_pattern", "*/*.sqlite")
    cfg.setdefault("llm_max_tables", 10)
    cfg.setdefault("llm_max_tokens", 600)
    cfg.setdefault("llm_concurrency", 6)
    cfg.setdefault("llm_model", os.environ.get("OPENAI_API_MODEL", ""))
    if cfg["summarizer"] not in ("template", "llm"):
        raise SystemExit("summarizer must be 'template' or 'llm'")
    return cfg


def _abs(root: str, path: str) -> str:
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(root, path))
