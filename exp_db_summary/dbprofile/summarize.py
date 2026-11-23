"""Table summaries: a deterministic renderer and an optional LLM writer.

Both paths produce the same field (``summary``) and differ only in
``summary_source``.  The card must stay short — it is the compressed view that
tells a model what a table roughly is, without listing every column.
"""

from __future__ import annotations

import json
import os

from . import llm

__all__ = ["template_summary", "llm_summary", "SummaryCache", "build_prompt",
           "table_card"]

_PROMPT_VERSION = "v1"


# ---------------------------------------------------------------------------
# deterministic summarizer
# ---------------------------------------------------------------------------
def template_summary(table: dict, cfg: dict) -> str:
    """``name: 9,859 rows, 27 cols; joins schools; e.g. 'Alameda', 'K-12'``."""
    rows = _num(table["rows"]) + ("+" if table.get("rows_capped") else "")
    parts = [f"{table['name']}: {rows} rows, {table['n_columns']} cols"]
    neighbours = sorted({fk["to_table"] for fk in table.get("fk_out") or []})
    if neighbours:
        parts.append("joins " + ", ".join(neighbours))
    samples = _sample_values(table, cfg.get("sample_values", 3))
    if samples:
        parts.append("e.g. " + ", ".join(samples))
    return _clip("; ".join(parts), cfg.get("summary_max_chars", 160))


# ---------------------------------------------------------------------------
# LLM summarizer
# ---------------------------------------------------------------------------
def build_prompt(table: dict, db_id: str, cfg: dict) -> str:
    """English prompt; the model sees columns + samples but must not list them."""
    max_words = cfg.get("summary_max_words", 18)
    cols = ", ".join(c["name"] for c in table.get("columns") or [] if not c["pk"])
    return "\n".join([
        "You are describing one database table for a text-to-SQL assistant.",
        "The description is a compressed memory aid, not a schema dump.",
        "",
        f"Database: {db_id}",
        f"Table: {table['name']}",
        f"Rows: {_num(table['rows'])}",
        f"Columns ({table['n_columns']}): {cols or '-'}",
        f"Primary key: {', '.join(table.get('pk') or []) or '-'}",
        f"Foreign keys: {_fk_text(table)}",
        f"Sample rows: {_sample_text(table)}",
        "",
        f"Write ONE sentence of at most {max_words} words that says what this "
        "table stores and what a single row represents.",
        "Rules:",
        "- do not enumerate columns, do not write SQL, no markdown, no preamble;",
        "- use only what the table name, keys and samples imply; do not invent "
        "domains or column names;",
        "- if the row grain is unclear, describe it in generic terms "
        "(e.g. \"one row per record\").",
        "Return only the sentence.",
    ])


def llm_summary(table: dict, db_id: str, cfg: dict) -> str | None:
    text = llm.chat(build_prompt(table, db_id, cfg), cfg)
    if not text:
        return None
    text = " ".join(text.strip().split())
    text = text.strip('"').strip()
    return _clip(text, cfg.get("summary_max_chars", 160)) if text else None


# ---------------------------------------------------------------------------
# cache (so a later full LLM pass never re-pays for a table it already saw)
# ---------------------------------------------------------------------------
class SummaryCache:
    def __init__(self, path: str):
        self.path = path
        self.entries: dict[str, dict] = {}
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self.entries = json.load(fh)
            except (OSError, json.JSONDecodeError):
                self.entries = {}

    @staticmethod
    def key(cfg: dict, db_id: str, table: str) -> str:
        """Keyed by (dataset, db, table): the prompt does not mention the split,
        so MINIDEV reuses the summaries computed for dev."""
        model = cfg.get("llm_model") or "default"
        return f"{cfg['dataset']}|{db_id}|{table}|{model}|{_PROMPT_VERSION}"

    def get(self, key: str) -> str | None:
        hit = self.entries.get(key)
        return hit.get("summary") if isinstance(hit, dict) else None

    def put(self, key: str, summary: str) -> None:
        self.entries[key] = {"summary": summary}

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.entries, fh, ensure_ascii=False, indent=0, sort_keys=True)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# the card written to disk
# ---------------------------------------------------------------------------
def table_card(table: dict, summary: str, source: str, cfg: dict) -> dict:
    """Compressed table card: no column list, only the metadata + summary."""
    neighbours = sorted({fk["to_table"] for fk in table.get("fk_out") or []})
    return {
        "name": table["name"],
        "rows": table["rows"],
        "rows_capped": table.get("rows_capped", False),
        "n_columns": table["n_columns"],
        "pk": table["pk"],
        "fk_out": [{"to_table": fk["to_table"], "pairs": fk["pairs"]}
                   for fk in table.get("fk_out") or []],
        "neighbours": neighbours,
        "sample": _sample_values(table, cfg.get("sample_values", 3)),
        "summary": summary,
        "summary_chars": len(summary),
        "summary_source": source,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _sample_values(table: dict, limit: int) -> list[str]:
    """A few short, distinct, non-null values — enough to hint at the domain."""
    out: list[str] = []
    for row in table.get("sample_rows") or []:
        for value in row.values():
            if value is None or not str(value).strip():
                continue
            text = str(value)
            if text[:1] in "{[":          # JSON / array blobs are not a domain hint
                continue
            if text in out:
                continue
            out.append(text)
            if len(out) >= limit:
                return out
    return out


def _num(value) -> str:
    return f"{value:,}" if isinstance(value, int) else "?"


def _fk_text(table: dict) -> str:
    items = []
    for fk in table.get("fk_out") or []:
        pairs = ", ".join(f"{a}->{b}" for a, b in fk["pairs"])
        items.append(f"{pairs} to {fk['to_table']}")
    return "; ".join(items) or "-"


def _sample_text(table: dict) -> str:
    rows = table.get("sample_rows") or []
    if not rows:
        return "-"
    return json.dumps(rows, ensure_ascii=False)[:400]


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
