"""Check how much BIRD gold SQL the CatSQL/CAT template can express.

Usage:
    conda run -n znws python scripts/cat_template_check.py
    conda run -n znws python scripts/cat_template_check.py --split train --out-dir outputs/cat_template

The CAT template (Fu et al., PVLDB 16(6), 2023, Table 1) generates:
    aquery = SELECT [CAT]+ FROM [table | nested]*
             [WHERE|HAVING] [CAT]* GROUP BY [CAT]* ORDER BY [CAT]* [LIMIT lit]?
    CAT    = AGG? DISTINCT? column OP? value? [AND|OR]? [ASC|DESC]?
    value  = literal | (literal, literal) | nested

Consequences checked here, on the parsed IR of each gold SQL:
    - no arithmetic / function call / CASE / CAST anywhere (not in the template),
    - aggregate arguments must be a bare column (no `COUNT(*)`, no expression),
    - comparison operands must be a column vs a literal or a nested query
      (no column-to-column, no expression operand),
    - projections must be column / aggregate, never `*`,
    - CTE, window functions and set operations are outside the template.

Outputs:
    <out-dir>/cat_expressibility.json   counts per split and per reason
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ir_roundtrip import Parser, Unsupported  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

EXPR_KINDS = {"bin", "fn", "cast", "case", "neg"}


def walk(node, hits: set[str]) -> None:
    if isinstance(node, dict):
        k = node.get("k")
        if k in EXPR_KINDS:
            hits.add("expression (arithmetic / function / CASE / CAST)")
        if k == "agg":
            arg = node["arg"]
            if arg.get("k") == "star":
                hits.add("AGG(*)")
            elif arg.get("k") != "col":
                hits.add("expression (arithmetic / function / CASE / CAST)")
        if k == "cmp" and node["r"].get("k") not in {"lit", "sub"}:
            hits.add("column-to-column / expression operand")
        if k == "in" and node["l"].get("k") != "col":
            hits.add("expression (arithmetic / function / CASE / CAST)")
        for v in node.values():
            walk(v, hits)
    elif isinstance(node, list):
        for v in node:
            walk(v, hits)


def check_split(rows: list[dict]) -> dict:
    reasons: Counter = Counter()
    inexpressible = parsed = parse_fail = 0
    for r in rows:
        try:
            ir = Parser(r["SQL"]).parse()
        except Unsupported as exc:
            parse_fail += 1
            reasons["outside template: " + str(exc).split(" got ")[0]] += 1
            continue
        parsed += 1
        hits: set[str] = set()
        if any(it["role"] == "star" for it in ir["project"]):
            hits.add("SELECT *")
        for key in ("project", "filter", "group", "having", "sort"):
            walk(ir[key], hits)
        if hits:
            inexpressible += 1
            for h in hits:
                reasons[h] += 1
    n = len(rows)
    return {
        "total": n,
        "parsed": parsed,
        "parse_fail": parse_fail,
        "inexpressible": inexpressible,
        "inexpressible_pct": round(100 * inexpressible / n, 1),
        "expressible_upper_bound_pct": round(100 * (n - inexpressible) / n, 1),
        "reasons": {k: [v, round(100 * v / n, 1)] for k, v in reasons.most_common()},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "train", "both"])
    ap.add_argument("--out-dir", default=str(ROOT / "outputs" / "cat_template"))
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    splits = ["dev", "train"] if args.split == "both" else [args.split]
    paths = {
        "dev": ROOT / "BIRD/dev_20240627/dev.json",
        "train": ROOT / "BIRD/train/train.json",
    }
    result = {}
    for sp in splits:
        rows = json.loads(paths[sp].read_text(encoding="utf-8"))
        result[sp] = check_split(rows)
        s = result[sp]
        print(f"[{sp}] n={s['total']} inexpressible={s['inexpressible']} "
              f"({s['inexpressible_pct']}%)  upper bound={s['expressible_upper_bound_pct']}%")
        for k, (c, p) in s["reasons"].items():
            print(f"   {c:5d} ({p:4.1f}%)  {k}")

    (out_dir / "cat_expressibility.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print("written:", out_dir)


if __name__ == "__main__":
    main()
