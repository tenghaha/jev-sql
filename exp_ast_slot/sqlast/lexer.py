"""Dialect-aware SQL tokenizer.

Purpose
-------
Turns one SQL statement (or a ``;``-separated script) into a flat token list.
The tokenizer knows only lexical facts: which quote character delimits an
identifier, which delimits a string, whether backslash escapes are enabled and
which comment prefixes start a line comment.  Everything structural lives in
``parser.py``.

Token kinds
-----------
``name``   unquoted word (keyword or identifier are not distinguished here)
``qname``  quoted identifier (backticks for sqlite/mysql/bigquery, double quotes
           for postgres/snowflake)
``string`` string literal (single quotes, plus double quotes in
           mysql/bigquery where they are string delimiters)
``num``    numeric literal
``sym``    operator or punctuation
``param``  bind parameter (``?``, ``@name``, ``:name``, ``$1``)
``eof``    end of input

Every token keeps half-open character offsets ``[start, end)`` into the source
text so that downstream stages can attach provenance spans to AST nodes.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Tok", "Dialect", "DIALECTS", "LexError", "tokenize", "get_dialect"]


@dataclass(frozen=True)
class Dialect:
    """Lexical conventions of one SQL dialect."""

    name: str
    ident_quotes: tuple[str, ...] = ("`",)
    string_quotes: tuple[str, ...] = ("'",)
    backslash_escapes: bool = False
    hash_comment: bool = False
    dollar_quote: bool = False


DIALECTS: dict[str, Dialect] = {
    "sqlite": Dialect("sqlite", ident_quotes=("`", "\""), string_quotes=("'",)),
    "mysql": Dialect("mysql", ident_quotes=("`",), string_quotes=("'", "\""),
                     backslash_escapes=True, hash_comment=True),
    "postgres": Dialect("postgres", ident_quotes=("\"",), string_quotes=("'",),
                        dollar_quote=True),
    "bigquery": Dialect("bigquery", ident_quotes=("`",), string_quotes=("'", "\""),
                        backslash_escapes=True, hash_comment=True),
    "snowflake": Dialect("snowflake", ident_quotes=("\"",), string_quotes=("'",),
                         backslash_escapes=True),
}


def get_dialect(name: str) -> Dialect:
    try:
        return DIALECTS[name]
    except KeyError:  # pragma: no cover - configuration error
        raise SystemExit(
            f"unknown dialect {name!r}; known: {', '.join(sorted(DIALECTS))}"
        ) from None


@dataclass(frozen=True)
class Tok:
    kind: str
    text: str
    start: int
    end: int
    value: str = ""
    quote: str = ""

    @property
    def upper(self) -> str:
        """Upper-cased text for keyword comparison (names and symbols only)."""
        return self.text.upper() if self.kind in ("name", "sym") else self.text


class LexError(Exception):
    def __init__(self, msg: str, pos: int, sql: str):
        self.pos = pos
        snippet = sql[max(0, pos - 30): pos + 30].replace("\n", " ")
        super().__init__(f"{msg} at offset {pos} near {snippet!r}")


# Longest-first so that greedy matching is a plain startswith loop.
_SYMBOLS = (
    "->>", "<=>", "::", "->", ">=", "<=", "<>", "!=", "||", "=>", ":=", "<<",
    ">>", "!<", "!>",
    "=", "<", ">", "+", "-", "*", "/", "%", "(", ")", "[", "]", "{", "}",
    ",", ".", ";", "&", "|", "^", "~", "?", ":", "@",
)


def _is_name_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_" or ord(ch) > 127


def _is_name_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$" or ord(ch) > 127


def tokenize(sql: str, dialect: str | Dialect = "sqlite") -> list[Tok]:
    """Split ``sql`` into tokens.  Raises :class:`LexError` on malformed input."""
    d = dialect if isinstance(dialect, Dialect) else get_dialect(dialect)
    toks: list[Tok] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]

        # ---- whitespace -------------------------------------------------
        if ch in " \t\r\n\f\v":
            i += 1
            continue

        # ---- line comments ----------------------------------------------
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if ch == "#" and d.hash_comment:
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue

        # ---- block comment ----------------------------------------------
        if ch == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                raise LexError("unterminated block comment", i, sql)
            i = j + 2
            continue

        # ---- string literals --------------------------------------------
        if ch in d.string_quotes:
            tok, i = _scan_quoted(sql, i, ch, d, "string")
            toks.append(tok)
            continue

        # ---- quoted identifiers -----------------------------------------
        if ch in d.ident_quotes and ch not in d.string_quotes:
            tok, i = _scan_quoted(sql, i, ch, d, "qname")
            toks.append(tok)
            continue

        # ---- prefixed string literals (r'...', b'...', rb'...') ---------
        if ch in "rRbB":
            j = i
            prefix = ""
            while j < n and sql[j] in "rRbB" and len(prefix) < 2:
                prefix += sql[j]
                j += 1
            if j < n and sql[j] in d.string_quotes:
                tok, i = _scan_quoted(sql, j, sql[j], d, "string")
                toks.append(Tok("string", prefix + tok.text, tok.start, tok.end,
                                tok.value, tok.quote))
                continue

        # ---- dollar-quoted strings (postgres) ---------------------------
        if ch == "$" and d.dollar_quote:
            end_tag = sql.find("$", i + 1)
            if sql.startswith("$$", i) or (
                end_tag > i + 1 and sql[i + 1:end_tag].replace("_", "").isalnum()
            ):
                tag = sql[i:end_tag + 1]
                close = sql.find(tag, end_tag + 1)
                if close >= 0:
                    body = sql[end_tag + 1: close]
                    toks.append(Tok("string", sql[i:close + len(tag)], i,
                                    close + len(tag), body, "$"))
                    i = close + len(tag)
                    continue

        # ---- numeric literals -------------------------------------------
        if ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            tok, i = _scan_number(sql, i)
            toks.append(tok)
            continue

        # ---- parameters --------------------------------------------------
        if ch == "?":
            toks.append(Tok("param", "?", i, i + 1, "?"))
            i += 1
            continue
        if ch in "@$" and i + 1 < n and (_is_name_char(sql[i + 1])):
            j = i + 1
            while j < n and _is_name_char(sql[j]):
                j += 1
            toks.append(Tok("param", sql[i:j], i, j, sql[i + 1:j]))
            i = j
            continue

        # ---- identifiers / keywords --------------------------------------
        if _is_name_start(ch):
            j = i + 1
            while j < n and _is_name_char(sql[j]):
                j += 1
            toks.append(Tok("name", sql[i:j], i, j, sql[i:j]))
            i = j
            continue

        # ---- operators / punctuation -------------------------------------
        for sym in _SYMBOLS:
            if sql.startswith(sym, i):
                toks.append(Tok("sym", sym, i, i + len(sym), sym))
                i += len(sym)
                break
        else:
            raise LexError(f"unexpected character {ch!r}", i, sql)

    toks.append(Tok("eof", "", n, n, ""))
    return toks


def _scan_quoted(sql: str, i: int, quote: str, d: Dialect, kind: str) -> tuple[Tok, int]:
    """Scan a quoted token starting at ``i``; ``quote`` repeats to escape."""
    n = len(sql)
    j = i + 1
    parts: list[str] = []
    while j < n:
        c = sql[j]
        if c == "\\" and d.backslash_escapes:
            if j + 1 < n:
                parts.append(sql[j:j + 2])
                j += 2
                continue
        if c == quote:
            if j + 1 < n and sql[j + 1] == quote:  # doubled quote escape
                parts.append(quote)
                j += 2
                continue
            text = sql[i:j + 1]
            value = _unescape("".join(parts), d)
            return Tok(kind, text, i, j + 1, value, quote), j + 1
        parts.append(c)
        j += 1
    raise LexError("unterminated quoted literal", i, sql)


def _unescape(body: str, d: Dialect) -> str:
    if not d.backslash_escapes or "\\" not in body:
        return body
    out: list[str] = []
    k = 0
    simple = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\",
              "'": "'", "\"": "\"", "`": "`"}
    while k < len(body):
        c = body[k]
        if c == "\\" and k + 1 < len(body):
            nxt = body[k + 1]
            out.append(simple.get(nxt, nxt))
            k += 2
            continue
        out.append(c)
        k += 1
    return "".join(out)


def _scan_number(sql: str, i: int) -> tuple[Tok, int]:
    n = len(sql)
    j = i
    if sql.startswith(("0x", "0X"), i):
        j = i + 2
        while j < n and (sql[j].isdigit() or sql[j].lower() in "abcdef"):
            j += 1
        return Tok("num", sql[i:j], i, j, sql[i:j]), j
    while j < n and sql[j].isdigit():
        j += 1
    if j < n and sql[j] == "." and not (j + 1 < n and sql[j + 1] == "."):
        j += 1
        while j < n and sql[j].isdigit():
            j += 1
    if j < n and sql[j] in "eE":
        k = j + 1
        if k < n and sql[k] in "+-":
            k += 1
        if k < n and sql[k].isdigit():
            j = k
            while j < n and sql[j].isdigit():
                j += 1
    return Tok("num", sql[i:j], i, j, sql[i:j]), j
