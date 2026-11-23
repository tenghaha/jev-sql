"""Hand-written SQL parser: recursive descent over a context-free grammar, with
Pratt (precedence-climbing) parsing for expressions.

Design
------
* **Statements / clauses are parsed by recursive descent**, one method per
  production, mirroring ``docs/grammar.ebnf``.  The method names are the rule
  ids reported in ``self.rules`` so that a rule table with real hit counts can
  be generated from any corpus.
* **Operators are parsed by Pratt parsing**: ``parse_expr(min_bp)`` consumes
  infix/postfix operators while their left binding power is ``>= min_bp`` and
  recurses on the right with ``bp + 1`` for left associativity.  All binding
  powers live in :data:`BINDING_POWER`, i.e. precedence is data, not control
  flow.
* **Dialect differences are data**: the lexer handles quoting/escaping; this
  parser only branches where grammar differs (``LIMIT a, b`` for
  sqlite/mysql, ``DISTINCT ON`` for postgres, ``QUALIFY`` for
  bigquery/snowflake, ``UNNEST``/``* EXCEPT`` for bigquery, ``::`` casts for
  postgres/snowflake).

The AST is deliberately *syntactic*: it records what the statement says, not
what it means.  Semantic annotations are a separate stage (``slots.py``).
"""

from __future__ import annotations

from .lexer import Tok, tokenize

__all__ = ["ParseError", "Unsupported", "Parser", "parse_sql", "BINDING_POWER"]


class ParseError(Exception):
    def __init__(self, msg: str, tok: Tok):
        self.msg = msg
        self.tok = tok
        super().__init__(f"{msg} at offset {tok.start} near {tok.text!r}")


class Unsupported(ParseError):
    """Raised for constructs that are lexically valid but outside the grammar."""


# ---------------------------------------------------------------------------
# Operator table (Pratt binding powers)
# ---------------------------------------------------------------------------
# key -> (left binding power, right binding power).  Left associativity is
# encoded as rbp = lbp + 1.  Prefix operators get their own entry in
# ``PREFIX_BP``.  Comparison-family operators all share lbp=6 so that
# ``a = b`` and ``a IS NULL`` sit on the same level, below ``AND``.
BINDING_POWER: dict[str, tuple[int, int]] = {
    "OR": (1, 2),
    "AND": (3, 4),
    # 5 is reserved for prefix NOT: NOT binds looser than comparisons
    "=": (6, 7), "==": (6, 7), "!=": (6, 7), "<>": (6, 7),
    "<": (6, 7), "<=": (6, 7), ">": (6, 7), ">=": (6, 7), "<=>": (6, 7),
    "IS": (6, 7), "IN": (6, 7), "BETWEEN": (6, 7), "LIKE": (6, 7),
    "ILIKE": (6, 7), "RLIKE": (6, 7), "REGEXP": (6, 7), "GLOB": (6, 7),
    "|": (8, 9), "&": (9, 10), "^": (9, 10),
    "||": (10, 11),
    "+": (12, 13), "-": (12, 13),
    "*": (14, 15), "/": (14, 15), "%": (14, 15),
    "DIV": (14, 15), "MOD": (14, 15),
}

PREFIX_BP = {"NOT": 5, "-": 15, "+": 15, "~": 15}
POSTFIX_BP = {"::": 16, "[": 17}

COMPARISON_OPS = {"=", "==", "!=", "<>", "<", "<=", ">", ">=", "<=>"}
PATTERN_OPS = {"LIKE", "ILIKE", "RLIKE", "REGEXP", "GLOB", "SIMILAR"}
WORD_BINARY_OPS = {"AND", "OR", "DIV", "MOD", "XOR"}

# Words that can never start or continue a bare (AS-less) alias.
RESERVED = {
    "ALL", "AND", "ANY", "ARRAY", "AS", "ASC", "BETWEEN", "BY", "CASE",
    "CAST", "COLLATE", "CROSS", "CUBE", "CURRENT", "DEFAULT", "DESC", "DISTINCT",
    "ELSE", "ESCAPE", "EXCEPT", "EXCLUDE", "EXISTS", "FETCH", "FILTER",
    "FIRST", "FOLLOWING", "FOR", "FROM", "FULL", "GROUP", "GROUPING", "HAVING",
    "IGNORE", "ILIKE", "IN", "INNER", "INTERSECT", "INTERVAL", "INTO", "IS",
    "JOIN", "LAST", "LATERAL", "LEFT", "LIKE", "LIMIT", "MATERIALIZED",
    "NATURAL", "NOT", "NULLS", "OFFSET", "ON", "OR", "ORDER", "OUTER", "OVER",
    "PARTITION", "PRECEDING", "QUALIFY", "RANGE", "REGEXP", "RESPECT",
    "RIGHT", "ROLLUP", "ROW", "ROWS", "SELECT", "SEPARATOR", "SET", "SOME",
    "STRUCT", "TABLESAMPLE", "THEN", "TIES", "TO", "UNBOUNDED", "UNION",
    "UNNEST", "USING", "VALUES", "WHEN", "WHERE", "WINDOW", "WITH",
    "WITHIN", "OFFSET",
}

# Aggregate function names: recorded on the node as ``is_aggregate`` because it
# is a lexical fact about the called function, not a semantic guess.
AGGREGATE_FUNCS = {
    "COUNT", "SUM", "AVG", "MIN", "MAX", "TOTAL", "GROUP_CONCAT", "STRING_AGG",
    "ARRAY_AGG", "LISTAGG", "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE",
    "VAR_POP", "VAR_SAMP", "MEDIAN", "ANY_VALUE", "APPROX_COUNT_DISTINCT",
    "APPROX_QUANTILES", "LOGICAL_AND", "LOGICAL_OR", "BOOL_AND", "BOOL_OR",
    "CORR", "COVAR_POP", "COVAR_SAMP", "PERCENTILE_CONT", "PERCENTILE_DISC",
    "ARRAY_CONCAT_AGG", "JSON_AGG", "JSONB_AGG", "COUNT_IF",
}

# Words that can never be read as an identifier (column name or function name).
# Kept deliberately small: ``OFFSET``, ``FILTER``, ``ROLLUP``, ``NULLS``,
# ``PARTITION`` etc. are real column names in BIRD / Spider2 schemas.
NON_NAME_WORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "ORDER", "HAVING", "LIMIT",
    "UNION", "INTERSECT", "EXCEPT", "JOIN", "ON", "USING", "AND", "OR", "NOT",
    "IN", "IS", "BETWEEN", "LIKE", "ILIKE", "THEN", "WHEN", "AS", "BY",
    "ASC", "DESC", "DISTINCT", "QUALIFY", "WINDOW", "WITH", "LATERAL",
    "VALUES", "SET", "DECLARE", "INTO", "FOR", "CASE",
}

# Clause starts that can follow a select list; used only to tolerate the
# malformed trailing comma seen in a handful of gold queries (``SELECT a, FROM t``).
TRAILING_COMMA_STOP = {"FROM", "WHERE", "GROUP", "ORDER", "HAVING", "LIMIT",
                       "QUALIFY", "WINDOW", "UNION", "INTERSECT", "EXCEPT"}

