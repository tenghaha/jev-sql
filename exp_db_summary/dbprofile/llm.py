"""Minimal OpenAI-compatible chat client (stdlib only) used for the optional
LLM table summarizer.

Credentials come from the repository ``.env`` (``OPENAI_API_KEY`` /
``OPENAI_API_BASE`` / ``OPENAI_API_MODEL``); nothing is hard-coded and nothing
is printed.  A failed call returns ``None`` so a run can fall back to the
deterministic summarizer instead of dying halfway.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

__all__ = ["load_env", "chat"]


def load_env(root: str) -> dict:
    """Parse ``<root>/.env`` (KEY=VALUE lines, ``#`` comments) + os.environ."""
    env = {}
    path = os.path.join(root, ".env")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_API_MODEL"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def chat(prompt: str, cfg: dict, timeout: float = 120.0) -> str | None:
    """One-shot chat completion; returns the assistant text or ``None``.

    ``max_tokens`` must leave room for the model's reasoning, because a
    reasoning model spends completion tokens on ``reasoning_content`` before it
    writes the answer (with a tight budget the answer comes back empty with
    ``finish_reason == "length"``).  On such an empty answer the call is retried
    once with twice the budget.
    """
    budget = int(cfg.get("llm_max_tokens", 600))
    text = _call(prompt, cfg, budget, timeout)
    if text is None:
        text = _call(prompt, cfg, min(budget * 2, 2048), timeout)
    return text


def _call(prompt: str, cfg: dict, max_tokens: int, timeout: float) -> str | None:
    env = load_env(cfg["root"])
    api_key = env.get("OPENAI_API_KEY")
    base = (env.get("OPENAI_API_BASE") or "").rstrip("/")
    model = cfg.get("llm_model") or env.get("OPENAI_API_MODEL")
    if not (api_key and base and model):
        print("[llm] missing OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_API_MODEL")
        return None

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        choice = payload["choices"][0]
        text = (choice.get("message") or {}).get("content") or ""
        text = text.strip()
        if not text:
            # reasoning-only response (budget exhausted before the answer)
            return None
        return text
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001 - diagnostics only
            pass
        print(f"[llm] HTTP {exc.code} for model {model}: {detail}")
        return None
    except (urllib.error.URLError, KeyError, IndexError,
            json.JSONDecodeError, TimeoutError) as exc:
        print(f"[llm] call failed: {type(exc).__name__}: {exc}")
        return None
