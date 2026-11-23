"""Model clients: OpenAI-compatible chat (template generation) and JEV (slot filling)."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

import requests

__all__ = ["chat", "jev_call", "extract_json"]


# ---------------------------------------------------------------------------
# OpenAI-compatible chat
# ---------------------------------------------------------------------------
def chat(prompt: str, cfg: dict, system: str | None = None,
         max_tokens: int | None = None) -> str | None:
    llm_cfg = cfg.get("template_llm") or {}
    api_key = os.environ.get(llm_cfg.get("api_key_env", "OPENAI_API_KEY"))
    base = (os.environ.get(llm_cfg.get("base_url_env", "OPENAI_API_BASE")) or "").rstrip("/")
    # an explicit `model:` in the config overrides the .env default
    model = llm_cfg.get("model") or os.environ.get(
        llm_cfg.get("model_env", "OPENAI_API_MODEL"))
    if not (api_key and base and model):
        raise RuntimeError("missing OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_API_MODEL")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {
        "model": model,
        "messages": messages,
        "temperature": float(llm_cfg.get("temperature", 0.0)),
        "max_tokens": int(max_tokens or llm_cfg.get("max_tokens", 4000)),
    }
    req = urllib.request.Request(
        f"{base}/chat/completions", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=float(llm_cfg.get("timeout", 180))) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"[llm] HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}")
        return None
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[llm] {type(exc).__name__}: {exc}")
        return None
    choice = (payload.get("choices") or [{}])[0]
    return ((choice.get("message") or {}).get("content") or "").strip() or None


def extract_json(text: str) -> dict:
    """Parse the first JSON object in ``text`` (tolerates fences and prose)."""
    if not text:
        raise ValueError("empty model output")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cleaned[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = cleaned.find("{", start + 1)
    raise ValueError("no JSON object in model output")


# ---------------------------------------------------------------------------
# JEV decision model
# ---------------------------------------------------------------------------
def jev_call(state: dict, questions: dict, cfg: dict) -> dict:
    """One batched JEV request; returns the ``answers`` mapping."""
    if not questions:
        return {"answers": {}, "usage": None, "elapsed_seconds": 0.0}
    jev_cfg = cfg.get("jev") or {}
    endpoint = os.environ.get(jev_cfg.get("endpoint_env", "JEV_API_BASE"))
    api_key = os.environ.get(jev_cfg.get("api_key_env", "AI_GATEWAY_API_KEY"))
    if not (endpoint and api_key):
        raise RuntimeError("missing JEV_API_BASE / AI_GATEWAY_API_KEY")
    payload = {"model": jev_cfg.get("model", "typesafe-ai/jev"),
               "state": state, "questions": questions}
    last_error = None
    attempts = max(1, int(jev_cfg.get("retries", 3)))
    for attempt in range(attempts):
        try:
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=int(jev_cfg.get("timeout", 120)))
            if not response.ok:
                raise RuntimeError(f"JEV {response.status_code}: {response.text[:300]}")
            data = response.json()
            break
        except Exception as exc:  # noqa: BLE001 - retry transient transport errors
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(3 * (attempt + 1))
    else:
        raise RuntimeError(f"JEV call failed after {attempts} attempts: {last_error}")
    if not isinstance(data.get("answers"), dict):
        raise RuntimeError("JEV response has no answers object")
    return {"answers": data["answers"], "usage": data.get("usage")}
