"""Cluster parse failures of one split by (message, offending token).

Usage
-----
    python tools/diagnose_parse.py bird_dev [--top 15] [--context 70]

Reads the split definitions from ``configs/datasets.yaml`` and prints, per
failure cluster, the count, the token the parser stopped at and one snippet of
source around it.  This is the feedback loop used to grow the grammar: each
cluster is one missing production or one over-eager rule.
"""

from __future__ import annotations

import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlast.dataset import iter_records, load_splits  # noqa: E402
from sqlast.parser import ParseError, parse_sql  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("split")
    ap.add_argument("--config", default="configs/datasets.yaml")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--context", type=int, default=70)
    args = ap.parse_args()

    splits, _ = load_splits(args.config)
    target = next((s for s in splits if s.name == args.split), None)
    if target is None:
        raise SystemExit(f"no such split: {args.split}")

    counts: collections.Counter = collections.Counter()
    examples: dict[tuple[str, str], str] = {}
    total = 0
    for rec in iter_records(target):
        total += 1
        try:
            parse_sql(rec["sql"], rec["dialect"], spans=True)
        except ParseError as exc:
            tok = exc.tok
            key = (exc.msg, "<eof>" if tok.kind == "eof" else tok.text)
            counts[key] += 1
            if key not in examples:
                lo = max(0, tok.start - args.context)
                examples[key] = rec["sql"][lo:tok.start + args.context].replace("\n", " ")
        except Exception as exc:  # parser bug, not a grammar gap
            key = (f"BUG {type(exc).__name__}", str(exc)[:60])
            counts[key] += 1
            examples.setdefault(key, rec["sql"][:2 * args.context].replace("\n", " "))

    bad = sum(counts.values())
    print(f"{args.split}: {total - bad}/{total} parsed, {bad} failures, "
          f"{len(counts)} clusters")
    for (msg, tok), c in counts.most_common(args.top):
        print(f"{c:5d}  [{msg}] at token {tok!r}")
        print(f"        ...{examples[(msg, tok)]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
