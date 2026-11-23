"""Decoupling audit: is the role template really free of schema and values?

Two independent checks per gold query:

1. **Residue scan** - the template must contain
   no ``Column`` node, no ``Literal`` of kind string/number, no
   ``TypedLiteral``, every ``Table.name`` must be a ``Slot``, and the column
   lists that the AST keeps as bare strings (``JOIN ... USING``, ``* EXCEPT``,
   ``* REPLACE ... AS c``) must be slots too.

2. **Rename invariance** - rename every table part, column name, literal value,
   struct/JSON field and USING/EXCEPT name in the *parsed AST* to a fresh token,
   re-run the role pass, and require the resulting template to be
   **bit-identical** (signature + role/kind sequence) to the original one.  If a
   template still depends on a real name or value, this check fails.

Usage
-----
    python tools/verify_template.py
    python tools/verify_template.py --split spider2_snow --show 5
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
from sqlast.roles import annotate_roles, role_sequence, signature  # noqa: E402

_VALUE_KINDS = ("string", "number")
_SCHEMA_KEYS = ("parts", "value", "field", "path", "using", "except", "exclude")


# ---------------------------------------------------------------------------
# 1. residue scan
# ---------------------------------------------------------------------------
def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def residue(template: dict) -> list[str]:
    problems: list[str] = []
    for n in _walk(template):
        t = n.get("type")
        if t == "Column":
            problems.append("Column node")
        elif t == "Literal" and n.get("kind") in _VALUE_KINDS:
            problems.append("Literal with a value")
        elif t == "TypedLiteral":
            problems.append("TypedLiteral with a value")
        elif t == "Table":
            name = n.get("name")
            if not (isinstance(name, dict) and name.get("type") == "Slot"):
                problems.append("Table with a real name")
        elif t == "Join":
            for v in n.get("using") or []:
                if not (isinstance(v, dict) and v.get("type") == "Slot"):
                    problems.append("USING column as a bare string")
        elif t == "SelectItem" and isinstance(n.get("star"), dict):
            star = n["star"]
            for key in ("except", "exclude"):
                for v in star.get(key) or []:
                    if not (isinstance(v, dict) and v.get("type") == "Slot"):
                        problems.append(f"* {key} column as a bare string")
            for entry in star.get("replace") or []:
                if not (isinstance(entry.get("name"), dict)
                        and entry["name"].get("type") == "Slot"):
                    problems.append("* REPLACE column as a bare string")
        elif t in ("FieldAccess", "JsonPath"):
            key = "field" if t == "FieldAccess" else "path"
            if isinstance(n.get(key), str):
                problems.append(f"{t} with a real field name")
    return problems


def string_leaves(template: dict) -> collections.Counter:
    """Diagnostic: which keys still carry string payloads (aliases are fine)."""
    out: collections.Counter = collections.Counter()
    for n in _walk(template):
        for k, v in n.items():
            if isinstance(v, str) and k not in ("type", "role", "kind", "alias",
                                                "name", "op", "desc", "test",
                                                "ref", "field", "path"):
                out[f"{n.get('type')}.{k}"] += 1
    return out


# ---------------------------------------------------------------------------
# 2. rename invariance
# ---------------------------------------------------------------------------
class _Renamer:
    def __init__(self, aliases=frozenset()):
        self.n = 0
        self.aliases = aliases

    def token(self) -> str:
        self.n += 1
        return f"zz{self.n}"

    def rename(self, node) -> None:
        if isinstance(node, list):
            for v in node:
                self.rename(v)
            return
        if not isinstance(node, dict):
            return
        t = node.get("type")
        if t == "Table":
            name = node.get("name") or {}
            if isinstance(name.get("parts"), list):
                name["parts"] = [self.token() for _ in name["parts"]]
                name.pop("raw", None)
        elif t == "Column":
            parts = node.get("parts")
            if isinstance(parts, list) and parts:
                # a single-part name that matches a projection alias is an
                # AliasRef, not a column: renaming it would change the role pass
                is_alias_ref = len(parts) == 1 and parts[0].lower() in self.aliases
                if not is_alias_ref:
                    parts[-1] = self.token()      # keep qualifiers (aliases) as-is
        elif t == "Literal" and node.get("kind") in _VALUE_KINDS:
            node["value"] = self.token()
            node.pop("raw", None)
        elif t == "TypedLiteral":
            node["value"] = self.token()
        elif t in ("FieldAccess", "JsonPath"):
            key = "field" if t == "FieldAccess" else "path"
            if isinstance(node.get(key), str):
                node[key] = self.token()
        elif t == "Join" and node.get("using"):
            node["using"] = [self.token() for _ in node["using"]]
        elif t == "SelectItem" and isinstance(node.get("star"), dict):
            star = node["star"]
            for key in ("except", "exclude"):
                if star.get(key):
                    star[key] = [self.token() for _ in star[key]]
            for entry in star.get("replace") or []:
                if isinstance(entry.get("name"), str):
                    entry["name"] = self.token()
        for v in node.values():
            self.rename(v)


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
        n = bad_residue = bad_rename = 0
        examples: list[str] = []
        kinds: collections.Counter = collections.Counter()
        for line in open(path, encoding="utf-8"):
            rec = json.loads(line)
            if rec.get("status") != "ok":
                continue
            n += 1
            template = rec["template"]

            problems = residue(template)
            if problems:
                bad_residue += 1
                if len(examples) < args.show:
                    examples.append(f"{rec['id']}: residue {problems[:3]} "
                                    f"| {' '.join(rec['sql'].split())[:100]!r}")

            ast, _ = parse_sql(rec["sql"], rec["dialect"])
            annotate_intent(ast)
            aliases = {(n.get("alias") or "").lower() for n in _walk(ast)
                       if isinstance(n, dict) and n.get("alias")}
            aliases.discard("")
            _Renamer(aliases).rename(ast)
            renamed = annotate_roles(ast).template
            kinds.update(s["kind"] for s in rec["slots"])

            if (signature(renamed) != signature(template)
                    or role_sequence(renamed) != role_sequence(template)):
                bad_rename += 1
                if len(examples) < args.show:
                    examples.append(f"{rec['id']}: template changed after renaming "
                                    f"| {' '.join(rec['sql'].split())[:100]!r}")

        grand_bad += bad_residue + bad_rename
        grand_n += n
        print(f"{split:26s} {n:6d} templates  residue={bad_residue:<3d} "
              f"rename-invariance-fail={bad_rename:<3d} "
              f"slots={dict(sorted(kinds.items()))}")
        for ex in examples:
            print("    ", ex)
    print(f"\ntotal {grand_n} templates, {grand_bad} problems")
    return 0 if grand_bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
