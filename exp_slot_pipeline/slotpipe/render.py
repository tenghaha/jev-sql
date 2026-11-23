"""Unparse a filled AST back to a SQLite statement.

Parentheses are emitted conservatively around every compound expression, so
precedence cannot be lost; the goal is a valid, faithful statement rather than
pretty output.
"""

from __future__ import annotations

__all__ = ["to_sql", "RenderError"]


class RenderError(Exception):
    pass


def to_sql(script: dict) -> str:
    if script.get("type") == "Script":
        stmts = script.get("stmts") or []
        if not stmts:
            raise RenderError("empty script")
        return query(stmts[0]) + ";"
    return query(script) + ";"


def query(node: dict) -> str:
    kind = node.get("type")
    if kind == "Query":
        parts = []
        ctes = node.get("with") or []
        if ctes:
            parts.append("WITH " + ", ".join(
                f"{ident(c['name'])} AS ({query(c['body'])})" for c in ctes))
        parts.append(body(node.get("body") or {}))
        order = node.get("order_by") or []
        if order:
            parts.append("ORDER BY " + ", ".join(_order(item) for item in order))
        limit = node.get("limit")
        if limit:
            parts.append("LIMIT " + expr(limit.get("expr")))
        offset = node.get("offset")
        if offset:
            parts.append("OFFSET " + expr(offset.get("expr")))
        return " ".join(p for p in parts if p)
    return body(node)


def body(node: dict) -> str:
    kind = node.get("type")
    if kind == "Select":
        return select(node)
    if kind == "SetOp":
        op = node.get("op", "UNION")
        all_ = " ALL" if node.get("all") else ""
        return f"{body(node['l'])} {op}{all_} {body(node['r'])}"
    if kind == "ParenQuery":
        return f"({query(node['query'])})"
    if kind == "Values":
        rows = ", ".join("(" + ", ".join(expr(v) for v in row) + ")"
                         for row in node.get("rows") or [])
        return f"VALUES {rows}"
    raise RenderError(f"unsupported query body: {kind}")


def select(node: dict) -> str:
    parts = ["SELECT"]
    distinct = node.get("distinct")
    if isinstance(distinct, dict) and distinct.get("kind") == "distinct":
        parts.append("DISTINCT")
    elif distinct is True:
        parts.append("DISTINCT")
    parts.append(", ".join(item for item in
                           (_select_item(i) for i in node.get("items") or [])))
    from_items = node.get("from") or []
    if from_items:
        parts.append("FROM " + ", ".join(from_item(i) for i in from_items))
    if node.get("where") is not None:
        parts.append("WHERE " + expr(node["where"]))
    group = node.get("group_by") or []
    if group:
        parts.append("GROUP BY " + ", ".join(expr(g) for g in group))
    if node.get("having") is not None:
        parts.append("HAVING " + expr(node["having"]))
    return " ".join(parts)


def _select_item(item: dict) -> str:
    star = item.get("star")
    if star is not None:
        qualifier = star.get("qualifier")
        return f"{ident(qualifier)}.*" if qualifier else "*"
    text = expr(item.get("expr"))
    alias = item.get("alias")
    return f"{text} AS {ident(alias)}" if alias else text


def from_item(node: dict) -> str:
    kind = node.get("type")
    if kind == "Table":
        name = node.get("name") or {}
        parts = name.get("parts") or []
        if not parts:
            raise RenderError("Table node without a name")
        text = ".".join(ident(p) for p in parts)
        alias = node.get("alias")
        return f"{text} AS {ident(alias)}" if alias else text
    if kind == "Join":
        left = from_item(node["left"])
        if isinstance(node["left"], dict) and node["left"].get("type") == "Join":
            left = f"({left})"          # a chained join needs its own ON clause
        right = from_item(node["right"])
        if isinstance(node["right"], dict) and node["right"].get("type") == "Join":
            right = f"({right})"        # right-nested joins need their own ON too
        kind_text = node.get("kind") or "INNER"
        on = node.get("on")
        using = node.get("using")
        if using:
            cond = "USING (" + ", ".join(ident(u) if isinstance(u, str) else expr(u)
                                         for u in using) + ")"
        elif on is not None:
            cond = "ON " + expr(on)
        else:
            cond = ""
        return f"{left} {kind_text} JOIN {right} {cond}".strip()
    if kind == "DerivedTable":
        text = f"({query(node['query'])})"
        alias = node.get("alias")
        return f"{text} AS {ident(alias)}" if alias else text
    raise RenderError(f"unsupported FROM item: {kind}")


def _order(item: dict) -> str:
    node = item.get("expr") if item.get("type") == "OrderItem" else item
    text = expr(node)
    return text + " DESC" if item.get("desc") else text


