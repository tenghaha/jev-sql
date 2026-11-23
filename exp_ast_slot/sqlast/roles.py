"""Role slots: keep the AST skeleton, replace schema and values by typed holes.

Idea
----
The AST's *branch structure* is the part that transfers across databases; the
table names, column names and literal values are the part that does not.  So we
walk the AST and replace each of those leaves by a ``Slot`` carrying a **role**
(what the hole is for) and a **kind** (what must be filled in), and keep the
rest of the tree untouched.  The result is a *template* that mentions no table,
no column and no value, plus a side table that records what the gold SQL had
there (the silver label).

Roles (assigned by clause, ``kind="table"`` for relations)
---------------------------------------------------------
===========  ==========================================================
 role         where it comes from
===========  ==========================================================
 ``TABLE``    every table reference in ``FROM`` / ``JOIN``
 ``JOINKEY``  ``JOIN ... ON`` / ``USING``; also the key compared through
              ``IN (SELECT ...)`` / ``= (SELECT ...)`` / ``EXISTS``
 ``MEASURE``  every column of the ``SELECT`` list, aggregated or not, plus
              every column inside an aggregate anywhere
 ``DIMENSION````GROUP BY`` key (and ``PARTITION BY`` of a window)
 ``FILTER``   ``WHERE`` / ``HAVING`` / ``QUALIFY`` condition that is not
              inside an aggregate
 ``SORT``     ``ORDER BY`` key (and the ``ORDER BY`` inside a window)
===========  ==========================================================

Kinds (what the hole is filled with)
------------------------------------
``table``   a table name;        ``column``  a column name
``value``   a literal value;     ``field``   a struct/JSON field accessor

Value slots reuse the role of the clause they sit in (a literal compared in
``WHERE`` is a ``FILTER`` hole, a literal in ``SELECT`` a ``MEASURE`` hole), so
``kind`` is what tells a column hole from a value hole.

Three refinements keep the roles semantically honest without adding structure:

* **aggregate wins** — a column inside an aggregate is ``MEASURE`` wherever the
  aggregate sits (``HAVING MAX(x) > 3``, ``ORDER BY AVG(x)``);
* **alias is not a column** — ``ORDER BY n`` where ``n`` is a projection alias
  stays an ``AliasRef`` node instead of becoming a slot: it refers to a computed
  projection, and asking a model to bind it to a table column would be wrong;
* **NULL/TRUE/FALSE stay literals** — they are keywords, not data values.

Usage
-----
    from sqlast.parser import parse_sql
    from sqlast.roles import annotate_roles, ROLE_SPEC, role_stats

    ast, _ = parse_sql(sql, "sqlite")
    annotated = annotate_roles(ast, schema=schema, db_id="toxicology")
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, replace

__all__ = ["ROLES", "KINDS", "ROLE_SPEC", "Annotated", "annotate_roles",
           "role_stats", "signature", "role_sequence"]

ROLES = ("TABLE", "JOINKEY", "MEASURE", "DIMENSION", "FILTER", "SORT")
KINDS = ("table", "column", "value", "field")

ROLE_SPEC = [
    {"role": "TABLE", "kind": "table",
     "clause": "every table reference in FROM / JOIN",
     "meaning": "which relation the branch reads from",
     "check": "the filled name exists in the target schema"},
    {"role": "JOINKEY", "kind": "column",
     "clause": "JOIN ... ON / USING, or a key compared through IN/EXISTS",
     "meaning": "a column that connects two relations (FK/PK, or a key both sides compare on)",
     "check": "resolves to a PK/FK column in the schema; equality between two relations"},
    {"role": "MEASURE", "kind": "column|value",
     "clause": "SELECT list (aggregated or not), or inside an aggregate",
     "meaning": "a column/value the answer reports: the measured quantity, or an output attribute",
     "check": "SUM/AVG need a numeric column; COUNT and bare projections accept any column"},
    {"role": "DIMENSION", "kind": "column|value",
     "clause": "GROUP BY key (and window PARTITION BY)",
     "meaning": "the grain the answer is grouped by",
     "check": "appears in GROUP BY / PARTITION BY"},
    {"role": "FILTER", "kind": "column|value",
     "clause": "WHERE / HAVING / QUALIFY predicate (outside aggregates)",
     "meaning": "the column the question constrains, and the value it is compared with",
     "check": "type-compatible pair; the value must exist in (or be derivable from) the column's domain"},
    {"role": "SORT", "kind": "column|value",
     "clause": "ORDER BY key (also the ORDER BY inside a window)",
     "meaning": "the ranking key; may be hidden (not projected)",
     "check": "sortable type; visible or hidden relative to the projection"},
]

#: clause -> role.  ``MEASURE`` also wins inside aggregates (see ``_slot_column``).
_CLAUSE_ROLE = {
    "join": "JOINKEY",
    "where": "FILTER",
    "having": "FILTER",
    "qualify": "FILTER",
    "group": "DIMENSION",
    "select": "MEASURE",
    "order": "SORT",
    "window_partition": "DIMENSION",
    "window_order": "SORT",
    "limit": "FILTER",
}

_VALUE_KINDS = ("string", "number")


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Ctx:
    clause: str = "select"
    in_agg: bool = False
    key_mode: bool = False          # projection of a subquery used as a key
    agg: str | None = None          # the aggregate a MEASURE feeds
    scope: tuple = ()               # ((alias_lower, table), ...)
    aliases: frozenset = frozenset()  # projection aliases of the enclosing SELECT

    def at(self, clause: str) -> "_Ctx":
        return replace(self, clause=clause)

    def within_agg(self, name: str | None) -> "_Ctx":
        return replace(self, in_agg=True, agg=name)

    def role(self) -> str:
        if self.in_agg:
            return "MEASURE"
        if self.key_mode:
            return "JOINKEY"
        return _CLAUSE_ROLE.get(self.clause, "FILTER")


@dataclass
class Annotated:
    template: dict
    slots: list[dict]


# ---------------------------------------------------------------------------
# annotator
# ---------------------------------------------------------------------------
class _RoleAnnotator:
    def __init__(self, schema=None, db=None, slot_tables: bool = True,
                 slot_values: bool = True):
        self.schema = schema
        self.db = db
        self.slot_tables = slot_tables
        self.slot_values = slot_values
        self.slots: list[dict] = []
        self._scopes: dict[int, tuple] = {}

    # -- bookkeeping ---------------------------------------------------------
    def _sid(self) -> int:
        return len(self.slots) + 1

    def _emit(self, binding: dict) -> dict:
        self.slots.append(binding)
        return {"type": "Slot", "role": binding["role"], "kind": binding["kind"],
                "sid": binding["sid"]}

    # -- entry --------------------------------------------------------------
    def script(self, ast: dict) -> dict:
        """Mutate a *copy* of ``ast`` in place; return the template."""
        for stmt in ast.get("stmts") or []:
            self.statement(stmt, _Ctx(), "stmts[]")
        return ast

    def statement(self, node: dict, ctx: _Ctx, path: str) -> None:
        t = node.get("type")
        if t == "Query":
            self.query(node, ctx, path, key_mode=False)
        elif t == "Declare" and node.get("default"):
            self.expr(node["default"], ctx.at("where"), path + ".default")
        elif t == "CreateFunction" and node.get("body"):
            self.expr(node["body"], ctx.at("where"), path + ".body")
        elif t == "SetVar" and node.get("value") is not None:
            self.expr(node["value"], ctx.at("where"), path + ".value")

    # -- queries ------------------------------------------------------------
    def query(self, q: dict, ctx: _Ctx, path: str, key_mode: bool) -> None:
        for i, cte in enumerate(q.get("with") or []):
            self.query(cte["body"], ctx, f"{path}.with[{i}]", key_mode=False)
        body = q.get("body")
        if body:
            self.core(body, ctx, f"{path}.body", key_mode)
        # ORDER BY is evaluated in the scope of the SELECT it attaches to
        owner = _body_select(body)
        order_ctx = ctx
        if owner is not None:
            order_ctx = replace(ctx, scope=self._scopes.get(id(owner), ()),
                                aliases=_aliases_of(owner))
        for i, item in enumerate(q.get("order_by") or []):
            self.expr(item, order_ctx.at("order"), f"{path}.order_by[{i}]")
        for key in ("limit", "offset"):
            if q.get(key):
                self.expr(q[key], ctx.at("limit"), f"{path}.{key}")

    def core(self, node: dict, ctx: _Ctx, path: str, key_mode: bool) -> None:
        t = node.get("type")
        if t == "Select":
            self.select(node, ctx, path, key_mode)
        elif t == "SetOp":
            self.core(node["l"], ctx, path + ".l", key_mode)
            self.core(node["r"], ctx, path + ".r", key_mode)
        elif t == "ParenQuery":
            self.query(node["query"], ctx, path, key_mode)
        elif t == "Values":
            for i, row in enumerate(node.get("rows") or []):
                for j, e in enumerate(row):
                    self.expr(e, ctx.at("select"), f"{path}.rows[{i}][{j}]")

    def select(self, sel: dict, ctx: _Ctx, path: str, key_mode: bool) -> None:
        local = replace(ctx, scope=self._scopes.get(id(sel), ()),
                        aliases=_aliases_of(sel), in_agg=False, key_mode=key_mode)
        distinct = sel.get("distinct") or {}
        for i, e in enumerate(distinct.get("exprs") or []):
            # DISTINCT ON (...) keys behave like grouping keys
            self.expr(e, local.at("group"), f"{path}.distinct.exprs[{i}]")
        for i, item in enumerate(sel.get("items") or []):
            if "star" in item:
                self.star(item["star"], local, f"{path}.items[{i}].star")
                continue
            if item.get("expr") is not None:
                self.expr(item["expr"], local.at("select"), f"{path}.items[{i}].expr")
        for i, item in enumerate(sel.get("from") or []):
            self.from_item(item, local, f"{path}.from[{i}]")
        if sel.get("where"):
            self.expr(sel["where"], local.at("where"), path + ".where")
        for i, g in enumerate(sel.get("group_by") or []):
            self.expr(g, local.at("group"), f"{path}.group_by[{i}]")
        if sel.get("having"):
            self.expr(sel["having"], local.at("having"), path + ".having")
        if sel.get("qualify"):
            self.expr(sel["qualify"], local.at("qualify"), path + ".qualify")
        for i, w in enumerate(sel.get("window") or []):
            self.window(w.get("spec") or {}, local, f"{path}.window[{i}]")

    def star(self, star: dict, ctx: _Ctx, path: str) -> None:
        """``* EXCEPT(a, b)`` / ``* REPLACE(expr AS c)`` name columns too."""
        for key, role in (("except", "DIMENSION"), ("exclude", "DIMENSION"),
                          ("replace", "MEASURE")):
            values = star.get(key)
            if not values:
                continue
            for j, entry in enumerate(values):
                if isinstance(entry, str):
                    binding = self._column_binding(entry, None, ctx,
                                                   f"{path}.{key}[{j}]", role)
                    if binding:
                        values[j] = self._emit(binding)
                elif isinstance(entry, dict):
                    if entry.get("expr") is not None:
                        self.expr(entry["expr"], ctx.at("select"),
                                  f"{path}.{key}[{j}].expr")
                    name = entry.get("name")
                    if isinstance(name, str):
                        binding = self._column_binding(name, None, ctx,
                                                       f"{path}.{key}[{j}].name", role)
                        if binding:
                            entry["name"] = self._emit(binding)

    def window(self, spec: dict, ctx: _Ctx, path: str) -> None:
        for i, e in enumerate(spec.get("partition_by") or []):
            self.expr(e, ctx.at("window_partition"), f"{path}.partition_by[{i}]")
        for i, item in enumerate(spec.get("order_by") or []):
            self.expr(item, ctx.at("window_order"), f"{path}.order_by[{i}]")
        frame = spec.get("frame") or {}
        for key in ("start", "end"):
            bound = frame.get(key)
            if isinstance(bound, dict) and isinstance(bound.get("bound"), dict):
                self.expr(bound["bound"], ctx.at("window_order"),
                          f"{path}.frame.{key}")

    def from_item(self, item: dict, ctx: _Ctx, path: str) -> None:
        t = item.get("type")
        if t == "Join":
            self.from_item(item["left"], ctx, path + ".left")
            self.from_item(item["right"], ctx, path + ".right")
            if item.get("on"):
                self.expr(item["on"], ctx.at("join"), path + ".on")
            using = item.get("using")
            if using:
                for j, name in enumerate(using):
                    binding = self._column_binding(name, None, ctx.at("join"),
                                                   f"{path}.using[{j}]", "JOINKEY")
                    if binding:
                        binding["via"] = "USING"
                        using[j] = self._emit(binding)
        elif t == "Table":
            if self.slot_tables:
                self._table_slot(item, path)
        elif t == "DerivedTable":
            self.query(item["query"], ctx, path + ".query", key_mode=False)
        elif t == "Unnest":
            self.expr(item["expr"], ctx.at("select"), path + ".expr")
        elif t == "TableFunction":
            if item.get("expr") is not None:
                self.expr(item["expr"], ctx.at("select"), path + ".expr")
            func = item.get("func") or {}
            for i, a in enumerate(func.get("args") or []):
                self.expr(a, ctx.at("select"), f"{path}.func.args[{i}]")

    # -- expressions --------------------------------------------------------
    def expr(self, node, ctx: _Ctx, path: str) -> None:
        if not isinstance(node, dict) or "type" not in node:
            return
        t = node["type"]

        if t == "Column":
            self._column_slot(node, ctx, path)
            return

        if t == "Literal":
            if self.slot_values and node.get("kind") in _VALUE_KINDS:
                self._value_slot(node, ctx, path)
            return

        if t == "TypedLiteral":
            if self.slot_values:
                self._typed_value_slot(node, ctx, path)
            return

        if t in ("FieldAccess", "JsonPath"):
            self.expr(node.get("e"), ctx, path + ".e")
            key = "field" if t == "FieldAccess" else "path"
            if isinstance(node.get(key), str):
                self._field_slot(node, key, "field" if t == "FieldAccess" else "json",
                                 ctx, path)
            return

        if t == "Func":
            inner = (ctx.within_agg(node.get("name"))
                     if node.get("is_aggregate") else ctx)
            for i, a in enumerate(node.get("args") or []):
                self.expr(a, inner, f"{path}.args[{i}]")
            if node.get("filter"):
                self.expr(node["filter"], ctx.at("where"), path + ".filter")
            if node.get("over"):
                self.window(node["over"], ctx, path + ".over")
            return

        if t == "Subquery":
            self.query(node["query"], ctx, path + ".query", key_mode=False)
            return

        if t == "In" and node.get("query"):
            self.expr(node.get("e"), ctx, path + ".e")
            self.query(node["query"], ctx, path + ".query", key_mode=True)
            return

        if t == "Exists":
            if node.get("query"):
                self.query(node["query"], ctx, path + ".query", key_mode=True)
            return

        if t == "BinaryOp":
            right = node.get("r")
            if isinstance(right, dict) and right.get("type") == "Subquery":
                self.expr(node.get("l"), ctx, path + ".l")
                self.query(right["query"], ctx, path + ".r.query", key_mode=True)
                return

        for child, sub in _children_with_paths(node):
            self.expr(child, ctx, f"{path}.{sub}")

    # -- the replacements ---------------------------------------------------
    def _table_slot(self, item: dict, path: str) -> None:
        name = item.get("name") or {}
        parts = name.get("parts") or []
        if not parts:
            return
        item["name"] = self._emit({
            "sid": self._sid(), "role": "TABLE", "kind": "table",
            "path": path + ".name", "table": parts[-1], "table_parts": parts,
        })

    def _column_slot(self, node: dict, ctx: _Ctx, path: str) -> None:
        parts = node.get("parts") or []
        if not parts:
            return
        column = parts[-1]
        qualifier = ".".join(parts[:-1]) or None
        if qualifier is None and column.lower() in ctx.aliases:
            node.clear()
            node.update({"type": "AliasRef", "name": column})
            return
        binding = self._column_binding(column, qualifier, ctx, path, ctx.role(),
                                       parts[-2] if len(parts) > 1 else None)
        if binding:
            node.clear()
            node.update(self._emit(binding))

    def _column_binding(self, column: str, qualifier, ctx: _Ctx, path: str,
                        role: str, lookup=None) -> dict | None:
        """Build one column binding; ``lookup`` is the part that names the table."""
        if not column:
            return None
        table, source = self._resolve(lookup if lookup is not None else qualifier,
                                      column, ctx.scope)
        binding = {
            "sid": self._sid(), "role": role, "kind": "column",
            "path": path, "column": column, "qualifier": qualifier,
            "table": table, "binding": source,
        }
        if role == "MEASURE" and ctx.agg:
            binding["agg"] = ctx.agg
        datatype = self._type(table, column)
        if datatype != "unknown":
            binding["data_type"] = datatype
            if self.db:
                key = self._key_role(table, column)
                if key != "none":
                    binding["key_role"] = key
        return binding

    def _value_slot(self, node: dict, ctx: _Ctx, path: str) -> None:
        value = node.get("value")
        binding = {
            "sid": self._sid(), "role": ctx.role(), "kind": "value",
            "path": path, "value": value, "value_class": node.get("kind"),
        }
        text = "" if value is None else str(value)
        if node.get("kind") == "string":
            binding["format_token"] = "true" if "%" in text else "false"
        node.clear()
        node.update(self._emit(binding))

    def _typed_value_slot(self, node: dict, ctx: _Ctx, path: str) -> None:
        binding = {
            "sid": self._sid(), "role": ctx.role(), "kind": "value",
            "path": path, "value": node.get("value"), "value_class": "date_string",
            "typed": node.get("data_type"),
        }
        node.clear()
        node.update(self._emit(binding))

    def _field_slot(self, node: dict, key: str, accessor: str, ctx: _Ctx,
                    path: str) -> None:
        node[key] = self._emit({
            "sid": self._sid(), "role": ctx.role(), "kind": "field",
            "path": f"{path}.{key}", "field": node[key], "accessor": accessor,
        })

    # -- scope / schema lookups --------------------------------------------
    def precompute_scopes(self, node) -> None:
        """Cache each SELECT's scope before table names are replaced by slots."""
        if isinstance(node, dict):
            if node.get("type") == "Select":
                self._scopes[id(node)] = _scope_of(node)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    self.precompute_scopes(v)
        elif isinstance(node, list):
            for v in node:
                self.precompute_scopes(v)

    def _resolve(self, qualifier, column, scope) -> tuple[str | None, str]:
        table_of = dict(scope)
        if qualifier:
            table = table_of.get(qualifier.lower())
            if table:
                return table, "explicit"
            if self.db and qualifier.lower() in self.db.tables_present():
                return qualifier, "explicit"
            return qualifier, "unresolved"
        if self.db and table_of:
            hits = [t for t in table_of.values() if self.db.column_type(t, column)]
            if len(hits) == 1:
                return hits[0], "inferred"
            if len(hits) > 1:
                return None, "ambiguous"
        return None, "unresolved"

    def _type(self, table: str | None, column: str) -> str:
        if not self.db:
            return "unknown"
        t = self.db.column_type(table, column)
        if t is None:
            return "unknown"
        t = t.lower()
        if "int" in t:
            return "integer"
        if t in ("real", "double", "float", "numeric", "decimal", "number"):
            return "real"
        if t in ("date", "datetime", "timestamp", "time"):
            return "date"
        return "text"

    def _key_role(self, table: str | None, column: str) -> str:
        if not self.db or not table:
            return "none"
        pk = bool(self.db.is_primary_key(table, column))
        fk = bool(self.db.is_foreign_key(table, column))
        return "both" if pk and fk else "pk" if pk else "fk" if fk else "none"


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------
def _aliases_of(sel: dict) -> frozenset:
    return frozenset((it.get("alias") or "").lower()
                     for it in sel.get("items") or [] if it.get("alias"))