DIALECT_LIMIT_COMMA = {"sqlite", "mysql"}
DIALECT_QUALIFY = {"bigquery", "snowflake"}


class Parser:
    """One parser instance per SQL statement (state is a token cursor)."""

    def __init__(self, sql: str, dialect: str = "sqlite", spans: bool = False):
        self.sql = sql
        self.dialect = dialect
        self.spans = spans
        self.toks = tokenize(sql, dialect)
        self.i = 0
        self.rules: dict[str, int] = {}

    # -- rule-hit accounting -------------------------------------------------
    def hit(self, rule: str, n: int = 1) -> None:
        self.rules[rule] = self.rules.get(rule, 0) + n

    # -- token helpers -------------------------------------------------------
    def peek(self, k: int = 0) -> Tok:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def at_eof(self) -> bool:
        return self.peek().kind == "eof"

    def at_word(self, *words: str) -> bool:
        t = self.peek()
        return t.kind == "name" and t.upper in words

    def at_sym(self, *syms: str) -> bool:
        t = self.peek()
        return t.kind == "sym" and t.text in syms

    def accept_word(self, *words: str) -> Tok | None:
        if self.at_word(*words):
            t = self.peek()
            self.i += 1
            return t
        return None

    def accept_sym(self, *syms: str) -> Tok | None:
        if self.at_sym(*syms):
            t = self.peek()
            self.i += 1
            return t
        return None

    def expect_word(self, word: str) -> Tok:
        t = self.accept_word(word)
        if t is None:
            raise ParseError(f"expected {word}", self.peek())
        return t

    def expect_sym(self, sym: str) -> Tok:
        t = self.accept_sym(sym)
        if t is None:
            raise ParseError(f"expected {sym!r}", self.peek())
        return t

    def span(self, start_tok: Tok) -> dict:
        if not self.spans:
            return {}
        return {"span": [start_tok.start, self.toks[self.i - 1].end if self.i else start_tok.end]}

    # -- entry point ---------------------------------------------------------
    def parse_script(self) -> dict:
        stmts = []
        while not self.at_eof():
            if self.accept_sym(";"):
                continue
            stmts.append(self.parse_statement())
            if not self.at_eof() and not self.accept_sym(";"):
                raise ParseError("expected ';' between statements", self.peek())
        if not stmts:
            raise ParseError("empty statement", self.peek())
        return {"type": "Script", "stmts": stmts}

    def parse_statement(self) -> dict:
        if self.at_word("DECLARE"):
            return self.parse_declare()
        if self.at_word("CREATE"):
            return self.parse_create()
        if self.at_word("SET") and self.peek(1).kind in ("name", "qname", "param"):
            return self.parse_set_var()
        return self.parse_query()

    # -- statements ----------------------------------------------------------
    def parse_declare(self) -> dict:
        start = self.expect_word("DECLARE")
        name = self.read_name()
        data_type = None
        if not self.at_word("DEFAULT") and not self.at_sym(";", ")", ","):
            if not self.at_eof():
                data_type = self.parse_type()
        default = None
        if self.accept_word("DEFAULT"):
            default = self.parse_expr()
        self.hit("stmt.declare")
        node = {"type": "Declare", "name": name, "data_type": data_type, "default": default}
        node.update(self.span(start))
        return node

    def parse_create(self) -> dict:
        """``CREATE [OR REPLACE] [TEMP|TEMPORARY] FUNCTION name(args) ... AS (...)``.

        BigQuery gold scripts declare UDFs before the main query; the body is
        kept as an expression so that the enclosing script stays parseable.
        Other DDL is rejected rather than silently skipped.
        """
        start = self.expect_word("CREATE")
        if self.accept_word("OR"):
            self.expect_word("REPLACE")
        self.accept_word("TEMP") or self.accept_word("TEMPORARY")
        if not self.at_word("FUNCTION"):
            raise Unsupported("only CREATE FUNCTION is supported", self.peek())
        self.i += 1
        name = ".".join(self.read_name_parts() or [self.read_name()])
        self.expect_sym("(")
        params = []
        if not self.at_sym(")"):
            while True:
                pname = self.read_name()
                ptype = self.parse_type()
                params.append({"name": pname, "type": ptype})
                if not self.accept_sym(","):
                    break
        self.expect_sym(")")
        returns = None
        if self.accept_word("RETURNS"):
            returns = self.parse_type()
        body = None
        if self.accept_word("AS"):
            self.expect_sym("(")
            body = self.parse_expr()
            self.expect_sym(")")
        self.hit("stmt.create_function")
        node = {"type": "CreateFunction", "name": name, "params": params,
                "returns": returns, "body": body}
        node.update(self.span(start))
        return node

    def parse_set_var(self) -> dict:
        start = self.expect_word("SET")
        t = self.peek()
        if t.kind in ("name", "qname"):
            self.i += 1
            name = t.value or t.text
        elif t.kind == "param":
            self.i += 1
            name = t.text
        else:
            raise ParseError("expected variable name after SET", t)
        self.expect_sym("=")
        value = self.parse_expr()
        self.hit("stmt.set")
        node = {"type": "SetVar", "name": name, "value": value}
        node.update(self.span(start))
        return node

    # -- query ---------------------------------------------------------------
    def parse_query(self) -> dict:
        start = self.peek()
        with_ = None
        if self.at_word("WITH"):
            with_ = self.parse_with()
        body = self.parse_set_expr()
        order_by = None
        if self.at_word("ORDER"):
            order_by = self.parse_order_by()
        limit, offset = self.parse_limit_offset()
        node = {"type": "Query", "with": with_, "body": body,
                "order_by": order_by, "limit": limit, "offset": offset}
        node.update(self.span(start))
        self.hit("query.query")
        return node

    def parse_with(self) -> list[dict]:
        self.expect_word("WITH")
        self.accept_word("RECURSIVE")
        ctes = [self.parse_cte()]
        while self.accept_sym(","):
            ctes.append(self.parse_cte())
        self.hit("query.with", len(ctes))
        return ctes

    def parse_cte(self) -> dict:
        start = self.peek()
        name = self.read_name()
        columns: list[str] = []
        if self.at_sym("("):
            save = self.i
            try:
                self.i += 1
                columns = [self.read_name()]
                while self.accept_sym(","):
                    columns.append(self.read_name())
                self.expect_sym(")")
                if not self.at_word("AS"):
                    raise ParseError("expected AS after CTE column list", self.peek())
            except ParseError:
                self.i = save
                columns = []
        self.expect_word("AS")
        if self.accept_word("NOT"):
            self.expect_word("MATERIALIZED")
        else:
            self.accept_word("MATERIALIZED")
        self.expect_sym("(")
        body = self.parse_query()
        self.expect_sym(")")
        node = {"type": "CTE", "name": name, "columns": columns or None, "body": body}
        node.update(self.span(start))
        self.hit("query.cte")
        return node

    def parse_set_expr(self) -> dict:
        left = self.parse_select_core()
        while self.at_word("UNION", "INTERSECT", "EXCEPT"):
            op = self.peek().upper
            self.i += 1
            all_ = False
            if self.accept_word("ALL"):
                all_ = True
            else:
                self.accept_word("DISTINCT")
            right = self.parse_select_core()
            left = {"type": "SetOp", "op": op, "all": all_, "l": left, "r": right}
            self.hit("query.setop")
        return left

    def parse_select_core(self) -> dict:
        if self.at_sym("("):
            start = self.peek()
            self.i += 1
            inner = self.parse_query()
            self.expect_sym(")")
            node = {"type": "ParenQuery", "query": inner}
            node.update(self.span(start))
            self.hit("query.paren")
            return node
        if self.at_word("VALUES"):
            return self.parse_values()
        if not self.at_word("SELECT"):
            raise ParseError("expected SELECT / VALUES / '('", self.peek())
        return self.parse_select()

    def parse_select(self) -> dict:
        start = self.expect_word("SELECT")
        distinct = None
        if self.accept_word("DISTINCT"):
            distinct = {"kind": "distinct", "exprs": None}
            if self.at_word("ON"):
                self.i += 1
                self.expect_sym("(")
                exprs = [self.parse_expr()]
                while self.accept_sym(","):
                    exprs.append(self.parse_expr())
                self.expect_sym(")")
                distinct = {"kind": "on", "exprs": exprs}
                self.hit("select.distinct_on")
            else:
                self.hit("select.distinct")
        else:
            self.accept_word("ALL")
        as_mod = None
        if self.at_word("AS") and self.peek(1).kind == "name" and self.peek(1).upper in ("STRUCT", "VALUE"):
            self.i += 1
            as_mod = self.peek().upper
            self.i += 1
            self.hit("select.as_modifier")

        items = [self.parse_select_item()]
        while self.accept_sym(","):
            # tolerate a trailing comma before a clause keyword (seen in gold SQL)
            if self.peek().kind == "name" and self.peek().upper in TRAILING_COMMA_STOP:
                self.hit("select.trailing_comma")
                break
            items.append(self.parse_select_item())

        from_: list[dict] | None = None
        if self.accept_word("FROM"):
            from_ = [self.parse_from_item()]
            while self.accept_sym(","):
                from_.append(self.parse_from_item())
                self.hit("from.comma_list")

        where = None
        if self.accept_word("WHERE"):
            where = self.parse_expr()
            self.hit("select.where")
        group_by = None
        if self.at_word("GROUP"):
            self.i += 1
            self.expect_word("BY")
            self.accept_word("ALL")
            self.accept_word("DISTINCT")
            group_by = [self.parse_expr()]
            while self.accept_sym(","):
                group_by.append(self.parse_expr())
            self.hit("select.group_by")
        having = None
        if self.accept_word("HAVING"):
            having = self.parse_expr()
            self.hit("select.having")
        qualify = None
        if self.dialect in DIALECT_QUALIFY and self.accept_word("QUALIFY"):
            qualify = self.parse_expr()
            self.hit("select.qualify")
        window = None
        if self.at_word("WINDOW"):
            self.i += 1
            window = []
            while True:
                wname = self.read_name()
                self.expect_word("AS")
                self.expect_sym("(")
                spec = self.parse_window_spec_body()
                self.expect_sym(")")
                window.append({"name": wname, "spec": spec})
                if not self.accept_sym(","):
                    break
            self.hit("select.window_clause")

        node = {"type": "Select", "distinct": distinct, "as": as_mod, "items": items,
                "from": from_, "where": where, "group_by": group_by,
                "having": having, "qualify": qualify, "window": window}
        node.update(self.span(start))
        self.hit("stmt.select")
        return node

    def parse_values(self) -> dict:
        start = self.expect_word("VALUES")
        rows = []
        while True:
            if self.at_sym("("):
                self.i += 1
                row = [self.parse_expr()]
                while self.accept_sym(","):
                    row.append(self.parse_expr())
                self.expect_sym(")")
            else:
                row = [self.parse_expr()]
            rows.append(row)
            if not self.accept_sym(","):
                break
        self.hit("query.values")
        node = {"type": "Values", "rows": rows}
        node.update(self.span(start))
        return node

    # -- select list ---------------------------------------------------------
    def parse_select_item(self) -> dict:
        start = self.peek()
        if self.at_sym("*"):
            self.i += 1
            star = self.parse_star_modifiers(None)
            self.hit("select.star")
            node = {"type": "SelectItem", "star": star}
            node.update(self.span(start))
            return node
        # qualified star: name ('.' name)* '.' '*'
        q = self.match_qualified_star()
        if q is not None:
            star = self.parse_star_modifiers(q)
            self.hit("select.qualified_star")
            node = {"type": "SelectItem", "star": star}
            node.update(self.span(start))
            return node
        expr = self.parse_expr()
        alias = self.parse_optional_alias()
        node = {"type": "SelectItem", "expr": expr, "alias": alias}
        node.update(self.span(start))
        if alias:
            self.hit("select.alias")
        return node

    def parse_star_modifiers(self, qualifier: str | None) -> dict:
        star: dict = {"qualifier": qualifier}
        if self.at_word("EXCEPT", "EXCLUDE"):
            kw = self.peek().upper
            self.i += 1
            self.expect_sym("(")
            cols = [self.read_name()]
            while self.accept_sym(","):
                cols.append(self.read_name())
            self.expect_sym(")")
            star["except"] = cols
            self.hit(f"select.star_{kw.lower()}")
        if self.at_word("REPLACE"):
            self.i += 1
            self.expect_sym("(")
            repls = []
            while True:
                e = self.parse_expr()
                self.expect_word("AS")
                repls.append({"expr": e, "name": self.read_name()})
                if not self.accept_sym(","):
                    break
            self.expect_sym(")")
            star["replace"] = repls
            self.hit("select.star_replace")
        return star

    def match_qualified_star(self) -> str | None:
        """Detect ``q.x.*`` / ``q.*`` without consuming tokens if absent."""
        save = self.i
        parts = []
        while True:
            t = self.peek()
            if t.kind not in ("name", "qname"):
                break
            if t.kind == "name" and t.upper in RESERVED:
                break
            parts.append(t.value or t.text)
            self.i += 1
            if not self.at_sym("."):
                break
            if self.peek(1).kind == "sym" and self.peek(1).text == "*":
                self.i += 1  # consume '.'
                self.i += 1  # consume '*'
                return ".".join(parts)
            self.i += 1  # consume '.'
        self.i = save
        return None

    def parse_optional_alias(self) -> str | None:
        if self.accept_word("AS"):
            t = self.peek()
            if t.kind in ("name", "qname"):
                self.i += 1
                return t.value or t.text
            if t.kind == "string":  # BigQuery allows quoted aliases
                self.i += 1
                return t.value
            raise ParseError("expected alias after AS", t)
        t = self.peek()
        if t.kind == "qname":
            self.i += 1
            return t.value or t.text
        if t.kind == "name" and t.upper not in RESERVED:
            self.i += 1
            return t.value or t.text
        return None

    # -- FROM ---------------------------------------------------------------
    def parse_from_item(self) -> dict:
        left = self.parse_table_factor()
        while self.peek_join_kind():
            left = self.parse_join(left)
        return left

    def peek_join_kind(self) -> bool:
        t = self.peek()
        if t.kind != "name":
            return False
        return t.upper in ("JOIN", "STRAIGHT_JOIN", "INNER", "CROSS", "NATURAL",
                           "LEFT", "RIGHT", "FULL")

    def parse_join(self, left: dict) -> dict:
        start = self.peek()
        words: list[str] = []
        straight = False
        while self.peek().kind == "name" and self.peek().upper in (
                "NATURAL", "INNER", "CROSS", "LEFT", "RIGHT", "FULL", "OUTER"):
            words.append(self.peek().upper)
            self.i += 1
        if self.accept_word("STRAIGHT_JOIN"):
            straight = True
        else:
            self.expect_word("JOIN")
        kind = next((w for w in words if w in ("INNER", "CROSS", "LEFT", "RIGHT", "FULL")), "INNER")
        natural = "NATURAL" in words
        right = self.parse_table_factor()
        on = using = None
        if self.accept_word("ON"):
            on = self.parse_expr()
        elif self.accept_word("USING"):
            self.expect_sym("(")
            using = [self.read_name()]
            while self.accept_sym(","):
                using.append(self.read_name())
            self.expect_sym(")")
            self.hit("from.join.using")
        self.hit(f"from.join.{kind.lower()}")
        if natural:
            self.hit("from.join.natural")
        if straight:
            self.hit("from.join.straight")
        node = {"type": "Join", "kind": kind, "natural": natural or None,
                "left": left, "right": right, "on": on, "using": using}
        node.update(self.span(start))
        return node

    def parse_table_factor(self) -> dict:
        start = self.peek()
        lateral = bool(self.accept_word("LATERAL"))

        if self.at_sym("("):
            self.i += 1
            if self.at_word("SELECT", "WITH", "VALUES"):
                inner = self.parse_query()
                self.expect_sym(")")
                alias, columns = self.parse_alias_with_columns()
                self.hit("from.derived")
                node = {"type": "DerivedTable", "query": inner, "alias": alias,
                        "columns": columns, "lateral": lateral or None}
                node.update(self.span(start))
                return node
            inner = self.parse_from_item()
            self.expect_sym(")")
            self.hit("from.parenthesized_join")
            return inner

        if self.at_word("UNNEST"):
            self.i += 1
            self.expect_sym("(")
            expr = self.parse_expr()
            self.expect_sym(")")
            with_offset, offset_alias = self.parse_with_offset()
            alias = self.parse_optional_alias()
            # BigQuery also allows ``UNNEST(x) AS t WITH OFFSET AS off``
            w2, o2 = self.parse_with_offset()
            self.hit("from.unnest")
            node = {"type": "Unnest", "expr": expr, "alias": alias,
                    "with_offset": (with_offset or w2) or None,
                    "offset_alias": offset_alias or o2}
            node.update(self.span(start))
            return node

        if self.at_word("TABLE") and self.peek(1).kind == "sym" and self.peek(1).text == "(":
            self.i += 1
            self.expect_sym("(")
            expr = self.parse_expr()
            self.expect_sym(")")
            alias = self.parse_optional_alias()
            self.hit("from.table_function")
            node = {"type": "TableFunction", "expr": expr, "alias": alias}
            node.update(self.span(start))
            return node

        parts = self.read_name_parts(allow_dash=True)
        if not parts:
            raise ParseError("expected table reference", self.peek())
        name = {"parts": parts, "raw": ".".join(parts)}
        if self.at_sym("("):
            self.i += 1
            args, _ = self.parse_call_args_inner(".".join(parts).upper())
            alias = self.parse_optional_alias()
            self.hit("from.table_function")
            node = {"type": "TableFunction",
                    "func": {"name": ".".join(parts), "args": args}, "alias": alias}
            node.update(self.span(start))
            return node
        alias = self.parse_optional_alias()
        self.hit("from.table")
        node = {"type": "Table", "name": name, "alias": alias}
        node.update(self.span(start))
        return node

    # -- order / limit -------------------------------------------------------
    def parse_order_by(self) -> list[dict]:
        self.expect_word("ORDER")
        self.expect_word("BY")
        items = []
        while True:
            start = self.peek()
            expr = self.parse_expr()
            desc = False
            if self.accept_word("DESC"):
                desc = True
            else:
                self.accept_word("ASC")
            nulls = None
            if self.accept_word("NULLS"):
                if self.accept_word("FIRST"):
                    nulls = "FIRST"
                elif self.accept_word("LAST"):
                    nulls = "LAST"
                else:
                    raise ParseError("expected FIRST/LAST after NULLS", self.peek())
            item = {"type": "OrderItem", "expr": expr, "desc": desc, "nulls": nulls}
            item.update(self.span(start))
            items.append(item)
            if not self.accept_sym(","):
                break
        self.hit("query.order_by")
        return items

    def parse_limit_offset(self) -> tuple[dict | None, dict | None]:
        limit = offset = None
        while True:
            if self.at_word("LIMIT"):
                self.i += 1
                if self.accept_word("ALL"):
                    limit = {"type": "Limit", "expr": None, "all": True}
                    continue
                expr = self.parse_expr()
                if self.dialect in DIALECT_LIMIT_COMMA and self.at_sym(","):
                    self.i += 1
                    offset = {"type": "Offset", "expr": expr}
                    expr = self.parse_expr()
                limit = {"type": "Limit", "expr": expr}
                self.hit("query.limit")
                continue
            if self.at_word("OFFSET"):
                self.i += 1
                offset = {"type": "Offset", "expr": self.parse_expr()}
                self.accept_word("ROWS") or self.accept_word("ROW")
                self.hit("query.offset")
                continue
            if self.at_word("FETCH"):
                self.i += 1
                self.accept_word("FIRST") or self.accept_word("NEXT")
                expr = None if self.at_word("ROW", "ROWS") else self.parse_expr()
                self.accept_word("ROW") or self.accept_word("ROWS")
                self.accept_word("ONLY") or self.accept_word("WITH")
                self.accept_word("TIES")
                limit = {"type": "Limit", "expr": expr}
                self.hit("query.fetch")
                continue
            break
        return limit, offset

    # ------------------------------------------------------------------
    # Expressions: Pratt / precedence climbing
    # ------------------------------------------------------------------
    def parse_expr(self, min_bp: int = 0) -> dict:
        left = self.parse_prefix()
        while True:
            t = self.peek()
            if t.kind == "eof":
                break
            # ---- postfix operators ------------------------------------
            if t.kind == "name":
                # sqlite/mysql postfix null tests: ``x NOTNULL`` / ``x ISNULL``
                if t.upper == "NOTNULL" and POSTFIX_BP["["] >= min_bp:
                    self.i += 1
                    left = {"type": "IsTest", "e": left, "test": "NULL", "negated": True}
                    self.hit("expr.notnull")
                    continue
                if t.upper == "ISNULL" and POSTFIX_BP["["] >= min_bp:
                    self.i += 1
                    left = {"type": "IsTest", "e": left, "test": "NULL"}
                    self.hit("expr.isnull")
                    continue
            if t.kind == "sym" and t.text == "::":
                if POSTFIX_BP["::"] < min_bp:
                    break
                self.i += 1
                data_type = self.parse_type()
                left = {"type": "Cast", "e": left, "to": data_type, "style": "colon"}
                self.hit("expr.colon_cast")
                continue
            if t.kind == "sym" and t.text == "[":
                if POSTFIX_BP["["] < min_bp:
                    break
                start = t
                self.i += 1
                idx = self.parse_expr()
                self.expect_sym("]")
                left = {"type": "Index", "e": left, "index": idx}
                left.update(self.span(start))
                self.hit("expr.index")
                continue
            # ---- null-ordering modifier: ``x IGNORE NULLS`` -------------
            if t.kind == "name" and t.upper in ("IGNORE", "RESPECT") and \
                    self.peek(1).kind == "name" and self.peek(1).upper == "NULLS":
                self.i += 2
                left = {"type": "NullsModifier", "e": left, "kind": t.upper}
                self.hit("expr.nulls_modifier")
                continue
            # ---- field access on an arbitrary expression --------------
            if t.kind == "sym" and t.text == "." and self.peek(1).kind in (
                    "name", "qname", "string"):
                if POSTFIX_BP["["] < min_bp:
                    break
                self.i += 1
                nt = self.peek()
                self.i += 1
                left = {"type": "FieldAccess", "e": left,
                        "field": nt.value or nt.text}
                self.hit("expr.field_access")
                continue
            # ---- snowflake semi-structured path: ``col:field`` --------
            if t.kind == "sym" and t.text == ":" and self.peek(1).kind in (
                    "name", "qname", "string"):
                if POSTFIX_BP["["] < min_bp:
                    break
                self.i += 1
                nt = self.peek()
                self.i += 1
                left = {"type": "JsonPath", "e": left, "path": nt.value or nt.text}
                self.hit("expr.json_path")
                continue
            # ---- comparison family ------------------------------------
            bp = self.peek_infix_bp()
            if bp is None:
                break
            lbp, rbp = bp
            if lbp < min_bp:
                break
            left = self.parse_infix(left, lbp, rbp)
        return left

    def peek_infix_bp(self) -> tuple[int, int] | None:
        t = self.peek()
        if t.kind == "sym":
            if t.text in BINDING_POWER:
                return BINDING_POWER[t.text]
            if t.text == "->" or t.text == "->>":  # json access, postgres
                return (18, 19)
            return None
        if t.kind != "name":
            return None
        u = t.upper
        if u in BINDING_POWER:
            return BINDING_POWER[u]
        if u == "NOT" and self.peek(1).kind == "name" and self.peek(1).upper in (
                "IN", "BETWEEN", "LIKE", "ILIKE", "RLIKE", "REGEXP", "GLOB", "SIMILAR"):
            return BINDING_POWER[{"IN": "IN", "BETWEEN": "BETWEEN"}.get(self.peek(1).upper, "LIKE")]
        return None

    def parse_infix(self, left: dict, lbp: int, rbp: int) -> dict:
        t = self.peek()
        u = t.upper
        negated = False
        if u == "NOT":
            self.i += 1
            negated = True
            t = self.peek()
            u = t.upper
        self.i += 1

        if u in ("IS",):
            return self.parse_is(left, negated)
        if u == "IN":
            node = self.parse_in(left, negated)
            return node
        if u == "BETWEEN":
            lo = self.parse_expr(rbp)
            self.expect_word("AND")
            hi = self.parse_expr(rbp)
            self.hit("expr.between")
            return {"type": "Between", "e": left, "lo": lo, "hi": hi,
                    "negated": negated or None}
        if u in PATTERN_OPS:
            pattern = self.parse_expr(rbp)
            escape = self.parse_expr(rbp) if self.accept_word("ESCAPE") else None
            self.hit("expr.like")
            return {"type": "Like", "e": left, "op": u, "pattern": pattern,
                    "escape": escape, "negated": negated or None}
        if u in ("AND", "OR"):
            right = self.parse_expr(rbp)
            self.hit(f"expr.bool.{u.lower()}")
            return {"type": "Logical", "op": u, "l": left, "r": right}
        right = self.parse_expr(rbp)
        self.hit("expr.binary")
        return {"type": "BinaryOp", "op": u if t.kind == "name" else t.text,
                "l": left, "r": right}

    def parse_is(self, left: dict, negated: bool) -> dict:
        if self.accept_word("NOT"):
            negated = True
        if self.accept_word("DISTINCT"):
            self.expect_word("FROM")
            other = self.parse_expr(7)
            self.hit("expr.is_distinct")
            return {"type": "IsDistinct", "e": left, "other": other,
                    "negated": negated or None}
        for word in ("NULL", "TRUE", "FALSE", "UNKNOWN"):
            if self.accept_word(word):
                self.hit(f"expr.is_{word.lower()}")
                return {"type": "IsTest", "e": left, "test": word,
                        "negated": negated or None}
        other = self.parse_expr(7)
        self.hit("expr.is_compare")
        return {"type": "BinaryOp", "op": "IS NOT" if negated else "IS",
                "l": left, "r": other}

    def parse_in(self, left: dict, negated: bool) -> dict:
        if self.at_sym("("):
            self.i += 1
            if self.at_word("SELECT", "WITH", "VALUES"):
                query = self.parse_query()
                self.expect_sym(")")
                self.hit("expr.in_subquery")
                return {"type": "In", "e": left, "query": query, "list": None,
                        "negated": negated or None}
            items = [self.parse_expr()]
            while self.accept_sym(","):
                items.append(self.parse_expr())
            self.expect_sym(")")
            self.hit("expr.in_list")
            return {"type": "In", "e": left, "list": items, "query": None,
                    "negated": negated or None}
        # BigQuery: IN UNNEST(expr)
        expr = self.parse_expr(7)
        self.hit("expr.in_expr")
        return {"type": "In", "e": left, "source": expr, "list": None, "query": None,
                "negated": negated or None}

    def parse_prefix(self) -> dict:
        t = self.peek()
        start = t

        # ---- unary operators -------------------------------------------
        if t.kind == "name" and t.upper == "NOT":
            self.i += 1
            inner = self.parse_expr(PREFIX_BP["NOT"])
            self.hit("expr.not")
            node = {"type": "Not", "e": inner}
            node.update(self.span(start))
            return node
        if t.kind == "sym" and t.text in ("-", "+", "~"):
            self.i += 1
            inner = self.parse_expr(PREFIX_BP[t.text])
            self.hit("expr.unary")
            node = {"type": "UnaryOp", "op": t.text, "e": inner}
            node.update(self.span(start))
            return node

        # ---- literals ---------------------------------------------------
        if t.kind == "num":
            self.i += 1
            self.hit("expr.literal.number")
            node = {"type": "Literal", "kind": "number", "value": t.text, "raw": t.text}
            node.update(self.span(start))
            return node
        if t.kind == "string":
            self.i += 1
            self.hit("expr.literal.string")
            node = {"type": "Literal", "kind": "string", "value": t.value, "raw": t.text}
            node.update(self.span(start))
            return node
        if t.kind == "sym" and t.text == ":" and self.peek(1).kind == "name":
            self.i += 2
            self.hit("expr.parameter")
            node = {"type": "Parameter", "raw": ":" + self.toks[self.i - 1].text}
            node.update(self.span(start))
            return node
        if t.kind == "param":
            self.i += 1
            self.hit("expr.parameter")
            node = {"type": "Parameter", "raw": t.text}
            node.update(self.span(start))
            return node
        if t.kind == "name":
            u = t.upper
            if u == "NULL":
                self.i += 1
                self.hit("expr.literal.null")
                node = {"type": "Literal", "kind": "null", "value": None, "raw": "NULL"}
                node.update(self.span(start))
                return node
            if u in ("TRUE", "FALSE"):
                self.i += 1
                self.hit("expr.literal.bool")
                node = {"type": "Literal", "kind": "bool", "value": u == "TRUE", "raw": u}
                node.update(self.span(start))
                return node
            if u == "CASE":
                return self.parse_case()
            if u in ("CAST", "TRY_CAST", "SAFE_CAST") and self.peek(1).kind == "sym" and self.peek(1).text == "(":
                return self.parse_cast()
            if u == "EXTRACT" and self.peek(1).kind == "sym" and self.peek(1).text == "(":
                return self.parse_extract()
            if u == "INTERVAL":
                return self.parse_interval()
            if u == "EXISTS" and self.peek(1).kind == "sym" and self.peek(1).text == "(":
                self.i += 1
                self.expect_sym("(")
                query = self.parse_query()
                self.expect_sym(")")
                self.hit("expr.exists")
                node = {"type": "Exists", "query": query}
                node.update(self.span(start))
                return node
            if u in ("DATE", "TIMESTAMP", "TIME", "DATETIME") and self.peek(1).kind == "string":
                self.i += 2
                self.hit("expr.typed_literal")
                node = {"type": "TypedLiteral", "data_type": u,
                        "value": self.toks[self.i - 1].value}
                node.update(self.span(start))
                return node
            if u in ("ARRAY",) and self.peek(1).kind == "sym" and self.peek(1).text == "[":
                self.i += 2
                items = []
                if not self.at_sym("]"):
                    items.append(self.parse_expr())
                    while self.accept_sym(","):
                        items.append(self.parse_expr())
                self.expect_sym("]")
                self.hit("expr.array")
                node = {"type": "ArrayLit", "items": items}
                node.update(self.span(start))
                return node
            if u in ("ARRAY", "STRUCT") and self.peek(1).kind == "sym" and self.peek(1).text == "(":
                return self.parse_array_struct_ctor()
            if u in ("CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP",
                     "CURRENT_USER", "LOCALTIME", "LOCALTIMESTAMP", "SYSDATE"):
                self.i += 1
                if self.at_sym("(") and self.peek(1).kind == "sym" and self.peek(1).text == ")":
                    self.i += 2
                self.hit("expr.current_time")
                node = {"type": "Func", "name": u, "args": []}
                node.update(self.span(start))
                return node

        if t.kind == "sym" and t.text == "(":
            return self.parse_paren_expr()
        if t.kind == "sym" and t.text == "[":
            self.i += 1
            items = []
            if not self.at_sym("]"):
                items.append(self.parse_expr())
                while self.accept_sym(","):
                    items.append(self.parse_expr())
            self.expect_sym("]")
            self.hit("expr.array")
            node = {"type": "ArrayLit", "items": items}
            node.update(self.span(start))
            return node
        if t.kind == "sym" and t.text == "*":
            self.i += 1
            self.hit("expr.star")
            return {"type": "Star"}

        if t.kind in ("name", "qname"):
            return self.parse_name_chain_call()

        raise ParseError("unexpected token in expression", t)

    def parse_paren_expr(self) -> dict:
        start = self.expect_sym("(")
        if self.at_word("SELECT", "WITH", "VALUES"):
            query = self.parse_query()
            self.expect_sym(")")
            self.hit("expr.subquery")
            node = {"type": "Subquery", "query": query}
            node.update(self.span(start))
            return node
        first = self.parse_expr()
        if self.at_sym(","):
            items = [first]
            while self.accept_sym(","):
                items.append(self.parse_expr())
            self.expect_sym(")")
            self.hit("expr.tuple")
            node = {"type": "Tuple", "items": items}
            node.update(self.span(start))
            return node
        self.expect_sym(")")
        self.hit("expr.paren")
        return {"type": "Paren", "e": first}

    def parse_case(self) -> dict:
        start = self.expect_word("CASE")
        base = None
        if not self.at_word("WHEN"):
            base = self.parse_expr()
        whens = []
        while self.accept_word("WHEN"):
            cond = self.parse_expr()
            self.expect_word("THEN")
            res = self.parse_expr()
            whens.append({"when": cond, "then": res})
        else_ = None
        if self.accept_word("ELSE"):
            else_ = self.parse_expr()
        self.expect_word("END")
        self.hit("expr.case")
        node = {"type": "Case", "base": base, "whens": whens, "else": else_}
        node.update(self.span(start))
        return node

    def parse_cast(self) -> dict:
        start = self.peek()
        kind = self.peek().upper
        self.i += 1
        self.expect_sym("(")
        expr = self.parse_expr()
        self.expect_word("AS")
        data_type = self.parse_type()
        if self.at_word("FORMAT"):  # BigQuery CAST(x AS STRING FORMAT 'YYYY')
            self.i += 1
            self.parse_expr()
        self.expect_sym(")")
        self.hit("expr.cast")
        node = {"type": "Cast", "e": expr, "to": data_type,
                "style": "safe" if kind == "SAFE_CAST" else ("try" if kind == "TRY_CAST" else "cast")}
        node.update(self.span(start))
        return node

    def parse_extract(self) -> dict:
        start = self.expect_word("EXTRACT")
        self.expect_sym("(")
        if self.peek().kind in ("name", "qname", "string"):
            field = self.peek().value or self.peek().text
            self.i += 1
        else:
            raise ParseError("expected date part in EXTRACT", self.peek())
        if self.accept_word("FROM"):
            expr = self.parse_expr()
        else:  # BigQuery allows EXTRACT(part, expr) in some contexts
            self.expect_sym(",")
            expr = self.parse_expr()
        self.expect_sym(")")
        self.hit("expr.extract")
        node = {"type": "Extract", "field": field.upper(), "e": expr}
        node.update(self.span(start))
        return node

    def parse_interval(self) -> dict:
        start = self.expect_word("INTERVAL")
        value = self.parse_expr(POSTFIX_BP["::"])
        unit = None
        if self.peek().kind == "name" and self.peek().upper in (
                "YEAR", "YEARS", "MONTH", "MONTHS", "DAY", "DAYS", "HOUR", "HOURS",
                "MINUTE", "MINUTES", "SECOND", "SECONDS", "WEEK", "WEEKS",
                "QUARTER", "QUARTERS", "MICROSECOND", "MILLISECOND"):
            unit = self.peek().upper
            self.i += 1
        self.hit("expr.interval")
        node = {"type": "Interval", "value": value, "unit": unit}
        node.update(self.span(start))
        return node

    def parse_array_struct_ctor(self) -> dict:
        start = self.peek()
        kind = self.peek().upper
        self.i += 1
        self.expect_sym("(")
        items = []
        if not self.at_sym(")"):
            items.append(self.parse_expr())
            while self.accept_sym(","):
                items.append(self.parse_expr())
        self.expect_sym(")")
        if kind == "ARRAY" and self.at_sym("["):  # ARRAY<INT64>[...]
            pass
        self.hit("expr.array" if kind == "ARRAY" else "expr.struct")
        node = {"type": "ArrayLit" if kind == "ARRAY" else "StructLit", "items": items}
        node.update(self.span(start))
        return node

    # -- names, calls, windows ----------------------------------------------
    def read_name(self) -> str:
        t = self.peek()
        if t.kind in ("name", "qname"):
            self.i += 1
            return t.value or t.text
        if t.kind == "string":
            self.i += 1
            return t.value
        raise ParseError("expected name", t)

    def read_name_parts(self, allow_dash: bool = False) -> list[str]:
        """Read a possibly dotted name: ``a``, ``a.b``, ``a.b.c``.

        Quoted parts participate: mysql/BIRD write ``T1.`Col``` and some BIRD
        queries write ``T2.'Col'``.  A single leading quoted token may itself
        contain dots (`` `proj.dataset.table` ``) and is split accordingly.
        ``allow_dash`` additionally accepts unquoted BigQuery project names such
        as ``bigquery-public-data.x.y`` (dashes must be tight against the names
        so that arithmetic ``a-b`` is never swallowed).
        """
        t = self.peek()
        if t.kind == "qname":
            parts = (t.value or t.text).split(".")
            self.i += 1
        elif t.kind == "name" and t.upper not in NON_NAME_WORDS:
            parts = [t.text]
            self.i += 1
        elif t.kind == "string" and t.quote == '"' and len(t.value.split(".")) <= 3:
            # quoted identifier lexed as a string (double quotes under bigquery
            # string rules); accepting it keeps "T"."Col" readable.
            parts = t.value.split(".")
            self.i += 1
            self.hit("expr.quoted_ident_string")
        else:
            return []
        while True:
            if allow_dash and self.at_sym("-") and self.peek(1).kind == "name":
                dash, nxt = self.peek(), self.peek(1)
                tight = dash.start == self.toks[self.i - 1].end and nxt.start == dash.end
                nxt2 = self.peek(2)
                if tight and nxt2.kind == "sym" and nxt2.text in (".", "(", "-"):
                    self.i += 2
                    parts[-1] += "-" + nxt.text
                    continue
            if self.at_sym(".") and self.peek(1).kind in ("name", "qname", "string"):
                self.i += 1
                nt = self.peek()
                self.i += 1
                if nt.kind in ("qname", "string"):
                    if nt.kind == "string":
                        self.hit("expr.quoted_ident_string")
                    parts.extend((nt.value or nt.text).split("."))
                else:
                    parts.append(nt.text)
                continue
            break
        return parts

    def parse_with_offset(self) -> tuple[bool, str | None]:
        """``WITH OFFSET [AS alias]`` after ``UNNEST``."""
        if self.at_word("WITH") and self.peek(1).kind == "name" and self.peek(1).upper == "OFFSET":
            self.i += 2
            alias = None
            if self.accept_word("AS"):
                alias = self.read_name()
            elif self.peek().kind == "name" and self.peek().upper not in RESERVED:
                alias = self.read_name()
            return True, alias
        return False, None

    def parse_alias_with_columns(self) -> tuple[str | None, list[str] | None]:
        """``AS v(c1, c2)`` table alias with a column-alias list."""
        alias = self.parse_optional_alias()
        columns = None
        if alias and self.at_sym("(") and self.peek(1).kind in ("name", "qname"):
            save = self.i
            try:
                self.i += 1
                columns = [self.read_name()]
                while self.accept_sym(","):
                    columns.append(self.read_name())
                self.expect_sym(")")
                if not self.at_sym(")", ",", ";") and not self.at_eof() and not self.at_word(
                        "WHERE", "GROUP", "ORDER", "LIMIT", "ON", "JOIN", "UNION"):
                    raise ParseError("not a column alias list", self.peek())
            except ParseError:
                self.i = save
                columns = None
        return alias, columns

    def parse_type(self) -> str:
        parts: list[str] = []
        t = self.peek()
        if t.kind not in ("name", "qname"):
            raise ParseError("expected type name", t)
        self.i += 1
        parts.append(t.value or t.text)
        # dotted type names (snowflake "DB"."SCHEMA"."TYPE") are irrelevant here
        if self.at_sym("("):
            self.i += 1
            inner = []
            depth = 1
            while not self.at_eof() and depth:
                tt = self.peek()
                if tt.kind == "sym" and tt.text == "(":
                    depth += 1
                elif tt.kind == "sym" and tt.text == ")":
                    depth -= 1
                    if depth == 0:
                        self.i += 1
                        break
                inner.append(tt.text)
                self.i += 1
            parts[-1] += "(" + "".join(inner) + ")"
        if self.at_sym("<") and self.peek().start == self.toks[self.i - 1].end:
            # parameterised type: ARRAY<INT64>, STRUCT<a INT64>
            depth = 0
            raw = []
            while not self.at_eof():
                tt = self.peek()
                if tt.kind == "sym" and tt.text == "<":
                    depth += 1
                elif tt.kind == "sym" and tt.text == ">":
                    depth -= 1
                raw.append(tt.text)
                self.i += 1
                if depth == 0:
                    break
            parts[-1] += "".join(raw)
        return ".".join(parts)

    def parse_call_args_inner(self, fname: str = "") -> tuple[list[dict], bool]:
        """Parse call arguments; the opening ``(`` is already consumed.

        Returns ``(args, distinct)`` where ``distinct`` records an aggregate
        ``DISTINCT`` modifier such as ``COUNT(DISTINCT x)``.  ``fname`` enables
        the alternate argument syntax of a few standard functions
        (``SUBSTRING(x FROM 1 FOR 2)``).
        """
        args: list[dict] = []
        distinct = False
        if self.at_word("DISTINCT"):
            distinct = True
            self.i += 1
        elif self.at_word("ALL") and self.peek(1).kind != "name":
            self.i += 1
        if self.at_sym(")"):
            self.i += 1
            return args, distinct
        while True:
            if self.at_sym("*"):
                self.i += 1
                args.append({"type": "Star"})
                self.hit("expr.star_arg")
            elif (qstar := self.match_qualified_star()) is not None:
                args.append({"type": "Star", "qualifier": qstar})
                self.hit("expr.star_arg")
            else:
                arg = self.parse_expr()
                if fname in ("SUBSTRING", "SUBSTR") and (self.at_word("FROM") or self.at_word("FOR")):
                    extra = []
                    if self.accept_word("FROM"):
                        extra.append(self.parse_expr())
                    if self.accept_word("FOR"):
                        extra.append(self.parse_expr())
                    args.append(arg)
                    args.extend(extra)
                    self.hit("expr.substring_from_for")
                    self.expect_sym(")")
                    return args, distinct
                if self.accept_word("AS"):
                    arg = {"type": "AliasedExpr", "expr": arg, "alias": self.read_name()}
                if self.at_sym("=>"):  # snowflake named argument: name => value
                    self.i += 1
                    arg = {"type": "NamedArg", "name": self._name_of(arg),
                           "value": self.parse_expr()}
                    self.hit("expr.named_arg")
                args.append(arg)
            if self.at_word("ORDER") and self.peek(1).kind == "name" and self.peek(1).upper == "BY":
                args.append({"type": "OrderByArgs", "items": self.parse_order_by()})
                self.hit("expr.agg_order_by")
            if self.at_word("SEPARATOR"):  # mysql GROUP_CONCAT(x SEPARATOR ',')
                self.i += 1
                args.append({"type": "Separator", "value": self.parse_expr()})
            if not self.accept_sym(","):
                break
        self.expect_sym(")")
        return args, distinct

    def parse_window_spec_body(self) -> dict:
        spec: dict = {}
        # named window reference: OVER (w)
        if (self.peek().kind in ("name", "qname")
                and self.peek().upper not in ("PARTITION", "ORDER", "ROWS", "RANGE", "GROUPS")
                and self.peek(1).kind == "sym" and self.peek(1).text == ")"):
            spec["ref"] = self.read_name()
            self.hit("window.ref")
            return spec
        if self.accept_word("PARTITION"):
            self.expect_word("BY")
            spec["partition_by"] = [self.parse_expr()]
            while self.accept_sym(","):
                spec["partition_by"].append(self.parse_expr())
            self.hit("window.partition_by")
        if self.at_word("ORDER"):
            spec["order_by"] = self.parse_order_by()
        if self.at_word("ROWS", "RANGE", "GROUPS"):
            unit = self.peek().upper
            self.i += 1
            frame: dict = {"unit": unit}
            if self.accept_word("BETWEEN"):
                frame["start"] = self.parse_frame_bound()
                self.expect_word("AND")
                frame["end"] = self.parse_frame_bound()
                self.hit("window.frame_between")
            else:
                frame["start"] = self.parse_frame_bound()
                self.hit("window.frame")
            spec["frame"] = frame
        return spec

    def parse_frame_bound(self) -> dict:
        if self.accept_word("UNBOUNDED"):
            direction = "PRECEDING" if self.accept_word("PRECEDING") else (
                "FOLLOWING" if self.accept_word("FOLLOWING") else None)
            if direction is None:
                raise ParseError("expected PRECEDING/FOLLOWING", self.peek())
            return {"bound": "UNBOUNDED", "direction": direction}
        if self.accept_word("CURRENT"):
            self.expect_word("ROW")
            return {"bound": "CURRENT ROW", "direction": None}
        expr = self.parse_expr()
        direction = "PRECEDING" if self.accept_word("PRECEDING") else (
            "FOLLOWING" if self.accept_word("FOLLOWING") else None)
        if direction is None:
            raise ParseError("expected PRECEDING/FOLLOWING", self.peek())
        return {"bound": expr, "direction": direction}

    def parse_name_chain_call(self) -> dict:
        """Parse a name chain; if followed by ``(`` it is a function call."""
        start = self.peek()
        parts = self.read_name_parts()
        if not parts:
            raise ParseError("expected identifier", self.peek())
        if not self.at_sym("("):
            node = {"type": "Column", "parts": parts, "raw": ".".join(parts)}
            if len(parts) > 1:
                self.hit("expr.qualified_column")
            else:
                self.hit("expr.column")
            node.update(self.span(start))
            return node
        self.i += 1  # consume '('
        name = ".".join(parts).upper()
        args, distinct = self.parse_call_args_inner(name)
        func: dict = {"type": "Func", "name": name, "args": args}
        if distinct:
            func["distinct"] = True
            self.hit("expr.agg_distinct")
        if name in AGGREGATE_FUNCS:
            func["is_aggregate"] = True
            self.hit("expr.agg")
        else:
            self.hit("expr.func")
        if self.accept_word("IGNORE"):
            self.expect_word("NULLS")
            func["nulls"] = "IGNORE"
        elif self.accept_word("RESPECT"):
            self.expect_word("NULLS")
            func["nulls"] = "RESPECT"
        if self.at_word("WITHIN"):
            self.i += 1
            self.expect_word("GROUP")
            self.expect_sym("(")
            self.expect_word("ORDER")
            self.expect_word("BY")
            func["within_group"] = True
            self.parse_order_by_after_by()
            self.expect_sym(")")
            self.hit("expr.within_group")
        if self.at_word("FILTER") and self.peek(1).kind == "sym" and self.peek(1).text == "(":
            self.i += 1
            self.expect_sym("(")
            self.expect_word("WHERE")
            func["filter"] = self.parse_expr()
            self.expect_sym(")")
            self.hit("expr.filter_clause")
        if self.at_word("OVER"):
            self.i += 1
            if self.at_sym("("):
                self.i += 1
                spec = self.parse_window_spec_body()
                self.expect_sym(")")
                func["over"] = {"type": "Window", **spec}
            else:
                func["over"] = {"type": "Window", "ref": self.read_name()}
            self.hit("expr.window")
        func.update(self.span(start))
        return func

    def parse_order_by_after_by(self) -> None:
        """Consume order items when ORDER BY was already read."""
        while True:
            self.parse_expr()
            self.accept_word("ASC") or self.accept_word("DESC")
            if self.at_word("NULLS") and self.peek(1).kind == "name":
                self.i += 2
            if not self.accept_sym(","):
                break

    @staticmethod
    def _name_of(node: dict) -> str:
        if node.get("type") == "Column":
            return node["parts"][-1]
        return ""


def parse_sql(sql: str, dialect: str = "sqlite", spans: bool = False) -> tuple[dict, dict]:
    """Parse one statement (or script) and return ``(ast, rule_hits)``."""
    p = Parser(sql, dialect=dialect, spans=spans)
    ast = p.parse_script()
    return ast, p.rules
