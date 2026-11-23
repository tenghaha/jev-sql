"""Assemble the dataset-level JSON: per-database table cards + connectivity graph."""

from __future__ import annotations

import glob
import json
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import sqlite_meta
from .graph import build_graph
from .schema import build_database_schema, load_type_lookup
from .summarize import SummaryCache, llm_summary, table_card, template_summary

__all__ = ["iter_databases", "build_all", "write_json"]


# ---------------------------------------------------------------------------
# database sources
# ---------------------------------------------------------------------------
def iter_databases(cfg: dict) -> list[tuple[str, str]]:
    """Return ``[(db_id, sqlite_path), ...]`` for the configured scope."""
    backend = cfg.get("db_backend", "sqlite_dir")
    if backend == "sqlite_dir":
        pattern = os.path.join(cfg["databases_dir"], cfg["db_pattern"])
        out = []
        for path in sorted(glob.glob(pattern)):
            db_id = os.path.splitext(os.path.basename(path))[0]
            out.append((db_id, path))
        return out
    if backend == "sqlite_zip":
        cache = cfg.get("cache_dir") or os.path.join(cfg["exp_dir"], "outputs", "cache")
        os.makedirs(cache, exist_ok=True)
        with zipfile.ZipFile(cfg["db_zip"]) as zf:
            members = [m for m in zf.namelist()
                       if m.lower().endswith(".sqlite") and not m.startswith("__MACOSX")]
            out = []
            for member in sorted(members):
                db_id = os.path.splitext(os.path.basename(member))[0]
                target = os.path.join(cache, os.path.basename(member))
                need = (not os.path.exists(target)
                        or os.path.getsize(target) != zf.getinfo(member).file_size)
                if need:  # extracted once, reused afterwards
                    print(f"[extract] {member} -> {target}")
                    with zf.open(member) as src, open(target, "wb") as dst:
                        while True:
                            chunk = src.read(1 << 22)
                            if not chunk:
                                break
                            dst.write(chunk)
                out.append((db_id, target))
            return out
    raise SystemExit(f"unknown db_backend: {backend!r}")


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------
def build_all(cfg: dict, only_db: str | None = None,
              limit_tables: int | None = None) -> tuple[dict, dict]:
    """Return ``(summary_document, schema_document)`` for the configured scope."""
    type_lookup = load_type_lookup(cfg.get("tables_json"))
    databases = iter_databases(cfg)
    if only_db:
        databases = [(d, p) for d, p in databases if d == only_db]
        if not databases:
            raise SystemExit(f"no database named {only_db!r} in this config")

    cache = SummaryCache(_cache_path(cfg)) if cfg["summarizer"] == "llm" else None
    budget = cfg.get("llm_max_tables", 0) if limit_tables is None else limit_tables

    entries = []
    schema_databases = []
    total_tables = total_edges = total_isolated = total_components = 0
    databases_without_edges = 0
    total_chars = 0
    llm_calls = 0
    llm_cache_hits = 0
    llm_fallbacks = 0
    summary_sources: dict[str, int] = {}
    # (Counter would be simpler, but the dict keeps the JSON stable/sorted below)

    for db_id, path in databases:
        con = sqlite_meta.open_db(path)
        try:
            tables = sqlite_meta.list_tables(con)
            profiles = [sqlite_meta.profile_table(con, t, cfg) for t in tables]
            schema_databases.append(build_database_schema(
                con, db_id, profiles, cfg, type_lookup.get(db_id)))
        finally:
            con.close()

        summaries = _summarize_tables(profiles, db_id, cfg, cache, budget, llm_calls)
        llm_calls += sum(1 for _, source, cached in summaries
                         if source == "llm" and not cached)
        llm_cache_hits += sum(1 for _, _, cached in summaries if cached)
        llm_fallbacks += sum(1 for _, source, _ in summaries
                             if source == "template_fallback")
        cards = []
        for profile, (summary, source, _cached) in zip(profiles, summaries):
            label = "llm" if source == "llm" else "template"
            cards.append(table_card(profile, summary, label, cfg))
            summary_sources[label] = summary_sources.get(label, 0) + 1
            total_chars += len(summary)

        graph = build_graph(tables, {p["name"]: p["fk_out"] for p in profiles})
        total_tables += len(tables)
        total_edges += len(graph["edges"])
        databases_without_edges += 0 if graph["edges"] else 1
        total_isolated += len(graph["isolated"])
        total_components += len(graph["components"])

        if cache:
            cache.save()
        entries.append({
            "db_id": db_id,
            "source": os.path.basename(path),
            "n_tables": len(tables),
            "tables": cards,
            "graph": graph,
        })
        llm_here = sum(1 for _, src, _ in summaries if src == "llm")
        print(f"[db] {db_id:32s} tables={len(tables):3d} edges={len(graph['edges']):3d} "
              f"isolated={len(graph['isolated']):2d} llm={llm_here:2d}", flush=True)

    if cache:
        cache.save()

    summary = {
        "dataset": cfg["dataset"],
        "split": cfg["split"],
        "summarizer": cfg["summarizer"],
        "summary_max_chars": cfg["summary_max_chars"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": {
            "databases": len(entries),
            "tables": total_tables,
            "edges": total_edges,
            "isolated_tables": total_isolated,
            "components": total_components,
            "databases_without_edges": databases_without_edges,
            "summary_chars_total": total_chars,
            "summary_chars_mean": round(total_chars / total_tables, 1) if total_tables else 0,
            "summary_sources": dict(summary_sources),
            "llm_calls": llm_calls,
            "llm_cache_hits": llm_cache_hits,
            "llm_fallbacks": llm_fallbacks,
        },
        "databases": entries,
    }
    schema = {
        "dataset": cfg["dataset"],
        "split": cfg["split"],
        "include_adjacent_tables": True,
        "stats": {"databases": len(schema_databases),
                  "tables": sum(d["tables"].__len__() for d in schema_databases),
                  "columns": sum(len(t["columns"]) for d in schema_databases
                                 for t in d["tables"])},
        "databases": schema_databases,
    }
    return summary, schema


