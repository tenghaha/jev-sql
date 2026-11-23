"""JEV slot binding: one question set per slot, positives kept as candidates.

Both stages ask ``noul`` questions (probability in [0, 1]).  Every positive answer
is kept as a candidate of that slot; the binder then picks one candidate per slot
(highest probability first, preferring unused and type-compatible candidates).

Shared state (identical for every question of a stage):

* question, evidence and the **whole slot-AST template** - so each decision sees
  the query it is filling;
* table stage: all table cards of the database (table-level schema, no columns);
* column stage: one table at a time - that table's column-level schema only.

Table stage: one call holds every (table slot, table) question.
Column stage: one call per table holds every (column slot, column of that table)
question, so all columns of a table are covered in a single pass.
"""

from __future__ import annotations

import copy
import json

from .llm import jev_call
from .template import collect_slots

__all__ = ["fill", "bind_table_slots", "bind_column_slots", "slot_context",
           "column_slot_scopes", "slot_usage", "column_slot_usage"]

_NUMERIC = {"integer", "real", "number", "float", "decimal", "double", "numeric"}
_AGG_NUMERIC = {"SUM", "AVG", "TOTAL", "MEDIAN", "STDDEV", "VARIANCE"}


def _noul(answer) -> float:
    if isinstance(answer, dict):
        for key in ("noul", "yes"):
            if isinstance(answer.get(key), (int, float)):
                return float(answer[key])
    return 0.0