# ---------------------------------------------------------------------------
# expressions
# ---------------------------------------------------------------------------
def expr(node) -> str:
    if not isinstance(node, dict):
        raise RenderError(f"bad expression node: {node!r}")
    kind = node.get("type")
    if kind == "Literal":
        return literal(node)
    if kind == "Column":
        parts = node.get("parts") or []
        if not parts:
            raise RenderError("Column node without parts")
        return ".".join(ident(p) for p in parts)
    if kind == "Star":
        return "*"
    if kind == "Paren":
        return f"({expr(node['e'])})"
    if kind == "BinaryOp":
        op = node.get("op")
        return f"({expr(node['l'])} {op} {expr(node['r'])})"
    if kind == "Logical":
        return f"({expr(node['l'])} {node.get('op', 'AND')} {expr(node['r'])})"
    if kind == "Not":
        return f"(NOT {expr(node['e'])})"
    if kind == "Between":
        neg = "NOT " if node.get("negated") else ""
        return (f"({expr(node['e'])} {neg}BETWEEN {expr(node['lo'])} "
                f"AND {expr(node['hi'])})")
    if kind == "In":
        neg = "NOT " if node.get("negated") else ""
        if node.get("query") is not None:
            inner = query(node["query"])
        else:
            inner = ", ".join(expr(v) for v in node.get("list") or [])
        return f"({expr(node['e'])} {neg}IN ({inner}))"
    if kind == "Like":
        neg = "NOT " if node.get("negated") else ""
        op = node.get("op") or "LIKE"
        text = f"({expr(node['e'])} {neg}{op} {expr(node['pattern'])}"
        if node.get("escape") is not None:
            text += f" ESCAPE {expr(node['escape'])}"
        return text + ")"
    if kind == "IsTest":
        neg = "NOT " if node.get("negated") else ""
        return f"({expr(node['e'])} IS {neg}{node.get('test', 'NULL')})"
    if kind == "IsDistinct":
        neg = "NOT " if node.get("negated") else ""
        return f"({expr(node['e'])} IS {neg}DISTINCT FROM {expr(node['other'])})"
    if kind == "Exists":
        return f"EXISTS ({query(node['query'])})"
    if kind == "Subquery":
        return f"({query(node['query'])})"
    if kind == "Func":
        args = [expr(a) for a in node.get("args") or []]
        distinct = "DISTINCT " if node.get("distinct") else ""
        text = f"{node.get('name', '')}({distinct}{', '.join(args)})"
        if node.get("filter") is not None:
            text += f" FILTER (WHERE {expr(node['filter'])})"
        over = node.get("over")
        if isinstance(over, dict):
            text += " OVER (" + _window(over) + ")"
        return text
    if kind == "Case":
        parts = ["CASE"]
        if node.get("base") is not None:
            parts.append(expr(node["base"]))
        for branch in node.get("whens") or []:
            parts.append(f"WHEN {expr(branch['when'])} THEN {expr(branch['then'])}")
        if node.get("else") is not None:
            parts.append("ELSE " + expr(node["else"]))
        parts.append("END")
        return " ".join(parts)
    if kind == "Cast":
        return f"CAST({expr(node['e'])} AS {node.get('to', 'TEXT')})"
    if kind == "Tuple":
        return "(" + ", ".join(expr(v) for v in node.get("items") or []) + ")"
    if kind == "ArrayLit":
        return "[" + ", ".join(expr(v) for v in node.get("items") or []) + "]"
    if kind == "Extract":
        field = str(node.get("field", "YEAR")).upper()
        code = {"YEAR": "%Y", "MONTH": "%m", "DAY": "%d"}.get(field, "%Y")
        return f"CAST(STRFTIME('{code}', {expr(node['e'])}) AS INTEGER)"
    if kind == "AliasRef":
        return ident(node.get("name"))
    raise RenderError(f"unsupported expression: {kind}")


def _window(spec: dict) -> str:
    parts = []
    if spec.get("partition_by"):
        parts.append("PARTITION BY " + ", ".join(expr(e) for e in spec["partition_by"]))
    if spec.get("order_by"):
        parts.append("ORDER BY " + ", ".join(_order(i) for i in spec["order_by"]))
    return " ".join(parts)


def literal(node: dict) -> str:
    kind = node.get("kind")
    value = node.get("value")
    if kind == "null":
        return "NULL"
    if kind == "bool":
        return "TRUE" if value else "FALSE"
    if kind == "number":
        return str(value)
    text = "" if value is None else str(value)
    return "'" + text.replace("'", "''") + "'"


_KEYWORDS = set("""
select from where group by having order limit offset join inner left right outer
on using as and or not in is null like between case when then else end distinct
union intersect except all insert update delete values set table index
""".split())


def ident(name) -> str:
    text = "" if name is None else str(name)
    return '"' + text.replace('"', '""') + '"'