def _summarize_tables(profiles: list[dict], db_id: str, cfg: dict, cache,
                      budget: int, used: int) -> list[tuple[str, str]]:
    """Summarize one database's tables, in parallel when the LLM is used.

    Returns ``[(summary, source)]`` aligned with ``profiles``; ``source`` is
    ``template`` / ``llm`` / ``template_fallback``.  The cache is only written
    from the calling thread.
    """
    if cfg["summarizer"] == "template":
        return [(template_summary(p, cfg), "template", False) for p in profiles]

    todo: list[dict] = []
    results: dict[str, tuple[str, str, bool]] = {}
    for profile in profiles:
        key = SummaryCache.key(cfg, db_id, profile["name"])
        cached = cache.get(key)
        if cached:
            results[profile["name"]] = (cached, "llm", True)
        else:
            todo.append(profile)

    if todo:
        if budget and used + len(todo) > budget:
            raise SystemExit(
                f"LLM summarizer budget reached ({budget} tables). Raise "
                f"`llm_max_tables` in the config or `--limit-tables` on purpose; "
                f"cached summaries are reused on the next run.")
        workers = max(1, int(cfg.get("llm_concurrency", 6)))
        with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as pool:
            texts = list(pool.map(lambda p: llm_summary(p, db_id, cfg), todo))
        for profile, text in zip(todo, texts):
            if text:
                cache.put(SummaryCache.key(cfg, db_id, profile["name"]), text)
                results[profile["name"]] = (text, "llm", False)
            else:
                print(f"[llm] fallback to template for {db_id}.{profile['name']}")
                results[profile["name"]] = (template_summary(profile, cfg),
                                            "template_fallback", False)

    return [results[p["name"]] for p in profiles]


def _cache_path(cfg: dict) -> str:
    base = cfg.get("cache_dir") or os.path.join(cfg["exp_dir"], "outputs", "cache")
    return os.path.join(base, "llm_summaries.json")


def write_json(obj: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
