"""Zero-shot template generation: prompt, JSON contract, structural validation.

The model never sees columns: it gets the stage-1 table cards (name, size,
foreign-key neighbours, one-line summary) and must return slot-AST templates.
Concrete values are returned in a separate ``values`` map so the template stays
free of schema *and* of literal data.
"""

from __future__ import annotations

import copy
import json

from .llm import chat, extract_json

__all__ = ["build_prompt", "generate_templates", "validate_bundle",
           "collect_slots", "ROLES", "KINDS"]

ROLES = ("TABLE", "JOINKEY", "MEASURE", "DIMENSION", "FILTER", "SORT")
KINDS = ("table", "column", "value", "field")

#: lenient aliases for node/field names models tend to invent
_TYPE_ALIASES = {
    "aggregate": "Func", "aggregation": "Func", "function": "Func",
    "comparison": "BinaryOp", "binary": "BinaryOp", "op": "BinaryOp",
    "and": "Logical", "or": "Logical", "boolean": "Logical",
    "condition": "Logical", "predicate": "BinaryOp",
    "table_ref": "Table", "relation": "Table", "from": "Table",
    "selectitem": "SelectItem", "item": "SelectItem", "projection": "SelectItem",
    "wildcard": "Star", "columnref": "Slot", "column_ref": "Slot",
    "slotref": "Slot", "hole": "Slot",
}

_SPEC = """AST node kinds (JSON objects, "type" is required; omit optional fields when unused):

Query     {"type":"Query","cardinality":"SINGLE|TOP_K|GROUPED|LIST|SET","with":null,
           "body":<Select|SetOp>,"order_by":[OrderItem],"limit":Limit,"offset":null}
Select    {"type":"Select","distinct":false,"items":[SelectItem],"from":[Table|Join],
           "where":<expr>,"group_by":[<expr>],"having":<expr>}
SelectItem{"type":"SelectItem","expr":<expr>,"alias":"name"} | {"type":"SelectItem","star":{}}
Table     {"type":"Table","name":<TABLE slot>,"alias":"t1"}
Join      {"type":"Join","kind":"INNER|LEFT","left":<Table|Join>,"right":<Table|Join>,
           "on":<expr>,"using":null,"natural":null}
SetOp     {"type":"SetOp","op":"UNION|INTERSECT|EXCEPT","all":false,"l":<body>,"r":<body>}
CTE       {"type":"CTE","name":"c1","body":<Query>}
OrderItem {"type":"OrderItem","expr":<expr>,"desc":true}
Limit     {"type":"Limit","expr":<VALUE slot>}
Slot      {"type":"Slot","role":"<role>","kind":"table|column|value",
           "value":<string|number|boolean>}   ("value" only when kind is "value")
expr      Func{"type":"Func","name":"COUNT","args":[<expr>],"distinct":false}
          BinaryOp{"type":"BinaryOp","op":"=|<|>|<=|>=|!=|+|-|*|/","l":<expr>,"r":<expr>}
          Logical{"type":"Logical","op":"AND|OR","l":<expr>,"r":<expr>}
          Not{"type":"Not","e":<expr>}
          Between{"type":"Between","e":<expr>,"lo":<expr>,"hi":<expr>,"negated":false}
          In{"type":"In","e":<expr>,"list":[<expr>],"negated":false}
          Like{"type":"Like","e":<expr>,"op":"LIKE","pattern":<expr>}
          IsTest{"type":"IsTest","e":<expr>,"test":"NULL","negated":true}
          Case{"type":"Case","whens":[{"when":<expr>,"then":<expr>}],"else":<expr>}
          Cast{"type":"Cast","e":<expr>,"to":"REAL"}
          Subquery{"type":"Subquery","query":<Query>}
          Paren{"type":"Paren","e":<expr>}
          NullLiteral{"type":"Literal","kind":"null"}
          BoolLiteral{"type":"Literal","kind":"bool","value":true}"""

_RULES = """Hard rules:
1. Never write a real table or column name. A table reference is a TABLE slot;
   every column reference is a column slot (kind "column"). The database schema is
   not shown to you, so guessing names is always wrong.
2. Every concrete data value (string, number, date, LIKE pattern, format string,
   LIMIT count) becomes a value slot of kind "value", and the data itself goes into
   that slot's "value" field. Data must not appear anywhere else.
3. Do not write "sid" - slot ids are assigned by the caller in traversal order.
4. Roles: TABLE for relations; JOINKEY for join predicates and for the key compared
   through IN/=/(SELECT ...) subqueries; MEASURE for every projected column and every
   aggregate argument; DIMENSION for GROUP BY keys; FILTER for WHERE/HAVING
   predicates, for the LIMIT count, and for values compared against a column; SORT
   for ORDER BY keys.
5. cardinality: SINGLE when the projection is a single aggregate row, TOP_K when a
   LIMIT is used, GROUPED when GROUP BY has no LIMIT, SET for DISTINCT, else LIST.
6. Follow the evidence. When the evidence defines how a value is computed (for
   example "eligible free rate = free meal count / enrollment"), reproduce that
   computation as the same arithmetic over MEASURE slots, with FILTER slots for the
   conditions it mentions. Do not replace a defined computation by some other single
   column, and do not invent a computation the evidence does not support."""

