"""Generate one SQL query from the JEV-linked schema with a single LLM call."""

from __future__ import annotations

import logging
from typing import Any

from langchain_openai import ChatOpenAI

from .config import require_env
from .utils import message_text, parse_json_object

logger = logging.getLogger(__name__)

SQL_INSTRUCTIONS = """You write one SQLite query that answers a natural-language question.

Rules:
1. Target the SQLite dialect only: use SQLite syntax and functions (e.g. LIMIT for row limits, strftime for dates). 
2. Use only the tables and columns listed in the schema.
3. The statement must be syntactically correct and runnable as-is: quote identifiers with double quotes, give every JOIN an ON condition, and close every parenthesis and clause.
4. Follow the evidence when it specifies the computation or the expected output.
5. Output a single statement with no comments, and return only a JSON object of the form {"sql": "..."} without a markdown fence."""


def build_sql_llm(config: dict[str, Any]) -> ChatOpenAI:
    """Build the one-shot SQL generator from environment-variable config."""
    return ChatOpenAI(
        model=require_env(config["model_env"]),
        base_url=require_env(config["base_url_env"]),
        api_key=require_env(config["api_key_env"]),
        temperature=config.get("temperature", 0.0),
        timeout=config.get("timeout", 120),
        max_retries=config.get("max_retries", 2),
        # Disable reasoning mode for SQL generation.
        extra_body={"enable_thinking": False},
    )


def _column_index(database: dict[str, Any]) -> dict[str, tuple[str, dict[str, dict[str, Any]]]]:
    """Index a database schema by table name and then by column name (casefolded)."""
    index: dict[str, tuple[str, dict[str, dict[str, Any]]]] = {}
    for table in database.get("tables") or []:
        name = str(table.get("name", ""))
        columns = {
            str(column.get("name", "")).casefold(): column
            for column in table.get("columns") or []
        }
        index[name.casefold()] = (name, columns)
    return index


def render_linked_schema(database: dict[str, Any], linked_columns: list[dict[str, Any]]) -> str:
    """Render the linked tables/columns as SQLite CREATE TABLE statements."""
    index = _column_index(database)
    order: list[str] = []
    grouped: dict[str, dict[str, Any]] = {}
    for item in linked_columns or []:
        table = str(item.get("table") or "").strip()
        column = str(item.get("column") or "").strip()
        if not table or not column:
            continue
        key = table.casefold()
        if key not in grouped:
            grouped[key] = {"name": table, "columns": [], "seen": set()}
            order.append(key)
        entry = grouped[key]
        column_key = column.casefold()
        if column_key in entry["seen"]:
            continue
        entry["seen"].add(column_key)
        entry["columns"].append({"name": column, "dtype": item.get("dtype")})

    statements: list[str] = []
    for key in order:
        entry = grouped[key]
        _, lookup = index.get(key, (entry["name"], {}))
        column_lines = []
        for column in entry["columns"]:
            meta = lookup.get(column["name"].casefold()) or {}
            dtype = column.get("dtype") or meta.get("type") or "text"
            line = f'  "{column["name"]}" {str(dtype).upper()}'
            sample = meta.get("sample")
            if sample is not None:
                line += f"  -- e.g. {str(sample)[:40]}"
            column_lines.append(line)
        statements.append(f'CREATE TABLE "{entry["name"]}" (\n' + ",\n".join(column_lines) + "\n);")
    return "\n\n".join(statements)


def _strip_fence(text: str) -> str:
    """Remove a Markdown code fence from raw LLM output."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def extract_sql(text: str) -> str:
    """Extract a SQL statement from LLM output, tolerating JSON or a fenced block."""
    try:
        data = parse_json_object(text)
        sql = data.get("sql")
        if isinstance(sql, str) and sql.strip():
            return sql.strip()
    except ValueError:
        pass
    return _strip_fence(text)


def _usage(response: Any) -> dict[str, Any] | None:
    meta = getattr(response, "usage_metadata", None)
    if isinstance(meta, dict):
        return {
            "input_tokens": meta.get("input_tokens"),
            "output_tokens": meta.get("output_tokens"),
        }
    return None


def generate_sql(
    question: str,
    evidence: str | None,
    schema_text: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Run one generation call that returns a single SQL statement."""
    user_prompt = (
        f"{SQL_INSTRUCTIONS}\n\n"
        f"Schema:\n{schema_text or '(none)'}\n\n"
        f"Evidence:\n{evidence or '(none)'}\n\n"
        f"Question:\n{question}"
    )
    response = build_sql_llm(config).invoke(
        [
            ("system", "You are a precise SQLite SQL generator."),
            ("human", user_prompt),
        ]
    )
    output_text = message_text(response.content)
    logger.debug("SQL generation output: %s", output_text)
    return {"sql": extract_sql(output_text), "raw": output_text, "usage": _usage(response)}