def _scope_of(sel: dict) -> tuple:
    """``((alias_lower, table), ...)`` for the FROM tree of one SELECT."""
    out: dict[str, str] = {}

    def walk(item):
        t = item.get("type")
        if t == "Table":
            parts = (item.get("name") or {}).get("parts") or []
            if parts:
                out[(item.get("alias") or parts[-1]).lower()] = parts[-1]
        elif t == "Join":
            walk(item["left"])
            walk(item["right"])

    for item in sel.get("from") or []:
        walk(item)
    return tuple(sorted(out.items()))


def _strip_raw(node):
    """Drop source offsets and raw spellings: the template keeps semantics only."""
    if isinstance(node, dict):
        node.pop("span", None)
        node.pop("raw", None)
        for v in node.values():
            _strip_raw(v)
    elif isinstance(node, list):
        for v in node:
            _strip_raw(v)
    return node


def _body_select(body):
    """The SELECT whose scope governs ORDER BY (descend through set operations)."""
    while isinstance(body, dict):
        t = body.get("type")
        if t == "Select":
            return body
        if t == "SetOp":
            body = body.get("r")
        elif t == "ParenQuery":
            body = body.get("query", {}).get("body")
        else:
            return None
    return None


def _children_with_paths(node: dict):
    """Yield (child, relative key) for every AST child of ``node``."""
    for key, val in node.items():
        if key in ("slots", "span", "type"):
            continue
        if isinstance(val, dict) and "type" in val:
            yield val, key
        elif isinstance(val, list):
            for i, item in enumerate(val):
                if isinstance(item, dict):
                    if "type" in item:
                        yield item, f"{key}[{i}]"
                    else:  # containers such as {"when": .., "then": ..}
                        for kk, vv in item.items():
                            if isinstance(vv, dict) and "type" in vv:
                                yield vv, f"{key}[{i}].{kk}"


