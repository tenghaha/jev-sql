"""Round-trip check for the role layer: template + slots must rebuild the AST.

The role pass replaces columns by ``Slot{role, sid}`` and records the binding in a
side table.  If that is lossless, putting the bound columns back must reproduce
the parsed AST exactly (up to source spans and raw spellings).  This is the
fidelity test for stage 3 - the analogue of the literal-conservation check for
stage 1.

Checks per record
-----------------
1. every ``Slot`` has exactly one binding with the same ``sid`` (and vice versa);
2. ``sid`` values are ``1..n`` without holes or duplicates;
3. reconstruction from ``template + slots`` equals a fresh parse of the SQL
   (after dropping ``span``/``raw``/``slots``);
4. ``AliasRef`` nodes are restored to the alias column they stood for.

Usage
-----
    python tools/verify_roles.py
    python tools/verify_roles.py --split bird_dev --show 5
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlast.intent import annotate_intent  # noqa: E402
from sqlast.parser import parse_sql  # noqa: E402


def _drop(node, keys=("span", "raw", "slots")):
    """Canonical form: no source offsets, no raw spellings, no null fields.

    Artifacts are written with ``ast_json.prune`` (which drops ``None``), so the
    fresh parse has to be normalised the same way before comparing.
    """
    if isinstance(node, dict):
        return {k: _drop(v, keys) for k, v in node.items()
                if k not in keys and v is not None}
    if isinstance(node, list):
        return [_drop(v, keys) for v in node]
    return node


def _restore(node, by_sid: dict[int, dict]):
    """Put the bound table/column/value/field back into a copy of the template."""
    if isinstance(node, list):
        for v in node:
            _restore(v, by_sid)
        return
    if not isinstance(node, dict):
        return

    t = node.get("type")

    # a table hole lives in Table.name and restores to a name, not to a node
    if t == "Table":
        name = node.get("name")
        if isinstance(name, dict) and name.get("type") == "Slot":
            node["name"] = {"parts": by_sid[name["sid"]]["table_parts"]}
        return

    # these parents held plain strings (column names), not Column nodes
    if t == "Join" and node.get("using"):
        node["using"] = [by_sid[v["sid"]]["column"] if isinstance(v, dict)
                         and v.get("type") == "Slot" else v for v in node["using"]]
        for v in (node.get("left"), node.get("right"), node.get("on")):
            _restore(v, by_sid)
        return
    if t == "SelectItem" and isinstance(node.get("star"), dict):
        star = node["star"]
        for key in ("except", "exclude"):
            if star.get(key):
                star[key] = [by_sid[v["sid"]]["column"] if isinstance(v, dict)
                             and v.get("type") == "Slot" else v for v in star[key]]
        for entry in star.get("replace") or []:
            if isinstance(entry.get("name"), dict) and entry["name"].get("type") == "Slot":
                entry["name"] = by_sid[entry["name"]["sid"]]["column"]
            _restore(entry.get("expr"), by_sid)
        return

    # field / json accessors restore their string in place
    if t in ("FieldAccess", "JsonPath"):
        key = "field" if t == "FieldAccess" else "path"
        child = node.get(key)
        if isinstance(child, dict) and child.get("type") == "Slot":
            node[key] = by_sid[child["sid"]]["field"]
        _restore(node.get("e"), by_sid)
        return

    if t == "Slot":
        b = by_sid.get(node["sid"])
        if b is None:
            raise KeyError(f"slot {node['sid']} has no binding")
        kind = b.get("kind")
        node.clear()
        if kind == "column":
            prefix = (b["qualifier"] or "").split(".") if b.get("qualifier") else []
            node.update({"type": "Column", "parts": prefix + [b["column"]]})
        elif kind == "value":
            if b.get("typed"):
                node.update({"type": "TypedLiteral", "data_type": b["typed"],
                             "value": b["value"]})
            else:
                node.update({"type": "Literal", "kind": b["value_class"],
                             "value": b["value"]})
        else:  # table / field handled by their parents
            raise ValueError(f"slot {node['sid']} of kind {kind!r} reached standalone")
        return

    if t == "AliasRef":
        name = node.get("name") or ""
        node.clear()
        node.update({"type": "Column", "parts": [name]})
        return

    for v in node.values():
        _restore(v, by_sid)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role-dir", default="outputs/roles")
    ap.add_argument("--split", nargs="*")
    ap.add_argument("--show", type=int, default=3)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.role_dir, "role_ast_*.jsonl")))
    if args.split:
        files = [f for f in files if os.path.basename(f)[11:-6] in set(args.split)]

    grand_bad = grand_n = 0
    for path in files:
        split = os.path.basename(path)[11:-6]
        n = bad_bind = bad_sid = bad_rt = 0
        examples: list[str] = []
        for line in open(path, encoding="utf-8"):
            rec = json.loads(line)
            if rec.get("status") != "ok":
                continue
            n += 1
            slots = rec["slots"]
            sids = [s["sid"] for s in slots]
            if sorted(sids) != list(range(1, len(sids) + 1)):
                bad_sid += 1
                if len(examples) < args.show:
                    examples.append(f"{rec['id']}: sid sequence broken ({sids[:8]}...)")
            by_sid = {s["sid"]: s for s in slots}

            def count_slots(node, acc):
                if isinstance(node, dict):
                    if node.get("type") == "Slot":
                        acc.append(node["sid"])
                    for v in node.values():
                        count_slots(v, acc)
                elif isinstance(node, list):
                    for v in node:
                        count_slots(v, acc)

            seen: list[int] = []
            count_slots(rec["template"], seen)
            if collections.Counter(seen) != collections.Counter(sids):
                bad_bind += 1
                if len(examples) < args.show:
                    examples.append(f"{rec['id']}: template/binding mismatch "
                                    f"({len(seen)} slots vs {len(sids)} bindings)")

            template = json.loads(json.dumps(rec["template"]))
            try:
                _restore(template, by_sid)
            except KeyError as exc:
                bad_rt += 1
                if len(examples) < args.show:
                    examples.append(f"{rec['id']}: {exc}")
                continue

            fresh, _ = parse_sql(rec["sql"], rec["dialect"])
            annotate_intent(fresh)  # the pipeline adds ``cardinality`` after parsing
            if _drop(template) != _drop(fresh):
                bad_rt += 1
                if len(examples) < args.show:
                    examples.append(f"{rec['id']}: reconstructed AST differs | "
                                    f"{' '.join(rec['sql'].split())[:120]!r}")
        grand_bad += bad_bind + bad_sid + bad_rt
        grand_n += n
        print(f"{split:26s} {n:6d} records  sid-errors={bad_sid:<3d} "
              f"binding-mismatch={bad_bind:<3d} roundtrip-diff={bad_rt}")
        for ex in examples:
            print("    ", ex)
    print(f"\ntotal {grand_n} records, {grand_bad} problems")
    return 0 if grand_bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