_SIMPLE_RULES = """This template must be SIMPLE:
- exactly one SQL statement: one Query whose body is one Select (no set operation);
- only aggregate functions (COUNT, SUM, AVG, MIN, MAX, TOTAL) plus arithmetic
  (+ - * /) and comparisons; no other function;
- filters only in WHERE/HAVING; no CASE expression;
- no Common Table Expression, no window function (no OVER), no subquery nested more
  than one level deep."""

_COMPLEX_RULES = """This template must be COMPLEX:
- use the full query language when the question needs it: subqueries, IN/EXISTS,
  UNION/INTERSECT/EXCEPT, Common Table Expressions, CASE, window functions (OVER),
  or any other function;
- nest as deeply as the question requires - there is no nesting limit."""

_FORMAT = """Return ONLY this JSON object, no markdown fence, no commentary:
{"template": {"type":"Script","stmts":[<Query>]}}"""


def build_prompt(cards: list[dict], question: str, evidence: str | None,
                 cfg: dict, kind: str = "simple") -> str:
    """Prompt for one template of ``kind`` ("simple" or "complex")."""
    lines = ["You write SQL query templates for a text-to-SQL system.",
             "The database is described only by short table cards (what each table stores,",
             "its size, its links). Column lists are not shown: tables and columns must",
             "stay as slots.", "",
             "Database tables:"]
    for card in cards:
        neighbours = ", ".join(card.get("neighbours") or []) or "-"
        lines.append(
            f"- {card['name']} ({card.get('rows')} rows, {card['n_columns']} cols; "
            f"links: {neighbours}): {card.get('summary', '').strip()}")
    lines += ["", "Evidence (dataset annotation - follow it, especially the definition "
                  "of the measure to compute):",
              f"{evidence or '(none)'}", "",
              _SPEC, "", _RULES, "",
              _SIMPLE_RULES if kind == "simple" else _COMPLEX_RULES, "",
              f"Question: {question}", "",
              _FORMAT]
    return "\n".join(lines)


def generate_templates(cards: list[dict], question: str, evidence: str | None,
                       cfg: dict) -> dict:
    """Generate the simple and the complex template, each with its own retry loop."""
    templates_cfg = cfg.get("templates") or {}
    wanted = [("simple", int(templates_cfg.get("simple_rounds", 2))),
              ("complex", int(templates_cfg.get("complex_rounds", 3)))]
    bundles, prompts = [], []
    for kind, rounds in wanted:
        prompt = build_prompt(cards, question, evidence, cfg, kind)
        prompts.append(prompt)
        last_error = ""
        for attempt in range(max(1, rounds)):
            text = chat(prompt if attempt == 0 else
                        f"{prompt}\n\nYour previous answer was rejected: {last_error}\n"
                        "Return the corrected JSON object only.",
                        cfg, system="You are a precise query-template generator.")
            if not text:
                last_error = "empty model output"
                continue
            try:
                bundle = validate_template(extract_json(text), cfg, kind)
            except ValueError as exc:
                last_error = str(exc)
                continue
            bundle.update(kind=kind, prompt=prompt, rounds=attempt + 1, raw=text)
            bundles.append(bundle)
            break
        else:
            bundles.append({"kind": kind, "index": len(bundles) + 1,
                            "error": last_error, "prompt": prompt})
    return {"templates": bundles, "prompts": prompts}


def _norm_type(node: dict) -> str:
    raw = str(node.get("type", ""))
    if raw in _KNOWN:
        return raw
    return _TYPE_ALIASES.get(raw.lower(), raw)


_KNOWN = {
    "Script", "Query", "Select", "SelectItem", "Table", "Join", "SetOp", "CTE",
    "OrderItem", "Limit", "Offset", "Slot", "Func", "BinaryOp", "Logical", "Not",
    "Between", "In", "Like", "IsTest", "Case", "Cast", "Subquery", "Paren",
    "Literal", "Star", "AliasRef", "Unnest", "DerivedTable", "Tuple",
    "ArrayLit", "TypedLiteral", "Interval", "Extract", "Values",
    "ParenQuery", "IsDistinct", "Index", "FieldAccess", "JsonPath",
}


def collect_slots(node, out: list[dict]) -> list[dict]:
    if isinstance(node, dict):
        if node.get("type") == "Slot":
            out.append(node)
        for value in node.values():
            collect_slots(value, out)
    elif isinstance(node, list):
        for value in node:
            collect_slots(value, out)
    return out


def validate_template(data: dict, cfg: dict, kind: str) -> dict:
    """Validate one model output; raise ``ValueError`` on any structural problem."""
    if not isinstance(data, dict) or not isinstance(data.get("template"), dict):
        raise ValueError('output must be {"template": {...}}')
    template = _normalise(data["template"])
    if template.get("type") != "Script":
        raise ValueError("top node must be Script")
    _check_shapes(template, 1)
    values = _number_slots(template)
    _check_template(template, 1, values)
    if kind == "simple":
        _check_simple(template)
    return {"index": 1, "template": template, "values": values}