def _iter_dicts(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_dicts(v)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def annotate_roles(ast: dict, *, schema=None, db_id: str | None = None,
                   question: str | None = None, evidence: str | None = None,
                   slot_tables: bool = True,
                   slot_values: bool = True) -> Annotated:
    """Return ``(template_ast, slots)``; ``ast`` itself is left untouched.

    ``slot_tables`` / ``slot_values`` exist for ablation: with them off, table
    names (resp. literal values) stay in the template, which is how the
    "columns only" signature level is produced.
    """
    work = _strip_raw(copy.deepcopy(ast))
    db = schema.db(db_id) if schema else None
    ann = _RoleAnnotator(schema=schema, db=db, slot_tables=slot_tables,
                         slot_values=slot_values)
    ann.precompute_scopes(work)
    template = ann.script(work)
    return Annotated(template=template, slots=ann.slots)


def role_sequence(template: dict) -> str:
    """Roles in AST (sid) order, e.g. ``TABLE>FILTER>MEASURE``."""
    found: list[tuple[int, str]] = []
    for n in _iter_dicts(template):
        if n.get("type") == "Slot":
            found.append((n.get("sid") or 0, n["role"]))
    return ">".join(role for _, role in sorted(found))


def _sig(node):
    """Normalised structural form used for template de-duplication.

    Literal *values* and slot contents never participate: only node types,
    operators and slot roles/kinds do.
    """
    if isinstance(node, list):
        return [_sig(v) for v in node]
    if not isinstance(node, dict):
        return node
    t = node.get("type")
    if t == "Literal":
        return {"t": "Lit", "k": node.get("kind")}
    if t == "Slot":
        return {"t": "Slot", "r": node["role"], "k": node.get("kind")}
    if t == "AliasRef":
        return {"t": "Alias", "n": node.get("name")}
    out = {"t": t}
    for k, v in node.items():
        # ``cardinality`` is implied by the structure below; leaving it out
        # keeps hashes comparable across versions that add or drop the label.
        # ``None`` carries no information and is dropped everywhere else too
        # (``ast_json.prune``), so signatures computed in memory and from the
        # written file are identical.
        if k in ("type", "span", "slots", "cardinality", "raw") or v is None:
            continue
        out[k] = _sig(v)
    return out


def signature(node: dict) -> str:
    """Short stable hash of a query/template (see :func:`_sig`)."""
    blob = json.dumps(_sig(node), sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def role_stats(records: list[dict]) -> dict:
    """Distributions and template de-duplication counts for one split."""
    kind_slots = {k: 0 for k in KINDS}
    role_column = {r: 0 for r in ROLES}
    role_value = {r: 0 for r in ROLES}
    queries_role = {r: 0 for r in ROLES}
    value_classes: dict[str, int] = {}
    combos: dict[str, int] = {}
    seqs: dict[str, int] = {}
    examples: dict[str, str] = {}
    sigs = {"ast": {}, "cols_only": {}, "template": {}}
    measure_bad = join_key_fk = join_key_bad = join_key_total = 0
    no_slot = 0
    n = 0

    for rec in records:
        if rec.get("status") != "ok":
            continue
        n += 1
        slots = rec["slots"]
        if not slots:
            no_slot += 1
        present = set()
        for s in slots:
            kind = s.get("kind")
            kind_slots[kind] = kind_slots.get(kind, 0) + 1
            if kind == "column":
                role_column[s["role"]] += 1
                present.add(s["role"])
                if (s["role"] == "MEASURE" and s.get("agg") in ("SUM", "AVG", "TOTAL")
                        and s.get("data_type") == "text"):
                    measure_bad += 1  # summing a text column is a real contradiction
                if s["role"] == "JOINKEY" and "data_type" in s:
                    join_key_total += 1
                    if s.get("key_role") in ("pk", "fk", "both"):
                        join_key_fk += 1
                    else:
                        join_key_bad += 1
            elif kind == "value":
                role_value[s["role"]] += 1
                cls = str(s.get("value_class"))
                value_classes[cls] = value_classes.get(cls, 0) + 1
        for r in present:
            queries_role[r] += 1
        combos[">".join(sorted(present))] = combos.get(">".join(sorted(present)), 0) + 1
        seq = role_sequence(rec["template"])
        seqs[seq] = seqs.get(seq, 0) + 1
        examples.setdefault(seq, rec["sql"].strip()[:160])
        for level, table in sigs.items():
            key = (rec.get("signatures") or {}).get(level)
            if key:
                table[key] = table.get(key, 0) + 1

    def _summary(m: dict) -> dict:
        return {"distinct": len(m),
                "singletons": sum(1 for v in m.values() if v == 1),
                "top_share": round(max(m.values()) / n, 4) if m and n else 0.0}

    return {
        "queries": n,
        "queries_without_slot": no_slot,
        "kind_slots": kind_slots,
        "role_column": role_column,
        "role_value": role_value,
        "queries_role_column": queries_role,
        "value_classes": value_classes,
        "role_sets": dict(sorted(combos.items(), key=lambda x: -x[1])),
        "role_sequences": [{"sequence": s, "count": c, "example_sql": examples[s]}
                           for s, c in sorted(seqs.items(), key=lambda x: -x[1])[:12]],
        "templates": {k: _summary(v) for k, v in sigs.items()},
        "sanity": {
            "measure_sum_on_text": measure_bad,
            "joinkey_with_schema": join_key_total,
            "joinkey_on_pk_or_fk": join_key_fk,
            "joinkey_not_a_key": join_key_bad,
        },
    }
