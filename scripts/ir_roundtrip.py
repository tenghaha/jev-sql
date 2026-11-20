"""Prototype: gold SQL -> semantic IR -> SQL, with round-trip execution check.

Usage:
    conda run -n znws python scripts/ir_roundtrip.py
    conda run -n znws python scripts/ir_roundtrip.py --split train --limit 500

Purpose:
    Measure whether a *semantic* intermediate representation carries enough
    information to regenerate an equivalent SQL. This is the self-verification
    loop used to derive and audit decompiler rules without any model training.

Method:
    1. parse gold SQL into an IR (clause operators + expression nodes),
    2. lower the IR back to SQL,
    3. execute both statements on the BIRD sqlite database,
    4. compare result bags (ordered when ORDER BY + LIMIT, else unordered),
    5. report per-rule trigger counts and failures.

Inputs:
    BIRD/dev_20240627/dev.json (question, evidence, SQL, db_id)
    BIRD/dev_20240627/dev_databases/<db_id>/<db_id>.sqlite

Outputs:
    outputs/ir_roundtrip/roundtrip.jsonl   per-query verdict
    outputs/ir_roundtrip/report.md         rule table + failure diagnosis
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

AGG_FUNCS = {"COUNT", "SUM", "AVG", "MIN", "MAX", "TOTAL", "GROUP_CONCAT"}
CLAUSE_STOP = {
    "FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "UNION",
    "INTERSECT", "EXCEPT", "WINDOW", "BY", "AS", "ON", "USING", "AND", "OR",
    "THEN", "ELSE", "END", "WHEN", "ASC", "DESC", "IS", "IN", "LIKE", "BETWEEN",
    "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "OUTER", "NATURAL",
    "OVER", "PARTITION",
}
JOIN_WORDS = {"INNER", "LEFT", "RIGHT", "FULL", "CROSS", "OUTER", "NATURAL"}


class Unsupported(Exception):
    """Gold SQL uses a construct the prototype does not model yet."""


# --------------------------------------------------------------------------- #
# tokenizer
# --------------------------------------------------------------------------- #
class Tok:
    __slots__ = ("kind", "text")

    def __init__(self, kind: str, text: str) -> None:
        self.kind = kind
        self.text = text

    def __repr__(self) -> str:
        return f"{self.kind}:{self.text}"


def tokenize(sql: str) -> list[Tok]:
    toks: list[Tok] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in " \t\r\n":
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c in "'\"":
            quote = c
            j = i + 1
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            toks.append(Tok("STR" if quote == "'" else "IDENT", sql[i:j]))
            i = j
            continue
        if c == "`":
            j = sql.find("`", i + 1)
            j = n if j < 0 else j + 1
            toks.append(Tok("IDENT", sql[i:j]))
            i = j
            continue
        if c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = i
            while j < n and (sql[j].isdigit() or sql[j] in ".eE"):
                j += 1
            toks.append(Tok("NUM", sql[i:j]))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            toks.append(Tok("WORD", sql[i:j]))
            i = j
            continue
        if c in "(),;":
            toks.append(Tok("PUNCT", c))
            i += 1
            continue
        for op in ("<=", ">=", "<>", "!=", "==", "||", "::"):
            if sql.startswith(op, i):
                toks.append(Tok("OP", op))
                i += len(op)
                break
        else:
            toks.append(Tok("OP", c))
            i += 1
    return toks


def up(t: Tok | None) -> str:
    return t.text.upper() if (t is not None and t.kind == "WORD") else ""


# --------------------------------------------------------------------------- #
# parser: SQL -> IR
# --------------------------------------------------------------------------- #
class Parser:
    def __init__(self, sql: str) -> None:
        self.toks = [t for t in tokenize(sql) if not (t.kind == "PUNCT" and t.text == ";")]
        self.i = 0
        self.rules: set[str] = set()

    # ---- token helpers
    def peek(self, k: int = 0) -> Tok | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def at(self, text: str) -> bool:
        t = self.peek()
        return t is not None and t.text.upper() == text

    def take(self) -> Tok:
        t = self.peek()
        if t is None:
            raise Unsupported("unexpected end of input")
        self.i += 1
        return t

    def expect(self, text: str) -> None:
        if not self.at(text):
            t = self.peek()
            raise Unsupported(f"expected {text}, got {t.text if t else 'EOF'}")
        self.i += 1

    # ---- entry
    def parse(self) -> dict:
        if self.at("WITH"):
            raise Unsupported("cte")
        q = self.parse_query()
        if self.peek() is not None:
            raise Unsupported(f"trailing token {self.peek().text}")
        return q

    def parse_query(self) -> dict:
        self.expect("SELECT")
        q: dict = {}
        if self.at("DISTINCT"):
            self.take()
            q["distinct"] = True
            self.rules.add("proj.distinct")
        else:
            q["distinct"] = False
        items = [self.parse_select_item()]
        while self.at(","):
            self.take()
            items.append(self.parse_select_item())
        q["project"] = items
        if self.at("FROM"):
            q["from"] = self.parse_from()
        else:
            q["from"] = {"scans": [], "joins": []}
            self.rules.add("from.none")
        q["filter"] = None
        q["group"] = []
        q["having"] = None
        q["sort"] = []
        q["limit"] = None
        q["offset"] = None
        if self.at("WHERE"):
            self.take()
            q["filter"] = self.parse_expr(0)
            self.rules.add("filter.where")
        if self.at("GROUP"):
            self.take()
            self.expect("BY")
            q["group"] = self.parse_expr_list()
            self.rules.add("group.by")
        if self.at("HAVING"):
            self.take()
            q["having"] = self.parse_expr(0)
            self.rules.add("having")
        if self.at("ORDER"):
            self.take()
            self.expect("BY")
            q["sort"] = self.parse_sort()
            self.rules.add("sort.order_by")
        if self.at("LIMIT"):
            self.take()
            first = self.parse_int()
            if self.at(","):
                self.take()
                q["offset"] = first
                q["limit"] = self.parse_int()
            else:
                q["limit"] = first
                if self.at("OFFSET"):
                    self.take()
                    q["offset"] = self.parse_int()
            self.rules.add("limit")
        if self.at("UNION") or self.at("INTERSECT") or self.at("EXCEPT"):
            raise Unsupported("set_operation")
        if self.at("WINDOW"):
            raise Unsupported("window")
        q["cardinality"] = self.cardinality(q)
        return q

    def parse_int(self) -> int:
        t = self.peek()
        if t is None or t.kind != "NUM":
            raise Unsupported("LIMIT/OFFSET expects a number")
        self.take()
        return int(float(t.text))

    def parse_select_item(self) -> dict:
        if self.at("*"):
            self.take()
            self.rules.add("proj.star")
            return {"role": "star", "expr": {"k": "star"}}
        expr = self.parse_expr(0)
        if self.at("OVER"):
            raise Unsupported("window")
        alias = None
        if self.at("AS"):
            self.take()
            alias = self.take().text
        elif self.peek() is not None and self.peek().kind in {"WORD", "IDENT"} and up(self.peek()) not in CLAUSE_STOP:
            alias = self.take().text
        role = "expression"
        if expr["k"] == "agg":
            role = "aggregate"
            self.rules.add("proj.aggregate")
        elif expr["k"] == "col":
            role = "column"
            self.rules.add("proj.column")
        else:
            self.rules.add("proj.expression")
        if alias:
            self.rules.add("proj.alias")
        return {"role": role, "expr": expr, "alias": alias}

    def parse_from(self) -> dict:
        self.expect("FROM")
        from_: dict = {"scans": [], "joins": []}
        ref = self.parse_table_ref()
        from_["scans"].append(ref)
        while True:
            if self.at(","):
                self.take()
                ref = self.parse_table_ref()
                from_["scans"].append(ref)
                self.rules.add("from.comma_join")
                continue
            jw = up(self.peek())
            if jw in JOIN_WORDS or jw == "JOIN":
                kind = "INNER"
                while up(self.peek()) in JOIN_WORDS:
                    w = self.take().text.upper()
                    if w in {"LEFT", "RIGHT", "FULL", "CROSS"}:
                        kind = w
                self.expect("JOIN")
                ref = self.parse_table_ref()
                on = None
                if self.at("ON"):
                    self.take()
                    on = self.parse_expr(0)
                elif self.at("USING"):
                    raise Unsupported("join_using")
                from_["joins"].append({"kind": kind, "ref": ref, "on": on})
                self.rules.add(f"from.join.{kind.lower()}")
                continue
            break
        return from_

    def parse_table_ref(self) -> dict:
        if self.at("("):
            self.take()
            q = self.parse_query()
            self.expect(")")
            alias = self.parse_optional_alias()
            self.rules.add("from.derived_table")
            return {"derived": q, "alias": alias}
        t = self.take()
        if t.kind not in {"WORD", "IDENT"}:
            raise Unsupported(f"bad table ref {t.text}")
        name = t.text
        alias = self.parse_optional_alias()
        self.rules.add("from.scan")
        return {"table": name, "alias": alias}

    def parse_optional_alias(self) -> str | None:
        if self.at("AS"):
            self.take()
            return self.take().text
        t = self.peek()
        if t is not None and t.kind in {"WORD", "IDENT"} and up(t) not in CLAUSE_STOP and up(t) not in JOIN_WORDS:
            return self.take().text
        return None

    def parse_expr_list(self) -> list[dict]:
        out = [self.parse_expr(0)]
        while self.at(","):
            self.take()
            out.append(self.parse_expr(0))
        return out

    def parse_sort(self) -> list[dict]:
        out = []
        while True:
            e = self.parse_expr(0)
            desc = False
            if self.at("DESC"):
                self.take()
                desc = True
            elif self.at("ASC"):
                self.take()
            out.append({"expr": e, "desc": desc})
            if self.at(","):
                self.take()
                continue
            break
        return out

    # ---- expression parser (precedence climbing)
    def parse_expr(self, min_prec: int) -> dict:
        left = self.parse_unary()
        while True:
            t = self.peek()
            if t is None:
                break
            key = t.text.upper() if t.kind == "WORD" else t.text
            if t.kind == "WORD":
                if key == "OR":
                    prec = 1
                elif key == "AND":
                    prec = 2
                elif key in {"IS", "IN", "LIKE", "BETWEEN"}:
                    prec = 4
                elif key == "NOT" and up(self.peek(1)) in {"IN", "LIKE", "BETWEEN"}:
                    prec = 4
                else:
                    break
            elif t.kind == "OP" and t.text in {"=", "==", "!=", "<>", "<", "<=", ">", ">="}:
                prec = 4
            elif t.kind == "OP" and t.text in {"+", "-", "||"}:
                prec = 5
            elif t.kind == "OP" and t.text in {"*", "/", "%"}:
                prec = 6
            else:
                break
            if prec < min_prec:
                break
            if key == "OR":
                self.take()
                rhs = self.parse_expr(prec + 1)
                left = self.mk_bool("or", left, rhs)
                self.rules.add("filter.or")
            elif key == "AND":
                self.take()
                rhs = self.parse_expr(prec + 1)
                left = self.mk_bool("and", left, rhs)
                self.rules.add("filter.and")
            else:
                left = self.parse_predicate(left, key, prec)
        return left

    @staticmethod
    def flatten(node: dict, kind: str) -> list[dict]:
        if node["k"] == kind:
            return list(node["args"])
        return [node]

    def mk_bool(self, kind: str, a: dict, b: dict) -> dict:
        args = self.flatten(a, kind) + self.flatten(b, kind)
        return {"k": kind, "args": args}

    def parse_predicate(self, left: dict, key: str, prec: int = 4) -> dict:
        not_ = False
        if key == "NOT":
            self.take()
            self.rules.add("pred.negated")
            not_ = True
            key = up(self.peek())
        if key == "IS":
            self.take()
            if self.at("NOT"):
                self.take()
                not_ = True
            self.expect("NULL")
            self.rules.add("pred.is_null")
            return {"k": "isnull", "not": not_, "e": left}
        if key == "BETWEEN":
            self.take()
            lo = self.parse_expr(5)
            self.expect("AND")
            hi = self.parse_expr(5)
            self.rules.add("pred.between")
            return {"k": "between", "not": not_, "e": left, "lo": lo, "hi": hi}
        if key == "LIKE":
            self.take()
            rhs = self.parse_expr(5)
            self.rules.add("pred.like")
            return {"k": "like", "not": not_, "l": left, "r": rhs}
        if key == "IN":
            self.take()
            self.expect("(")
            if self.at("SELECT"):
                q = self.parse_query()
                self.expect(")")
                self.rules.add("sub.in")
                return {"k": "in", "not": not_, "l": left, "items": None, "query": q}
            items = self.parse_expr_list()
            self.expect(")")
            self.rules.add("pred.in_list")
            return {"k": "in", "not": not_, "l": left, "items": items, "query": None}
        op = self.take().text
        rhs = self.parse_expr(prec + 1)
        if op in {"+", "-", "||", "*", "/", "%"}:
            return {"k": "bin", "op": op, "l": left, "r": rhs}
        self.rules.add("pred.compare")
        return {"k": "cmp", "op": op, "l": left, "r": rhs}

    def parse_unary(self) -> dict:
        t = self.peek()
        if t is not None and t.kind == "OP" and t.text in {"-", "+"}:
            self.take()
            e = self.parse_unary()
            if t.text == "+":
                return e
            if e["k"] == "lit" and not e["v"].startswith("-"):
                return {"k": "lit", "v": "-" + e["v"]}
            return {"k": "neg", "e": e}
        if t is not None and t.kind == "WORD" and t.text.upper() == "NOT":
            if up(self.peek(1)) == "EXISTS":
                self.take()
                self.take()
                self.expect("(")
                if not self.at("SELECT"):
                    raise Unsupported("NOT EXISTS without subquery")
                q = self.parse_query()
                self.expect(")")
                self.rules.add("sub.exists")
                return {"k": "exists", "not": True, "query": q}
            self.take()
            self.rules.add("filter.not")
            return {"k": "not", "e": self.parse_unary()}
        return self.parse_primary()

    def parse_primary(self) -> dict:
        t = self.peek()
        if t is None:
            raise Unsupported("EOF in expression")
        if t.kind == "PUNCT" and t.text == "(":
            self.take()
            if self.at("SELECT"):
                q = self.parse_query()
                self.expect(")")
                self.rules.add("sub.scalar")
                return {"k": "sub", "query": q}
            e = self.parse_expr(0)
            self.expect(")")
            return e
        if t.kind == "NUM":
            self.take()
            return {"k": "lit", "v": t.text}
        if t.kind == "STR":
            self.take()
            return {"k": "lit", "v": t.text}
        if t.kind == "IDENT":
            self.take()
            return {"k": "col", "name": t.text}
        if t.kind == "WORD":
            word = t.text.upper()
            if word in {"NULL", "TRUE", "FALSE"}:
                self.take()
                return {"k": "lit", "v": word}
            if word == "CASE":
                return self.parse_case()
            if word == "EXISTS":
                self.take()
                self.expect("(")
                if not self.at("SELECT"):
                    raise Unsupported("EXISTS without subquery")
                q = self.parse_query()
                self.expect(")")
                self.rules.add("sub.exists")
                return {"k": "exists", "not": False, "query": q}
            if word == "CAST":
                self.take()
                self.expect("(")
                e = self.parse_expr(0)
                self.expect("AS")
                typ = []
                while self.peek() is not None and not self.at(")"):
                    typ.append(self.take().text)
                self.expect(")")
                self.rules.add("expr.cast")
                return {"k": "cast", "e": e, "type": " ".join(typ)}
            nxt = self.peek(1)
            if nxt is not None and nxt.text == "(":
                return self.parse_function()
            # bare identifier / qualified name
            self.take()
            name = t.text
            while self.at("."):
                self.take()
                nxt = self.take()
                name += "." + nxt.text
            if name.endswith(".*"):
                return {"k": "star"}
            return {"k": "col", "name": name}
        raise Unsupported(f"unexpected token {t.text}")

    def parse_case(self) -> dict:
        self.expect("CASE")
        base = None
        if not self.at("WHEN"):
            base = self.parse_expr(0)
        branches = []
        while self.at("WHEN"):
            self.take()
            cond = self.parse_expr(0)
            self.expect("THEN")
            val = self.parse_expr(0)
            branches.append([cond, val])
        els = None
        if self.at("ELSE"):
            self.take()
            els = self.parse_expr(0)
        self.expect("END")
        self.rules.add("expr.case")
        return {"k": "case", "base": base, "branches": branches, "else": els}

    def parse_function(self) -> dict:
        name = self.take().text
        upper = name.upper()
        self.expect("(")
        distinct = False
        if self.at("DISTINCT"):
            self.take()
            distinct = True
        args: list[dict] = []
        if self.at("*"):
            self.take()
            args = [{"k": "star"}]
        elif not self.at(")"):
            args = self.parse_expr_list()
        self.expect(")")
        if upper in AGG_FUNCS:
            if len(args) != 1:
                raise Unsupported(f"{upper} with {len(args)} args")
            self.rules.add("expr.agg")
            if distinct:
                self.rules.add("agg.distinct")
            return {"k": "agg", "fn": upper, "arg": args[0], "distinct": distinct}
        if distinct:
            raise Unsupported("DISTINCT in scalar function")
        self.rules.add("expr.func")
        return {"k": "fn", "name": name, "args": args}

    # ---- derived metadata (not used by lowering, used by the agent/validator)
    def cardinality(self, q: dict) -> str:
        has_agg = any(it["role"] == "aggregate" for it in q["project"])
        if q["distinct"]:
            return "SET"
        if q["group"]:
            return "TOP_K" if q["limit"] is not None else "GROUPED"
        if has_agg:
            return "SINGLE"
        if q["limit"] is not None:
            return "TOP_K"
        return "LIST"


# --------------------------------------------------------------------------- #
# lowering: IR -> SQL
# --------------------------------------------------------------------------- #
PREC = {"or": 1, "and": 2, "not": 3, "cmp": 4, "like": 4, "in": 4, "between": 4,
        "isnull": 4, "exists": 4, "bin_add": 5, "bin_mul": 6, "neg": 7}


def prec_of(e: dict) -> int:
    k = e["k"]
    if k in {"and", "or", "not", "cmp", "like", "in", "between", "isnull", "exists"}:
        return PREC[k]
    if k == "bin":
        return PREC["bin_mul"] if e["op"] in {"*", "/", "%"} else PREC["bin_add"]
    if k == "neg":
        return PREC["neg"]
    return 9


def wrap(e: dict, parent: int) -> str:
    s = lower_expr(e)
    return f"({s})" if prec_of(e) < parent else s


def lower_expr(e: dict) -> str:
    k = e["k"]
    if k == "col":
        return e["name"]
    if k == "lit":
        return e["v"]
    if k == "star":
        return "*"
    if k == "agg":
        arg = "*" if e["arg"]["k"] == "star" else lower_expr(e["arg"])
        d = "DISTINCT " if e["distinct"] else ""
        return f"{e['fn']}({d}{arg})"
    if k == "fn":
        return f"{e['name']}({', '.join(lower_expr(a) for a in e['args'])})"
    if k == "cast":
        return f"CAST({lower_expr(e['e'])} AS {e['type']})"
    if k == "bin":
        p = prec_of(e)
        return f"{wrap(e['l'], p)} {e['op']} {wrap(e['r'], p)}"
    if k == "neg":
        return f"-{wrap(e['e'], PREC['neg'])}"
    if k == "cmp":
        return f"{wrap(e['l'], 4)} {e['op']} {wrap(e['r'], 5)}"
    if k == "and":
        return " AND ".join(wrap(a, 3) for a in e["args"])
    if k == "or":
        return " OR ".join(wrap(a, 2) for a in e["args"])
    if k == "not":
        return f"NOT ({lower_expr(e['e'])})"
    if k == "like":
        return f"{wrap(e['l'], 4)} {'NOT ' if e['not'] else ''}LIKE {wrap(e['r'], 5)}"
    if k == "in":
        if e["query"] is not None:
            inner = lower_query(e["query"])
            return f"{wrap(e['l'], 4)} {'NOT ' if e['not'] else ''}IN ({inner})"
        items = ", ".join(lower_expr(i) for i in e["items"])
        return f"{wrap(e['l'], 4)} {'NOT ' if e['not'] else ''}IN ({items})"
    if k == "between":
        return (f"{wrap(e['e'], 4)} {'NOT ' if e['not'] else ''}BETWEEN "
                f"{wrap(e['lo'], 5)} AND {wrap(e['hi'], 5)}")
    if k == "isnull":
        return f"{wrap(e['e'], 4)} IS {'NOT ' if e['not'] else ''}NULL"
    if k == "exists":
        return f"{'NOT ' if e['not'] else ''}EXISTS ({lower_query(e['query'])})"
    if k == "case":
        parts = ["CASE"]
        if e["base"] is not None:
            parts.append(lower_expr(e["base"]))
        for cond, val in e["branches"]:
            parts.append(f"WHEN {lower_expr(cond)} THEN {lower_expr(val)}")
        if e["else"] is not None:
            parts.append(f"ELSE {lower_expr(e['else'])}")
        parts.append("END")
        return " ".join(parts)
    if k == "sub":
        return f"({lower_query(e['query'])})"
    raise Unsupported(f"lower: unknown node {k}")


def lower_table(ref: dict) -> str:
    if "derived" in ref:
        s = f"({lower_query(ref['derived'])})"
        return s + (f" AS {ref['alias']}" if ref.get("alias") else "")
    s = ref["table"]
    return s + (f" AS {ref['alias']}" if ref.get("alias") else "")


def lower_query(q: dict) -> str:
    parts = ["SELECT "]
    parts.append("DISTINCT " if q["distinct"] else "")
    items = []
    for it in q["project"]:
        s = lower_expr(it["expr"])
        if it.get("alias"):
            s += f" AS {it['alias']}"
        items.append(s)
    parts.append(", ".join(items))
    if q["from"]["scans"]:
        from_s = lower_table(q["from"]["scans"][0])
        for extra in q["from"]["scans"][1:]:
            from_s += ", " + lower_table(extra)
        for j in q["from"]["joins"]:
            from_s += f" {j['kind']} JOIN {lower_table(j['ref'])}"
            if j["on"] is not None:
                from_s += f" ON {lower_expr(j['on'])}"
        parts.append(" FROM " + from_s)
    if q["filter"] is not None:
        parts.append(" WHERE " + lower_expr(q["filter"]))
    if q["group"]:
        parts.append(" GROUP BY " + ", ".join(lower_expr(g) for g in q["group"]))
    if q["having"] is not None:
        parts.append(" HAVING " + lower_expr(q["having"]))
    if q["sort"]:
        parts.append(" ORDER BY " + ", ".join(
            lower_expr(s["expr"]) + (" DESC" if s["desc"] else "") for s in q["sort"]))
    if q["limit"] is not None:
        parts.append(f" LIMIT {q['limit']}")
        if q["offset"]:
            parts.append(f" OFFSET {q['offset']}")
    elif q["offset"]:
        parts.append(f" LIMIT -1 OFFSET {q['offset']}")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# round-trip execution check
# --------------------------------------------------------------------------- #
def norm_cell(v):
    if isinstance(v, float):
        if v == int(v):
            return int(v)
        return round(v, 6)
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def run(db_path: Path, sql: str, timeout: float = 8.0):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    start = time.monotonic()

    def guard() -> int:
        return 1 if time.monotonic() - start > timeout else 0

    con.set_progress_handler(guard, 20000)
    try:
        cur = con.execute(sql)
        rows = [tuple(norm_cell(c) for c in r) for r in cur.fetchall()]
        return rows
    finally:
        con.set_progress_handler(None, 0)
        con.close()


def compare(a: list[tuple], b: list[tuple], ordered: bool) -> bool:
    if ordered:
        return a == b
    return Counter(a) == Counter(b)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--parse-only", action="store_true",
                    help="skip sqlite execution (fast coverage check on train)")
    ap.add_argument("--out-dir", default=str(ROOT / "outputs" / "ir_roundtrip"))
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.split == "dev":
        data = json.loads((ROOT / "BIRD/dev_20240627/dev.json").read_text(encoding="utf-8"))
        dbs = ROOT / "BIRD/dev_20240627/dev_databases"
    else:
        data = json.loads((ROOT / "BIRD/train/train.json").read_text(encoding="utf-8"))
        dbs = ROOT / "BIRD/train/train_databases"
    if args.limit:
        data = data[: args.limit]

    rule_total: Counter = Counter()
    rule_fail: Counter = Counter()
    stats = Counter()
    unsupported: Counter = Counter()
    mismatches: list[dict] = []

    with (out_dir / "roundtrip.jsonl").open("w", encoding="utf-8") as fh:
        for row in data:
            sql = row["SQL"]
            db_path = dbs / row["db_id"] / f"{row['db_id']}.sqlite"
            rec = {"db": row["db_id"], "question": row["question"], "gold": sql}
            try:
                p = Parser(sql)
                ir = p.parse()
            except Unsupported as exc:
                stats["parse_fail"] += 1
                unsupported[str(exc).split(" got ")[0]] += 1
                rec.update(status="parse_fail", reason=str(exc))
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                continue
            except Exception as exc:  # noqa: BLE001
                stats["parse_error"] += 1
                unsupported[f"error:{type(exc).__name__}"] += 1
                rec.update(status="parse_error", reason=str(exc)[:160])
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                continue
            stats["parsed"] += 1
            rule_total.update(p.rules)
            try:
                sql2 = lower_query(ir)
            except Exception as exc:  # noqa: BLE001
                stats["lower_fail"] += 1
                rule_fail.update(p.rules)
                rec.update(status="lower_fail", reason=str(exc)[:160], ir=ir)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                continue
            rec["lowered"] = sql2
            rec["ir"] = ir
            if args.parse_only:
                stats["checked_no_exec"] += 1
                rec["status"] = "parsed"
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                continue
            try:
                rows_gold = run(db_path, sql)
            except Exception as exc:  # noqa: BLE001
                stats["gold_exec_error"] += 1
                rec.update(status="gold_exec_error", reason=str(exc)[:160])
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                continue
            try:
                rows_new = run(db_path, sql2)
            except Exception as exc:  # noqa: BLE001
                stats["lower_exec_error"] += 1
                rule_fail.update(p.rules)
                rec.update(status="lower_exec_error", reason=str(exc)[:160])
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                continue
            ordered = bool(ir["sort"]) and ir["limit"] is not None
            if compare(rows_gold, rows_new, ordered):
                stats["match"] += 1
                rec.update(status="match", nrows=len(rows_gold))
            else:
                stats["result_mismatch"] += 1
                rule_fail.update(p.rules)
                rec.update(status="result_mismatch", n_gold=len(rows_gold), n_new=len(rows_new),
                           rows_gold=rows_gold[:3], rows_new=rows_new[:3])
                if len(mismatches) < 12:
                    mismatches.append(rec)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    total = len(data)
    report = {
        "split": args.split, "total": total, "stats": dict(stats),
        "unsupported": dict(unsupported.most_common(20)),
        "rules": {k: [rule_total[k], rule_fail[k]] for k in sorted(rule_total)},
    }
    (out_dir / "summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    md = [f"# 反编译 / lowering round-trip 结果（{args.split}, n={total}）\n"]
    md.append("| 结果 | 条数 | 占比 |\n|---|---:|---:|")
    for k in ["match", "result_mismatch", "parse_fail", "parse_error", "lower_fail",
              "lower_exec_error", "gold_exec_error"]:
        v = stats.get(k, 0)
        md.append(f"| {k} | {v} | {100*v/total:.1f}% |")
    md.append("\n## 各规则触发/失败次数\n")
    md.append("| rule | 触发 | 失败 |\n|---|---:|---:|")
    for k in sorted(rule_total):
        md.append(f"| `{k}` | {rule_total[k]} | {rule_fail[k]} |")
    md.append("\n## 未支持的构造\n")
    for k, v in unsupported.most_common(20):
        md.append(f"- {k}: {v}")
    if mismatches:
        md.append("\n## 结果不一致样例\n")
        for m in mismatches[:8]:
            md.append(f"- db={m['db']}\n  - gold: `{m['gold']}`\n  - new : `{m.get('lowered','')}`")
    (out_dir / "report.md").write_text("\n".join(md), encoding="utf-8")

    print(json.dumps(report["stats"], ensure_ascii=False))
    print("parse coverage: %.1f%%" % (100 * stats["parsed"] / total))
    if not args.parse_only:
        print("round-trip exact: %.1f%% of parsed (%.1f%% of all)" % (
            100 * stats["match"] / max(1, stats["parsed"]), 100 * stats["match"] / total))
    print("unsupported:", report["unsupported"])
    print("written:", out_dir)


if __name__ == "__main__":
    main()
