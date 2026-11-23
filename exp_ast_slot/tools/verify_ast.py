"""Fidelity checks for the produced AST sets.

Parse coverage alone can hide silent mis-parses (a rule that swallows the rest
of a query still "succeeds").  These checks are dialect-independent and cheap:

1. **Literal conservation** - the multiset of string/number literals in the AST
   must equal the multiset of string/number tokens in the source, modulo atoms
   the AST *re-classifies* (a quoted string used as an alias, numbers inside a
   type modifier such as ``DECIMAL(10,2)``).  This catches atoms dropped,
   duplicated or invented by the parser.
2. **Structural invariants** - every ``Select`` has a projection, every
   ``SetOp`` has two sides, every ``Query``/``CTE`` a body, ``Between`` both
   bounds, ``DISTINCT`` aggregates an argument.
3. **Data statistics that look like grammar gaps** - e.g. ``JOIN`` without
   ``ON``/``USING`` (legal in the source, so reported, not failed).

Usage
-----
    python tools/verify_ast.py                 # every split under outputs/ast
    python tools/verify_ast.py --split bird_dev
    python tools/verify_ast.py --show 5        # print offending SQL samples
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlast.intent import CARDINALITIES, cardinality_of  # noqa: E402
from sqlast.lexer import tokenize  # noqa: E402


def walk(node, out):
    """Collect every dict node carrying a ``type`` field."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "type" in cur:
                out.append(cur)
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


def ast_literals(nodes) -> collections.Counter:
    c: collections.Counter = collections.Counter()
    for n in nodes:
        t = n.get("type")
        if t == "Literal" and n.get("kind") in ("string", "number"):
            c[(n.get("kind"), str(n.get("value")))] += 1
        elif t == "TypedLiteral":
            c[("string", str(n.get("value")))] += 1
    return c


def ast_reclassified(nodes) -> tuple[collections.Counter, collections.Counter]:
    """Atoms the AST keeps but re-classifies, so conservation must allow them."""
    strings: collections.Counter = collections.Counter()
    numbers: collections.Counter = collections.Counter()
    for n in nodes:
        alias = n.get("alias")
        if isinstance(alias, str):
            strings[alias] += 1
        # a string token can also be a quoted identifier part (``T2.'Group'``)
        for part in n.get("parts") or []:
            if isinstance(part, str):
                strings[part] += 1
        types = [n.get(k) for k in ("to", "data_type", "returns")]
        types += [p.get("type") for p in (n.get("params") or []) if isinstance(p, dict)]
        for t in types:
            if isinstance(t, str):
                for m in re.findall(r"\d+", t):
                    numbers[m] += 1
    return strings, numbers


def source_literals(sql: str, dialect: str) -> collections.Counter:
    c: collections.Counter = collections.Counter()
    for tok in tokenize(sql, dialect):
        if tok.kind == "num":
            c[("number", tok.text)] += 1
        elif tok.kind == "string":
            c[("string", tok.value)] += 1
    return c


def check_invariants(nodes) -> list[str]:
    problems = []
    for n in nodes:
        t = n.get("type")
        if t == "Select" and not n.get("items"):
            problems.append("Select without items")
        if t == "SetOp" and not (n.get("l") and n.get("r")):
            problems.append("SetOp missing a side")
        if t == "Query" and not n.get("body"):
            problems.append("Query without body")
        if t == "CTE" and not n.get("body"):
            problems.append("CTE without body")
        if t == "Between" and not (n.get("lo") and n.get("hi")):
            problems.append("Between missing bound")
        if t == "Func" and n.get("distinct") and not n.get("args"):
            problems.append("DISTINCT aggregate without argument")
        if t == "Query":
            label = n.get("cardinality")
            if label not in CARDINALITIES:
                problems.append(f"Query without a valid cardinality ({label!r})")
            elif cardinality_of(n) != label:
                problems.append("cardinality label disagrees with the structure")
    return problems


def join_stats(nodes) -> int:
    """Number of joins with no ON / USING (legal but worth counting)."""
    n = 0
    for node in nodes:
        if node.get("type") == "Join" and not (
                node.get("on") or node.get("using") or node.get("natural")
                or node.get("kind") == "CROSS"):
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ast-dir", default="outputs/ast")
    ap.add_argument("--split", nargs="*")
    ap.add_argument("--show", type=int, default=3, help="samples per failure kind")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.ast_dir, "ast_*.jsonl")))
    if args.split:
        files = [f for f in files if os.path.basename(f)[4:-6] in set(args.split)]

    grand_bad = grand_n = grand_explained = 0
    for path in files:
        split = os.path.basename(path)[4:-6]
        n = lit_bad = struct_bad = explained = joinless = 0
        examples: list[str] = []
        for line in open(path, encoding="utf-8"):
            rec = json.loads(line)
            if rec.get("status") != "ok":
                continue
            n += 1
            nodes = walk(rec["ast"], [])
            got = ast_literals(nodes)
            want = source_literals(rec["sql"], rec["dialect"])
            ex_str, ex_num = ast_reclassified(nodes)
            allow = collections.Counter()
            allow.update({("string", s): c for s, c in ex_str.items()})
            allow.update({("number", s): c for s, c in ex_num.items()})
            missing = (want - got) - allow
            extra = got - want
            explained += sum(((want - got) & allow).values())
            if missing or extra:
                lit_bad += 1
                if len(examples) < args.show:
                    examples.append(f"{rec['id']} missing={dict(missing)} "
                                    f"extra={dict(extra)} | {rec['sql'][:140]!r}")
            probs = check_invariants(nodes)
            joinless += join_stats(nodes)
            if probs:
                struct_bad += 1
                if len(examples) < args.show:
                    examples.append(f"{rec['id']} {probs[:2]} | {rec['sql'][:140]!r}")
        grand_bad += lit_bad + struct_bad
        grand_n += n
        grand_explained += explained
        print(f"{split:26s} {n:6d} asts  literal-mismatch={lit_bad:<3d} "
              f"invariant-violation={struct_bad:<3d} explained-atoms={explained:<5d} "
              f"joins-without-predicate={joinless}")
        for ex in examples:
            print("    ", ex)
    print(f"\ntotal {grand_n} ASTs, {grand_bad} problems, "
          f"{grand_explained} re-classified atoms")
    return 0 if grand_bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
