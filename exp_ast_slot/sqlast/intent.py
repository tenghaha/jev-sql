"""Query-level intent: how many rows / what shape the answer has.

The AST records syntax; ``cardinality`` records the one decision that every
downstream step needs first: is the answer a single value, a ranked top-k, one
row per group, a list, or a set?

Values
------
===========  ==============================================================
 ``SINGLE``    one row by construction: the projection is aggregate-only, or
               the query has no FROM clause
 ``TOP_K``     the query has a LIMIT, i.e. it returns at most k rows
 ``GROUPED``   GROUP BY without LIMIT: one row per group
 ``LIST``      an ordered-free multi-row list (the default)
 ``SET``       de-duplicated rows: projection-level DISTINCT or UNION/INTERSECT/
               EXCEPT without ALL
===========  ==============================================================

Precedence (first match wins, so the label is deterministic)::

    GROUP BY + LIMIT  -> TOP_K        GROUP BY          -> GROUPED
    aggregate-only projection         -> SINGLE         (a LIMIT cannot change it)
    no FROM, no window                -> SINGLE
    LIMIT                             -> TOP_K
    projection DISTINCT               -> SET
    set operation (no ALL)            -> SET
    otherwise                         -> LIST

Consequences worth knowing: ``SELECT COUNT(*) ... LIMIT 1`` is ``SINGLE`` (the
aggregate already guarantees one row); ``SELECT x ... GROUP BY x ORDER BY c
LIMIT 2`` is ``TOP_K`` (the top-2 groups); ``COUNT(DISTINCT x)`` stays inside a
``SINGLE`` because that DISTINCT is a property of the aggregate, not of the
projection.

The field is a deterministic function of the branch structure that is already in
the template signature, so the signature omits it on purpose (see
``roles._sig``): hashes stay comparable across runs and only the label is new.

Usage
-----
    from sqlast.intent import cardinality_of, annotate_intent
    annotate_intent(ast)           # sets ast["stmts"][i]["cardinality"]
"""

from __future__ import annotations

__all__ = ["CARDINALITIES", "cardinality_of", "annotate_intent"]

CARDINALITIES = ("SINGLE", "TOP_K", "GROUPED", "LIST", "SET")


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------
def _iter_dicts(node):
    """Yield every dict node, depth first."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _iter_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_dicts(value)


def _has_aggregate(node) -> bool:
    """True when an aggregate function appears (window aggregates excluded)."""
    return any(n.get("type") == "Func" and n.get("is_aggregate") and "over" not in n
               for n in _iter_dicts(node))


def _projection_is_aggregate(item: dict) -> bool:
    if "star" in item:
        return False
    expr = item.get("expr")
    return isinstance(expr, dict) and _has_aggregate(expr)


def _setop_is_dedup(node: dict) -> bool:
    """True when no set operation in the tree keeps duplicates (UNION ALL ...)."""
    for n in _iter_dicts(node):
        if n.get("type") == "SetOp" and n.get("all"):
            return False
    return True


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------
def cardinality_of(query: dict) -> str:
    """Classify one ``Query`` node (see the table in the module docstring)."""
    body = query.get("body")
    if not isinstance(body, dict):
        return "LIST"

    kind = body.get("type")
    if kind == "SetOp":
        return "SET" if _setop_is_dedup(body) else "LIST"
    if kind == "ParenQuery":
        return cardinality_of(body.get("query") or {})
    if kind != "Select":
        return "LIST"  # VALUES

    select = body
    items = select.get("items") or []
    if not items:
        return "LIST"

    has_group = bool(select.get("group_by"))
    has_limit = query.get("limit") is not None
    if has_group:
        return "TOP_K" if has_limit else "GROUPED"

    aggregate_only = all(_projection_is_aggregate(it) for it in items)
    has_from = bool(select.get("from"))
    has_window = any(n.get("type") == "Func" and "over" in n
                     for n in _iter_dicts(select))
    if aggregate_only or (not has_from and not has_window):
        return "SINGLE"

    if has_limit:
        return "TOP_K"
    if select.get("distinct"):
        return "SET"
    return "LIST"


def annotate_intent(ast: dict) -> dict:
    """Set ``cardinality`` on every ``Query`` node (top level, CTEs, subqueries)."""
    # collect first: writing into a dict that the walker is iterating would
    # invalidate the generator
    queries = [n for n in _iter_dicts(ast) if n.get("type") == "Query"]
    for query in queries:
        label = cardinality_of(query)
        # keep the dict identity (parents hold the same object) but move the
        # label next to ``type`` so the artifact reads top-down
        rest = {k: v for k, v in query.items() if k not in ("type", "cardinality")}
        query.clear()
        query.update({"type": "Query", "cardinality": label, **rest})
    return ast
