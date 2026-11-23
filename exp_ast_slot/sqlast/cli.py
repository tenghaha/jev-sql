"""Command line entry point for the AST / semantic-slot pipeline.

Usage
-----
Run from the experiment directory (``exp_ast_slot``)::

    python -m sqlast.cli parse    --config configs/datasets.yaml [--split NAME ...]
    python -m sqlast.cli slots    --config configs/datasets.yaml [--split NAME ...]
    python -m sqlast.cli show     --sql "SELECT 1" --dialect sqlite

``parse`` writes one AST set per split plus a coverage report; ``slots`` writes
the slot-annotated AST sets plus slot distribution statistics.  Both read the
split definitions from the YAML config, so adding a dataset never touches code.

Outputs (relative to the config's ``out_dir``)::

    ast/ast_<split>.jsonl        one record per gold SQL, incl. failures
    ast/ast_<split>.json         JSON array of the same records
    ast/rules.json               grammar-rule hit counts per split
    slots/ast_slots_<split>.jsonl / .json
    reports/parse_report.md      coverage per split + error clusters
    reports/slot_report.md       slot distributions per split
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
import traceback

from .ast_json import prune
from .dataset import iter_records, load_splits
from .intent import CARDINALITIES, annotate_intent
from .parser import ParseError, Unsupported, parse_sql

__all__ = ["main"]


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------
def _error_signature(exc: Exception) -> str:
    msg = str(exc)
    if isinstance(exc, ParseError):
        # keep the message, drop offsets/near-token noise so clusters collapse
        return msg.split(" at offset ")[0]
    return f"{type(exc).__name__}: {msg.splitlines()[0][:160]}"


def cmd_parse(args) -> int:
    splits, meta = load_splits(args.config)
    out_dir = meta["out_dir"]
    ast_dir = os.path.join(out_dir, "ast")
    rep_dir = os.path.join(out_dir, "reports")
    os.makedirs(ast_dir, exist_ok=True)
    os.makedirs(rep_dir, exist_ok=True)

    wanted = set(args.split) if args.split else None
    summary: dict[str, dict] = {}
    all_rules: dict[str, dict] = {}

    for split in splits:
        if wanted and split.name not in wanted:
            continue
        t0 = time.time()
        records: list[dict] = []
        cardinality: collections.Counter = collections.Counter()
        errors: collections.Counter = collections.Counter()
        error_examples: dict[str, list[str]] = {}
        rules: collections.Counter = collections.Counter()
        ok = 0
        for rec in iter_records(split):
            sql = rec["sql"]
            base = {k: rec[k] for k in ("id", "split", "db_id", "dialect")}
            try:
                ast, hits = parse_sql(sql, dialect=rec["dialect"], spans=args.spans)
                annotate_intent(ast)
                rules.update(hits)
                for stmt in ast.get("stmts") or []:
                    if stmt.get("type") == "Query":
                        cardinality[stmt.get("cardinality", "?")] += 1
                ok += 1
                row = dict(base, status="ok", sql=sql,
                           question=rec["question"], evidence=rec["evidence"],
                           ast=ast)
            except (ParseError, Unsupported) as exc:
                sig = _error_signature(exc)
                errors[sig] += 1
                error_examples.setdefault(sig, [])
                if len(error_examples[sig]) < 3:
                    error_examples[sig].append(sql.strip()[:300])
                row = dict(base, status="error", phase="parse", sql=sql,
                           question=rec["question"], evidence=rec["evidence"],
                           error=str(exc), error_signature=sig)
            except Exception as exc:  # unexpected: keep the run alive, record it
                sig = f"BUG {type(exc).__name__}: {exc}"
                errors[sig] += 1
                error_examples.setdefault(sig, [])
                if len(error_examples[sig]) < 3:
                    error_examples[sig].append(sql.strip()[:300])
                row = dict(base, status="error", phase="parser-bug", sql=sql,
                           question=rec["question"], evidence=rec["evidence"],
                           error=traceback.format_exc(limit=3),
                           error_signature=sig)
            records.append(row)

        n = len(records)
        name = split.name
        _write_jsonl(os.path.join(ast_dir, f"ast_{name}.jsonl"), records)
        _write_json(os.path.join(ast_dir, f"ast_{name}.json"),
                    [prune(r) for r in records])
        summary[name] = {
            "split": name,
            "dialect": "mixed" if split.dialect_by_prefix else split.dialect,
            "n": n,
            "ok": ok,
            "errors": n - ok,
            "coverage": round(ok / n, 4) if n else 0.0,
            "seconds": round(time.time() - t0, 2),
            "cardinality": dict(cardinality),
            "error_clusters": [{"signature": s, "count": c,
                                "examples": error_examples[s]}
                               for s, c in errors.most_common()],
        }
        all_rules[name] = dict(sorted(rules.items()))
        print(f"[parse] {name:26s} {ok:6d}/{n:<6d} ok  "
              f"({100.0 * ok / n if n else 0:5.1f}%)  {time.time() - t0:5.1f}s")

    _write_json(os.path.join(ast_dir, "rules.json"), all_rules)
    _write_json(os.path.join(rep_dir, "parse_summary.json"), summary)
    _write_text(os.path.join(rep_dir, "parse_report.md"),
                render_parse_report(summary, all_rules))
    return 0


def _render_cardinality(summary: dict, split_names: list[str]) -> list[str]:
    """Table of query-level intent counts (see sqlast/intent.py)."""
    lines = ["| intent | " + " | ".join(split_names) + " |",
             "|---" * (1 + len(split_names)) + "|"]
    for value in CARDINALITIES:
        cells = []
        for name in split_names:
            counts = summary[name].get("cardinality") or {}
            total = sum(counts.values()) or 1
            n = counts.get(value, 0)
            cells.append(f"{n} ({100.0 * n / total:.1f}%)")
        lines.append(f"| `{value}` | " + " | ".join(cells) + " |")
    return lines


def render_parse_report(summary: dict, rules: dict) -> str:
    lines = ["# Gold-SQL parse coverage", "",
             "| split | dialect | queries | parsed | coverage |",
             "|---|---|---:|---:|---:|"]
    for name, s in summary.items():
        lines.append(f"| {name} | {s['dialect']} | {s['n']} | {s['ok']} | "
                     f"{100.0 * s['coverage']:.2f}% |")
    tot_n = sum(s["n"] for s in summary.values())
    tot_ok = sum(s["ok"] for s in summary.values())
    lines += ["", f"**Total**: {tot_ok}/{tot_n} "
                  f"({100.0 * tot_ok / tot_n if tot_n else 0:.2f}%)", ""]

    lines += ["## Query intent (`cardinality`)", "",
              "Assigned by `sqlast/intent.py` in a post-pass over the AST:", "",
              "| value | meaning |", "|---|---|",
              "| `SINGLE` | one row by construction (aggregate-only projection, or no FROM) |",
              "| `TOP_K` | the query has a LIMIT (at most k rows) |",
              "| `GROUPED` | GROUP BY without LIMIT (one row per group) |",
              "| `LIST` | multi-row list (default) |",
              "| `SET` | de-duplicated rows (projection DISTINCT, or a set operation without ALL) |",
              ""]
    lines += _render_cardinality(summary, list(summary))
    lines.append("")

    lines += ["## Grammar-rule hit counts", ""]
    all_ids: dict[str, dict[str, int]] = {}
    for name, hits in rules.items():
        for rid, c in hits.items():
            all_ids.setdefault(rid, {})[name] = c
    split_names = list(rules)
    head = "| rule id | " + " | ".join(split_names) + " |"
    lines += [head, "|---" * (len(split_names) + 1) + "|"]
    for rid in sorted(all_ids):
        cells = " | ".join(str(all_ids[rid].get(s, "")) for s in split_names)
        lines.append(f"| `{rid}` | {cells} |")
    lines.append("")

    any_err = False
    for name, s in summary.items():
        if not s["error_clusters"]:
            continue
        any_err = True
        lines += [f"## Unparsed constructs in `{name}`", ""]
        for cl in s["error_clusters"]:
            lines.append(f"- **{cl['count']}x** `{cl['signature']}`")
            for ex in cl["examples"]:
                lines.append(f"  - `{ex[:180]}`")
        lines.append("")
    if not any_err:
        lines += ["## Unparsed constructs", "", "None.", ""]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# slots
# ---------------------------------------------------------------------------
# roles: keep the AST skeleton, replace tables/columns/values by slot holes
# ---------------------------------------------------------------------------
def cmd_roles(args) -> int:
    from .roles import (KINDS, ROLE_SPEC, ROLES, annotate_roles, role_stats,
                        signature)
    from .schema import load_schema

    splits, meta = load_splits(args.config)
    out_dir = meta["out_dir"]
    slot_dir = os.path.join(out_dir, "roles")
    rep_dir = os.path.join(out_dir, "reports")
    os.makedirs(slot_dir, exist_ok=True)
    os.makedirs(rep_dir, exist_ok=True)

    wanted = set(args.split) if args.split else None
    summary: dict[str, dict] = {}
    stats: dict[str, dict] = {}
    sig_sets: dict[str, dict[str, set]] = {}
    example_records = None

    for split in splits:
        if wanted and split.name not in wanted:
            continue
        t0 = time.time()
        schema = load_schema(split.tables)
        records: list[dict] = []
        for rec in iter_records(split):
            base = {k: rec[k] for k in ("id", "split", "db_id", "dialect")}
            try:
                ast, _ = parse_sql(rec["sql"], dialect=rec["dialect"], spans=True)
            except Exception as exc:
                records.append(dict(base, status="error", sql=rec["sql"],
                                    question=rec["question"],
                                    evidence=rec["evidence"], error=str(exc)))
                continue
            annotate_intent(ast)
            annotated = annotate_roles(ast, schema=schema, db_id=rec["db_id"],
                                       slot_tables=not args.keep_tables,
                                       slot_values=not args.keep_values)
            # second level for the ablation ladder: columns only
            cols_only = annotate_roles(ast, schema=schema, db_id=rec["db_id"],
                                       slot_tables=False, slot_values=False)
            records.append(dict(
                base, status="ok", sql=rec["sql"],
                question=rec["question"], evidence=rec["evidence"],
                template=annotated.template,
                slots=annotated.slots,
                signatures={"ast": signature(ast),
                            "cols_only": signature(cols_only.template),
                            "template": signature(annotated.template)},
            ))
        n = len(records)
        ok = sum(1 for r in records if r["status"] == "ok")
        name = split.name
        _write_jsonl(os.path.join(slot_dir, f"role_ast_{name}.jsonl"), records)
        _write_json(os.path.join(slot_dir, f"role_ast_{name}.json"),
                    [prune(r) for r in records])
        if example_records is None:
            example_records = records
        st = role_stats(records)
        st["coverage"] = round(ok / n, 4) if n else 0.0
        st["with_schema"] = bool(split.tables)
        sig_sets[name] = {lvl: {r["signatures"][lvl] for r in records
                                if r.get("status") == "ok"}
                          for lvl in ("ast", "cols_only", "template")}
        cardinality: collections.Counter = collections.Counter()
        for r in records:
            if r.get("status") != "ok":
                continue
            for stmt in r["template"].get("stmts") or []:
                if stmt.get("type") == "Query":
                    cardinality[stmt.get("cardinality", "?")] += 1
        st["cardinality"] = dict(cardinality)
        stats[name] = st
        summary[name] = {"split": name, "n": n, "ok": ok,
                         "coverage": round(ok / n, 4) if n else 0.0,
                         "slots": sum(st["kind_slots"].values()),
                         "kind_slots": st["kind_slots"],
                         "cardinality": dict(cardinality),
                         "seconds": round(time.time() - t0, 2)}
        print(f"[roles] {name:26s} {ok:6d}/{n:<6d} ok  "
              f"{sum(st['kind_slots'].values()):6d} slots  {time.time() - t0:5.1f}s")

    _write_json(os.path.join(slot_dir, "role_schema.json"),
                {"roles": list(ROLES), "kinds": list(KINDS), "spec": ROLE_SPEC,
                 "slot_node": {"type": "Slot", "role": "one of ROLES",
                               "kind": "one of KINDS", "sid": "1..n per query"},
                 "clause_map": {"every table reference": "TABLE",
                                "JOIN ... ON/USING": "JOINKEY",
                                "IN/EXISTS/=(SELECT ...) subquery projection": "JOINKEY",
                                "SELECT (aggregated or not)": "MEASURE",
                                "inside an aggregate (any clause)": "MEASURE",
                                "GROUP BY / window PARTITION BY": "DIMENSION",
                                "WHERE / HAVING / QUALIFY": "FILTER",
                                "ORDER BY / window ORDER BY": "SORT",
                                "LIMIT / OFFSET count": "FILTER (kind=value)"},
                 "alias_refs_kept": "ORDER BY <projection alias> stays an AliasRef node, not a slot",
                 "keywords_kept": "NULL / TRUE / FALSE stay Literal nodes (keywords, not data)",
                 "cardinality": {
                     "field": "Query.cardinality",
                     "values": list(CARDINALITIES),
                     "precedence": ["GROUP BY + LIMIT -> TOP_K", "GROUP BY -> GROUPED",
                                    "aggregate-only projection -> SINGLE",
                                    "no FROM (and no window) -> SINGLE",
                                    "LIMIT -> TOP_K", "projection DISTINCT -> SET",
                                    "set operation without ALL -> SET", "else -> LIST"],
                     "note": "deterministic post-pass in sqlast/intent.py; "
                             "excluded from template signatures because it is implied by them"}})
    transfer = _template_transfer(sig_sets)
    _write_json(os.path.join(rep_dir, "role_stats.json"), stats)
    _write_json(os.path.join(rep_dir, "role_summary.json"), summary)
    _write_json(os.path.join(rep_dir, "role_transfer.json"), transfer)
    _write_text(os.path.join(rep_dir, "role_report.md"),
                render_role_report(stats, summary, transfer,
                                   example_records if args.show_example else None))
    return 0


#: (reference split, evaluation split) pairs worth measuring template transfer on:
#: BIRD dev/train share no database at all, so a hit can only come from structure.
TRANSFER_PAIRS = (("bird_train", "bird_dev"),
                  ("bird_train", "bird_minidev_sqlite"),
                  ("spider2_lite", "spider2_snow"))


def _template_transfer(sig_sets: dict) -> dict:
    out = []
    for source, target in TRANSFER_PAIRS:
        if source not in sig_sets or target not in sig_sets:
            continue
        row = {"source": source, "target": target}
        for level in ("ast", "cols_only", "template"):
            tgt = sig_sets[target][level]
            hit = len(tgt & sig_sets[source][level])
            row[level] = {"covered": hit, "of": len(tgt),
                          "rate": round(hit / len(tgt), 4) if tgt else 0.0}
        out.append(row)
    return {"pairs": out}


def render_role_report(stats: dict, summary: dict, transfer: dict | None = None,
                       dev_records=None) -> str:
    from .roles import KINDS, ROLES

    split_names = list(stats)
    lines = ["# Role slots: templates free of schema and values", "",
             "Every table reference, column and literal value is replaced by a",
             "`Slot{role, kind, sid}`; the rest of the AST branch structure is kept.", "",
             "| role | what the hole is for |", "|---|---|"]
    from .roles import ROLE_SPEC
    for spec in ROLE_SPEC:
        lines.append(f"| `{spec['role']}` | {spec['meaning']} |")
    lines += ["", "Slot kinds: `table` / `column` / `value` / `field`.", "",
              "| split | queries | slots | table | column | value | field | slots/query | schema |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for name, s in summary.items():
        c = stats[name]["kind_slots"]
        spq = s["slots"] / s["ok"] if s["ok"] else 0.0
        lines.append(f"| {name} | {s['n']} | {s['slots']} | {c.get('table', 0)} | "
                     f"{c.get('column', 0)} | {c.get('value', 0)} | {c.get('field', 0)} | "
                     f"{spq:.2f} | {'yes' if stats[name]['with_schema'] else 'no'} |")
    lines.append("")

    lines += ["## Column roles", "",
              "| role | " + " | ".join(split_names) + " |",
              "|---" * (1 + len(split_names)) + "|"]
    for role in [r for r in ROLES if r != "TABLE"]:
        cells = []
        for n in split_names:
            total = sum(stats[n]["role_column"].values()) or 1
            v = stats[n]["role_column"].get(role, 0)
            cells.append(f"{v} ({100.0 * v / total:.1f}%)")
        lines.append(f"| `{role}` | " + " | ".join(cells) + " |")
    lines.append("")

    lines += ["## Queries touching each column role", "",
              "| role | " + " | ".join(split_names) + " |",
              "|---" * (1 + len(split_names)) + "|"]
    for role in [r for r in ROLES if r != "TABLE"]:
        cells = []
        for n in split_names:
            q = stats[n]["queries"] or 1
            v = stats[n]["queries_role_column"].get(role, 0)
            cells.append(f"{v} ({100.0 * v / q:.0f}%)")
        lines.append(f"| `{role}` | " + " | ".join(cells) + " |")
    lines.append("")

    lines += ["## Value slots by role", "",
              "Where the concrete values sit (a value takes the role of its clause):", "",
              "| role | " + " | ".join(split_names) + " |",
              "|---" * (1 + len(split_names)) + "|"]
    for role in [r for r in ROLES if r != "TABLE"]:
        cells = []
        for n in split_names:
            total = sum(stats[n]["role_value"].values()) or 1
            v = stats[n]["role_value"].get(role, 0)
            cells.append(f"{v} ({100.0 * v / total:.1f}%)")
        lines.append(f"| `{role}` | " + " | ".join(cells) + " |")
    lines.append("")

    lines += ["## Query intent (`cardinality`)", ""]
    lines += _render_cardinality(summary, split_names)
    lines.append("")

    lines += ["## Template de-duplication", "",
              "Three levels of normalisation (literal values dropped in all of them):", "",
              "| level | " + " | ".join(split_names) + " |",
              "|---" * (1 + len(split_names)) + "|"]
    for level, label in (("ast", "names kept (tables + columns)"),
                         ("cols_only", "columns -> roles"),
                         ("template", "columns + tables + values -> slots")):
        cells = []
        for n in split_names:
            t = stats[n]["templates"][level]
            cells.append(f"{t['distinct']} ({t['singletons']} singletons)")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")

    if transfer and transfer.get("pairs"):
        lines += ["## Cross-split template transfer", "",
                  "Fraction of the *evaluation* split's distinct signatures that also",
                  "occur in the *reference* split.  BIRD dev and train share no database,",
                  "so a hit comes from structure rather than schema memory:", "",
                  "| reference -> evaluation | names kept | columns->roles | full template |",
                  "|---|---:|---:|---:|"]
        for row in transfer["pairs"]:
            cells = [f"{row[l]['covered']}/{row[l]['of']} "
                     f"({100.0 * row[l]['rate']:.1f}%)"
                     for l in ("ast", "cols_only", "template")]
            lines.append(f"| {row['source']} -> {row['target']} | "
                         + " | ".join(cells) + " |")
        lines.append("")

    lines += ["## Most common role sequences", ""]
    for name in split_names:
        seqs = stats[name]["role_sequences"][:5]
        lines += [f"**{name}**", "", "| count | roles | example SQL |", "|---:|---|---|"]
        for row in seqs:
            ex = row["example_sql"].replace("|", "\\|")
            lines.append(f"| {row['count']} | `{row['sequence'] or '(no slots)'}` | "
                         f"`{ex[:110]}` |")
        lines.append("")

    lines += ["## Role sanity checks (column slots only)", "",
              "| split | SUM/AVG over a text column | JOINKEY resolved | JOINKEY on PK/FK |",
              "|---|---:|---:|---:|"]
    for name in split_names:
        s = stats[name]["sanity"]
        jk = s["joinkey_with_schema"]
        frac = f"{s['joinkey_on_pk_or_fk']}/{jk}" if jk else "-"
        lines.append(f"| {name} | {s['measure_sum_on_text']} | {jk} | {frac} |")
    lines.append("")

    if dev_records:
        ex = next((r for r in dev_records if r.get("status") == "ok"), None)
        if ex:
            lines += ["## Worked example", "",
                      f"Q: {ex.get('question')}", "",
                      f"SQL: `{' '.join(ex['sql'].split())[:400]}`", "",
                      "```json",
                      json.dumps(ex["template"]["stmts"][-1], ensure_ascii=False)[:1400],
                      "```", "",
                      "| sid | role | kind | filled with | path |",
                      "|---:|---|---|---|---|"]
            for s in ex["slots"]:
                fill = (s.get("column") or s.get("value") or s.get("table") or "-")
                lines.append(f"| {s['sid']} | `{s['role']}` | {s['kind']} | "
                             f"`{fill}` | `{s['path'].replace('stmts[]', 'stmt')}` |")
            lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# inspect one statement
# ---------------------------------------------------------------------------
def cmd_show(args) -> int:
    from .ast_json import dumps
    ast, rules = parse_sql(args.sql, dialect=args.dialect, spans=args.spans)
    out = ast
    if args.roles:
        from .roles import annotate_roles, role_sequence
        annotated = annotate_roles(ast, question=args.question,
                                   evidence=args.evidence)
        print(dumps(annotated.template, indent=2))
        print(json.dumps({"role_sequence": role_sequence(annotated.template),
                          "slots": annotated.slots}, ensure_ascii=False, indent=2))
        out = None
    else:
        print(dumps(out, indent=2))
    if args.rules:
        print(json.dumps(rules, indent=2, sort_keys=True), file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# io helpers
# ---------------------------------------------------------------------------
def _write_jsonl(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(prune(row), ensure_ascii=False) + "\n")


def _write_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sqlast", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="parse every gold SQL and write AST sets")
    p.add_argument("--config", default="configs/datasets.yaml")
    p.add_argument("--split", nargs="*", help="subset of split names")
    p.add_argument("--spans", action="store_true",
                   help="keep [start,end) character offsets on AST nodes")
    p.set_defaults(func=cmd_parse)

    r = sub.add_parser("roles", help="replace columns by role slots (schema-free template)")
    r.add_argument("--config", default="configs/datasets.yaml")
    r.add_argument("--split", nargs="*")
    r.add_argument("--show-example", action="store_true",
                   help="include a worked example in the markdown report")
    r.add_argument("--keep-tables", action="store_true",
                   help="ablation: leave table names in the template")
    r.add_argument("--keep-values", action="store_true",
                   help="ablation: leave literal values in the template")
    r.set_defaults(func=cmd_roles)

    sh = sub.add_parser("show", help="parse one statement and print the AST")
    sh.add_argument("--sql", required=True)
    sh.add_argument("--dialect", default="sqlite")
    sh.add_argument("--spans", action="store_true")
    sh.add_argument("--roles", action="store_true")
    sh.add_argument("--question", default=None)
    sh.add_argument("--evidence", default=None)
    sh.add_argument("--rules", action="store_true")
    sh.set_defaults(func=cmd_show)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_argparser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