_SIMPLE_FUNCS = {"COUNT", "SUM", "AVG", "MIN", "MAX", "TOTAL"}


def _check_simple(template: dict) -> None:
    """Enforce the simple-template constraints (one statement, no advanced features)."""
    stmts = template.get("stmts") or []
    if len(stmts) != 1:
        raise ValueError("simple template must be exactly one statement")
    if stmts[0].get("type") != "Query":
        raise ValueError("simple template must be a single Query")

    def walk(node, depth):
        if isinstance(node, dict):
            kind = node.get("type")
            if kind == "Query":
                if node.get("with"):
                    raise ValueError("simple template must not use CTEs")
                if depth > 1:
                    raise ValueError("simple template must not nest subqueries "
                                     "more than one level")
                for value in node.values():
                    walk(value, depth)
                return
            if kind == "SetOp":
                raise ValueError("simple template must not use set operations")
            if kind == "Case":
                raise ValueError("simple template must not use CASE")
            if kind == "Func":
                name = str(node.get("name", "")).upper()
                if name not in _SIMPLE_FUNCS:
                    raise ValueError(f"simple template must not use function {name}")
                if node.get("over"):
                    raise ValueError("simple template must not use window functions")
            if kind == "Subquery":
                walk(node.get("query"), depth + 1)
                return
            for value in node.values():
                walk(value, depth)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth)

    walk(stmts[0], 0)


def _number_slots(template: dict) -> dict[str, object]:
    """Assign sids 1..N in traversal order and lift value-slot data out."""
    values: dict[str, object] = {}
    counter = [0]

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "Slot":
                counter[0] += 1
                node["sid"] = counter[0]
                if node.get("kind") == "value":
                    values[str(counter[0])] = node.pop("value", None)
                else:
                    node.pop("value", None)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(template)
    return values


def _check_shapes(node, index: int) -> None:
    """Reject unknown node types / kinds / roles before anything else runs."""
    if isinstance(node, dict):
        kind = node.get("type")
        if kind is None:                     # plain container (e.g. an empty window spec)
            for value in node.values():
                _check_shapes(value, index)
            return
        if kind not in _KNOWN:
            raise ValueError(f"template {index}: unknown node type {kind!r}")
        if kind == "Slot":
            if node.get("role") not in ROLES:
                raise ValueError(f"template {index}: bad role {node.get('role')!r}")
            if node.get("kind") not in KINDS:
                raise ValueError(f"template {index}: bad kind {node.get('kind')!r}")
        for value in node.values():
            _check_shapes(value, index)
    elif isinstance(node, list):
        for value in node:
            _check_shapes(value, index)


def _normalise(node):
    """Map model-invented node type aliases and drop empty fields."""
    if isinstance(node, list):
        return [_normalise(v) for v in node]
    if not isinstance(node, dict):
        return node
    node = copy.deepcopy(node)
    if "type" in node:
        node["type"] = _norm_type(node)
    for key, value in list(node.items()):
        if isinstance(value, (dict, list)):
            node[key] = _normalise(value)
    return {k: v for k, v in node.items() if v is not None}


def _check_template(template: dict, index: int, values: dict) -> None:
    if not template.get("stmts"):
        raise ValueError(f"template {index}: Script has no stmts")
    slots = collect_slots(template, [])
    if not slots:
        raise ValueError(f"template {index}: no slots at all")
    sids = [s.get("sid") for s in slots]
    if sorted(sids) != list(range(1, len(sids) + 1)):
        raise ValueError(f"template {index}: internal sid numbering is broken")
    value_sids = {str(s["sid"]) for s in slots if s["kind"] == "value"}
    missing = value_sids - set(values)
    if missing:
        raise ValueError(f"template {index}: value slots "
                         f"{sorted(missing, key=int)} have no data")
    table_sids = {s["sid"] for s in slots if s["kind"] == "table"}
    if not table_sids:
        raise ValueError(f"template {index}: no TABLE slot - a query needs a relation")
    _check_schema_free(template, index, table_sids)


def _check_schema_free(node, index: int, table_sids: set) -> None:
    """Reject any real table/column name, stray data, or bare literals."""
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "Column":
            raise ValueError(f"template {index}: real Column node "
                             f"{node.get('parts')} - use a column slot")
        if kind == "Table":
            name = node.get("name")
            if not (isinstance(name, dict) and name.get("type") == "Slot"):
                raise ValueError(f"template {index}: Table has a real name {name!r}")
            if name.get("sid") not in table_sids:
                raise ValueError(f"template {index}: Table slot must be kind=table")
        if kind == "Literal":
            if node.get("kind") in ("string", "number"):
                raise ValueError(f"template {index}: inline literal "
                                 f"{node.get('value')!r} - use a value slot")
            if node.get("kind") not in ("null", "bool"):
                raise ValueError(f"template {index}: literal kind "
                                 f"{node.get('kind')!r} is not allowed")
        for value in node.values():
            _check_schema_free(value, index, table_sids)
    elif isinstance(node, list):
        for value in node:
            _check_schema_free(value, index, table_sids)