def _template_json(template: dict) -> str:
    return json.dumps(template, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# slot context and scope
# ---------------------------------------------------------------------------
def slot_context(template: dict) -> dict[int, dict]:
    """Map ``sid -> {clause, agg, arith, parent, parent_op, peer_value_sid}``."""
    out: dict[int, dict] = {}

    def walk(node, clause, agg, arith=False, parent=None, parent_op=None):
        if isinstance(node, dict):
            kind = node.get("type")
            if kind == "Slot":
                peer = None
                if parent and parent.get("type") in ("BinaryOp", "Like", "Between", "In"):
                    for key in ("l", "r", "e", "pattern", "lo", "hi"):
                        other = parent.get(key)
                        if isinstance(other, dict) and other is not node and \
                                other.get("type") == "Slot" and other.get("kind") == "value":
                            peer = other.get("sid")
                out[node["sid"]] = {"clause": clause, "agg": agg, "arith": arith,
                                    "parent": (parent or {}).get("type"),
                                    "parent_op": parent_op, "peer_value_sid": peer}
                return
            if kind == "Select":
                for item in node.get("items") or []:
                    walk(item, "select", agg, arith, item)
                for item in node.get("from") or []:
                    walk(item, "from", agg, arith, item)
                for key in ("where", "having", "qualify"):
                    if node.get(key) is not None:
                        walk(node[key], key, agg, arith, node)
                for expr in node.get("group_by") or []:
                    walk(expr, "group", agg, arith, node)
                return
            if kind == "Query":
                for cte in node.get("with") or []:
                    walk(cte, clause, agg, arith, node)
                walk(node.get("body"), clause, agg, arith, node)
                for item in node.get("order_by") or []:
                    walk(item, "order", agg, arith, node)
                if node.get("limit") is not None:
                    walk(node["limit"], "limit", agg, arith, node)
                return
            if kind == "Func":
                name = str(node.get("name", "")).upper()
                next_agg = name if name in _AGG_NUMERIC or name in (
                    "COUNT", "MIN", "MAX", "TOTAL") else agg
                for arg in node.get("args") or []:
                    walk(arg, clause, next_agg, arith, node, name)
                return
            if kind == "BinaryOp":
                op = node.get("op")
                for key in ("l", "r"):
                    walk(node.get(key), clause, agg,
                         arith or op in ("/", "*", "+", "-"), node, op)
                return
            if kind == "Between":
                for key in ("e", "lo", "hi"):
                    walk(node.get(key), clause, agg, arith, node, "BETWEEN")
                return
            if kind in ("In", "Like"):
                op = node.get("op") if kind == "Like" else "IN"
                walk(node.get("e"), clause, agg, arith, node, op)
                for key in ("list", "pattern"):
                    if node.get(key) is not None:
                        walk(node[key], clause, agg, arith, node, op)
                if isinstance(node.get("query"), dict):
                    walk(node["query"], clause, agg, arith, node, op)
                return
            if kind == "IsTest":
                walk(node.get("e"), clause, agg, arith, node, "IS")
                return
            if kind == "OrderItem":
                walk(node.get("expr"), "order", agg, arith, node)
                return
            if kind == "Table":
                walk(node.get("name"), "from", agg, arith, node)
                return
            if kind == "Join":
                walk(node.get("left"), "from", agg, arith, node)
                walk(node.get("right"), "from", agg, arith, node)
                walk(node.get("on"), "join", agg, arith, node, "JOIN")
                return
            for value in node.values():
                walk(value, clause, agg, arith, node)
        elif isinstance(node, list):
            for value in node:
                walk(value, clause, agg, arith, parent)

    walk(template, "select", None)
    return out


def _iter_from(items):
    """Yield Table nodes of one FROM list (never descending into subqueries)."""
    for node in items:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "Table":
            yield node
        elif node.get("type") == "Join":
            yield from _iter_from([node.get("left")])
            yield from _iter_from([node.get("right")])


def column_slot_scopes(template: dict, table_bindings: dict) -> dict[int, list[str]]:
    """For each column slot, the relations visible in its query block."""
    scopes: dict[int, list[str]] = {}

    def bind_scope(from_items, scope: list[str]) -> None:
        for table_node in _iter_from(from_items):
            name = table_node.get("name")
            if isinstance(name, dict) and name.get("type") == "Slot":
                table = table_bindings.get(name["sid"])
                if table and table not in scope:
                    scope.append(table)

    def walk(node, visible: list[str]):
        if isinstance(node, dict):
            if node.get("type") == "Slot" and node.get("kind") == "column":
                scopes[node["sid"]] = list(visible)
                return
            if node.get("type") == "Query":
                scope: list[str] = []
                body = node.get("body")
                if isinstance(body, dict):
                    if body.get("type") == "Select":
                        bind_scope(body.get("from") or [], scope)
                    elif body.get("type") == "SetOp":
                        for side in (body.get("l"), body.get("r")):
                            if isinstance(side, dict) and side.get("type") == "Select":
                                bind_scope(side.get("from") or [], scope)
                inner = visible + [t for t in scope if t not in visible]
                for cte in node.get("with") or []:
                    walk(cte, visible)
                walk(body, inner)
                for item in node.get("order_by") or []:
                    walk(item, inner)
                walk(node.get("limit"), inner)
                walk(node.get("offset"), inner)
                return
            for value in node.values():
                walk(value, visible)
        elif isinstance(node, list):
            for value in node:
                walk(value, visible)

    walk(template, [])
    return scopes


# ---------------------------------------------------------------------------
# slot wording used inside the questions
# ---------------------------------------------------------------------------
def slot_usage(template: dict, sid: int) -> str:
    """Wording for a TABLE slot: alone in FROM, one side of a join, or a subquery."""
    found = {"text": "a relation of the query"}

    def walk(node, parent=None):
        if isinstance(node, dict):
            if node.get("type") == "Table":
                name = node.get("name")
                if isinstance(name, dict) and name.get("sid") == sid:
                    if parent and parent.get("type") == "Join":
                        found["text"] = ("one side of a join; the query compares a key "
                                         "across two relations")
                    elif parent and parent.get("type") == "Query":
                        found["text"] = "the single relation of a subquery"
                    else:
                        found["text"] = "the relation the query selects from"
            for value in node.values():
                walk(value, node)
        elif isinstance(node, list):
            for value in node:
                walk(value, parent)

    walk(template)
    return found["text"]


def column_slot_usage(context: dict, values: dict | None = None) -> str:
    """Wording for a column slot: role, clause, aggregation, compared value."""
    parts = [f"role {context.get('role')}"]
    where = {
        "select": "in the SELECT list",
        "where": "in the WHERE clause",
        "having": "in the HAVING clause",
        "group": "in GROUP BY",
        "order": "in ORDER BY",
        "limit": "in the row limit",
        "join": "in a join condition",
        "qualify": "in the window filter",
    }.get(context.get("clause") or "select",
          f"in {context.get('clause')}")
    parts.append(where)
    if context.get("agg"):
        parts.append(f"aggregated with {context['agg']}")
    if context.get("arith"):
        parts.append("an operand of an arithmetic expression")
    peer = context.get("peer_value_sid")
    if peer is not None and values:
        text = values.get(str(peer))
        if isinstance(text, str) and text:
            parts.append(f"compared with the value '{text}'")
        elif text is not None:
            parts.append(f"compared with the value {text}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# stage 1: table slots - one call over all slots x all table cards
# ---------------------------------------------------------------------------
def bind_table_slots(template: dict, cards: list[dict], db_id: str, question: str,
                     evidence: str | None, cfg: dict) -> dict:
    """Bind every TABLE slot; positives are kept as that slot's candidates."""
    threshold = float((cfg.get("fill") or {}).get("noul_threshold", 0.5))
    slots = [s for s in collect_slots(template, []) if s["kind"] == "table"]
    if not slots:
        return {"ok": False, "reason": "template has no table slots",
                "slots": {}, "selected": {}, "num_questions": 0}

    usages = {s["sid"]: slot_usage(template, s["sid"]) for s in slots}
    candidates, questions = {}, {}
    for index, card in enumerate(cards, start=1):
        tid = f"t{index}"
        candidates[tid] = {
            "table_name": card["name"],
            "rows": card.get("rows"),
            "n_columns": card.get("n_columns"),
            "links_to": card.get("neighbours") or [],
            "summary": card.get("summary", "").strip(),
        }
        for sid, usage in usages.items():
            questions[f"s1_{sid}_{tid}"] = {
                "type": "noul",
                "instructions": (
                    f"Can table {card['name']} fill slot #{sid}, which is {usage}?"),
            }
    state = {
        "task": "table_slot_binding",
        "instruction": (
            "Every TABLE slot of the template below must be filled by one database "
            "table. Each candidate is a table card (row count, column count, links and "
            "a one-line summary; column lists are omitted). Answer, per slot and per "
            "candidate, whether that table could fill that slot. Several tables may "
            "qualify for the same slot; answer only the provided questions."),
        "context": {"database": db_id, "question": question,
                    "evidence": evidence or "",
                    "template": _template_json(template)},
        "slots": [{"slot_id": f"s{sid}", "role": "TABLE", "usage": usages[sid]}
                  for sid in usages],
        "candidates": candidates,
        "output_schema": {"noul": "probability in [0, 1]"},
    }
    answer = jev_call(state, questions, cfg)

    result = {"ok": True, "slots": {}, "num_questions": len(questions),
              "usage_tokens": answer.get("usage")}
    for sid in usages:
        positives = []
        for index, card in enumerate(cards, start=1):
            noul = round(_noul(answer["answers"].get(f"s1_{sid}_t{index}")), 4)
            if noul >= threshold:
                positives.append({"table": card["name"], "noul": noul})
        positives.sort(key=lambda row: (-row["noul"], row["table"]))
        result["slots"][sid] = {"role": "TABLE", "usage": usages[sid],
                                "positives": positives}
    result["selected"] = _select_tables(result["slots"])
    return result


def _select_tables(slots: dict) -> dict[int, str]:
    """One table per slot: prefer unused tables, fall back to the best candidate."""
    selected: dict[int, str] = {}
    used: set[str] = set()
    first_positive: str | None = None
    for sid in sorted(slots):
        positives = slots[sid]["positives"]
        if positives and first_positive is None:
            first_positive = positives[0]["table"]
        pick = next((row["table"] for row in positives if row["table"] not in used), None)
        if pick is None and positives:
            pick = positives[0]["table"]
        if pick is None:
            pick = first_positive
        if pick is not None:
            selected[sid] = pick
            used.add(pick)
    return selected


# ---------------------------------------------------------------------------
# stage 2: column slots - one call per table, every (slot, column) question
# ---------------------------------------------------------------------------
def bind_column_slots(template: dict, table_bindings: dict, schema_db: dict,
                      cards: list[dict], db_id: str, question: str,
                      evidence: str | None, cfg: dict,
                      values: dict | None = None) -> dict:
    """Bind every column slot, table by table; positives kept as candidates."""
    threshold = float((cfg.get("fill") or {}).get("noul_threshold", 0.5))
    slots = [s for s in collect_slots(template, []) if s["kind"] == "column"]
    if not slots:
        return {"ok": True, "slots": {}, "selected": {}, "num_questions": 0}

    context = slot_context(template)
    scopes = column_slot_scopes(template, table_bindings)
    all_tables = list(dict.fromkeys(table_bindings.values()))
    table_map = {t["name"]: t for t in schema_db.get("tables") or []}
    cards_by_name = {c["name"]: c for c in cards}

    slot_usage_text = {s["sid"]: column_slot_usage({**context.get(s["sid"], {}),
                                                    "role": s.get("role")}, values)
                       for s in slots}
    slots_by_table: dict[str, list[dict]] = {}
    for slot in slots:
        for table_name in (scopes.get(slot["sid"]) or all_tables):
            slots_by_table.setdefault(table_name, []).append(slot)

    result = {"ok": True, "slots": {s["sid"]: {"role": s.get("role"),
                                               "usage": slot_usage_text[s["sid"]],
                                               "positives": [], "best": None}
                                    for s in slots},
              "num_questions": 0, "calls": []}
    for table_name in list(dict.fromkeys(table_bindings.values())):
        table = table_map.get(table_name)
        table_slots = slots_by_table.get(table_name) or []
        if not table or not table_slots:
            continue
        keys = set(table.get("primary_key") or [])
        columns = [{"cid": f"{table_name}_c{i}", "column": column["name"],
                    "dtype": column.get("type"), "sample": column.get("sample"),
                    "is_key": column["name"] in keys}
                   for i, column in enumerate(table.get("columns") or [], start=1)]
        if not columns:
            continue
        questions = {
            f"s2_{table_name}_{slot['sid']}_{col['cid']}": {
                "type": "noul",
                "instructions": (
                    f"Can column {table_name}.{col['column']} ({col['dtype']}, "
                    f"e.g. {str(col['sample'])[:40]}) fill slot #{slot['sid']}, "
                    f"which is {slot_usage_text[slot['sid']]}?"),
            }
            for slot in table_slots for col in columns
        }
        state = {
            "task": "column_slot_binding",
            "instruction": (
                "The template below still has column slots to fill. Only the columns of "
                "the focus table are candidates in this request. Answer, per slot and "
                "per column, whether that column could fill that slot. Several columns "
                "may qualify for the same slot; answer only the provided questions."),
            "context": {"database": db_id, "question": question,
                        "evidence": evidence or "",
                        "template": _template_json(template),
                        "table_summary": cards_by_name.get(table_name, {}).get("summary", "")},
            "focus_table": {"table_name": table_name,
                            "primary_key": sorted(keys)},
            "slots": [{"slot_id": f"s{slot['sid']}", "role": slot.get("role"),
                       "usage": slot_usage_text[slot["sid"]]} for slot in table_slots],
            "candidates": {
                col["cid"]: {"table_name": table_name, "column_name": col["column"],
                             "dtype": col["dtype"], "sample": col["sample"],
                             "primary_key": col["is_key"]}
                for col in columns},
            "output_schema": {"noul": "probability in [0, 1]"},
        }
        answer = jev_call(state, questions, cfg)
        result["num_questions"] += len(questions)
        result["calls"].append({"table": table_name, "num_questions": len(questions),
                                "usage": answer.get("usage")})
        for slot in table_slots:
            sid = slot["sid"]
            for col in columns:
                noul = round(_noul(answer["answers"].get(
                    f"s2_{table_name}_{sid}_{col['cid']}")), 4)
                row = {"table": table_name, "column": col["column"],
                       "dtype": col["dtype"], "noul": noul}
                if noul >= threshold:
                    result["slots"][sid]["positives"].append(dict(row))
                best = result["slots"][sid]["best"]
                if best is None or noul > best["noul"]:
                    result["slots"][sid]["best"] = dict(row)
    for sid in result["slots"]:
        result["slots"][sid]["positives"].sort(
            key=lambda row: (-row["noul"], row["table"], row["column"]))
    result["selected"] = _select_columns(result["slots"], context)
    return result


def _select_columns(slots: dict, context: dict) -> dict[int, dict]:
    """One column per slot; prefer unused, type-compatible, highest-noul candidates."""
    selected: dict[int, dict] = {}
    used: set[tuple[str, str]] = set()
    for sid in sorted(slots):
        positives = slots[sid]["positives"]
        fallback = False
        if not positives and slots[sid].get("best"):
            positives = [slots[sid]["best"]]      # nothing passed the threshold
            fallback = True
        if not positives:
            continue
        ctx = context.get(sid, {})
        agg = (ctx.get("agg") or "").upper()
        wants_numeric = agg in _AGG_NUMERIC or bool(ctx.get("arith"))

        def rank(row):
            numeric_miss = wants_numeric and str(row["dtype"]).lower() not in _NUMERIC
            return (numeric_miss, (row["table"], row["column"]) in used, -row["noul"])

        pick = sorted(positives, key=rank)[0]
        selected[sid] = {"table": pick["table"], "column": pick["column"],
                         "noul": pick["noul"], "fallback": fallback}
        used.add((pick["table"], pick["column"]))
    return selected


# ---------------------------------------------------------------------------
# binding everything into a renderable AST
# ---------------------------------------------------------------------------
def _value_node(value):
    if isinstance(value, bool):
        return {"type": "Literal", "kind": "bool", "value": value}
    if isinstance(value, (int, float)):
        return {"type": "Literal", "kind": "number", "value": value}
    return {"type": "Literal", "kind": "string", "value": str(value)}


def fill(template: dict, values: dict, table_bindings: dict, column_bindings: dict
         ) -> tuple[dict, list[int]]:
    """Return ``(filled_ast, unresolved_sids)``; the input template is untouched.

    Aliases are tracked per query block: a column is only qualified with an alias
    visible in its own scope (or an enclosing one, for correlated subqueries).
    """
    work = copy.deepcopy(template)
    unresolved: list[int] = []
    alias_by_node: dict[int, str] = {}
    counter = [0]

    def bind_scope(from_items, scope: dict) -> None:
        used = set(scope.values())
        for table_node in _iter_from(from_items):
            name = table_node.get("name")
            if not (isinstance(name, dict) and name.get("type") == "Slot"):
                continue
            if id(table_node) not in alias_by_node:
                alias = table_node.get("alias")
                if not alias or alias in used:
                    # a duplicate alias in one FROM list makes the SQL ambiguous
                    counter[0] += 1
                    alias = f"t{counter[0]}"
                    while alias in used:
                        counter[0] += 1
                        alias = f"t{counter[0]}"
                table_node["alias"] = alias
                alias_by_node[id(table_node)] = alias
            used.add(alias_by_node[id(table_node)])
            table = table_bindings.get(name["sid"])
            if table:
                scope.setdefault(table, alias_by_node[id(table_node)])

    def fill_slot(node: dict, scopes: list[dict]) -> None:
        sid, kind = node["sid"], node["kind"]
        if kind == "table":
            table = table_bindings.get(sid)
            if table is None:
                unresolved.append(sid)
                return
            node.clear()
            node.update({"parts": [table]})       # restores Table.name in place
            return
        if kind == "column":
            bound = column_bindings.get(sid)
            if bound is None:
                unresolved.append(sid)
                return
            alias = None
            for scope in reversed(scopes):
                if bound["table"] in scope:
                    alias = scope[bound["table"]]
                    break
            if alias is None:
                unresolved.append(sid)
                return
            node.clear()
            node.update({"type": "Column", "parts": [alias, bound["column"]]})
            return
        if kind == "value":
            if str(sid) not in values:
                unresolved.append(sid)
                return
            node.clear()
            node.update(_value_node(values[str(sid)]))
            return
        unresolved.append(sid)

    def walk(node, scopes):
        if isinstance(node, dict):
            if node.get("type") == "Slot":
                fill_slot(node, scopes)
                return
            if node.get("type") == "Query":
                scope: dict = {}
                body = node.get("body")
                if isinstance(body, dict):
                    if body.get("type") == "Select":
                        bind_scope(body.get("from") or [], scope)
                    elif body.get("type") == "SetOp":
                        for side in (body.get("l"), body.get("r")):
                            if isinstance(side, dict) and side.get("type") == "Select":
                                bind_scope(side.get("from") or [], scope)
                inner = scopes + [scope]
                for cte in node.get("with") or []:
                    walk(cte, scopes)
                walk(body, inner)
                for item in node.get("order_by") or []:
                    walk(item, inner)
                walk(node.get("limit"), inner)
                walk(node.get("offset"), inner)
                return
            for value in node.values():
                walk(value, scopes)
        elif isinstance(node, list):
            for value in node:
                walk(value, scopes)

    walk(work, [])
    return work, unresolved
