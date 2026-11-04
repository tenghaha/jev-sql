"""Extract all schema-relevant mentions from a question in one LLM call."""

from __future__ import annotations

import logging
from typing import Any

from langchain_openai import ChatOpenAI

from .config import require_env
from .utils import message_text, parse_json_object

logger = logging.getLogger(__name__)

MENTION_INSTRUCTIONS = """You extract schema-relevant mentions from a natural-language question for a text-to-SQL task.

Definition: a mention is a contiguous phrase that refers to a database object, a filter value, a time condition, an aggregation target, or a join relationship.

Rules:
1. Extract every mention in a single pass, without omitting implicit references.
2. Extract each distinct mention text only once, even if it appears multiple times.
3. Copy the mention text exactly as it appears in the question or evidence.
4. Assign one span_type from: entity, entity_attribute, numeric_filter, temporal_filter, aggregation, join_relation, other.
5. Assign sequential span_id values: m1, m2, m3, ...
6. Return only the JSON object, with no explanation and no markdown fence.

Output format:
{"mentions": [{"span_id": "m1", "text": "...", "span_type": "..."}]}"""


def build_mention_llm(config: dict[str, Any]) -> ChatOpenAI:
    return ChatOpenAI(
        model=require_env(config["model_env"]),
        base_url=require_env(config["base_url_env"]),
        api_key=require_env(config["api_key_env"]),
        temperature=config.get("temperature", 0.0),
        timeout=config.get("timeout", 120),
        max_retries=config.get("max_retries", 2),
        # Disable reasoning mode for mention extraction.
        extra_body={"enable_thinking": False},
    )


def extract_mentions(
    question: str,
    evidence: str | None,
    config: dict[str, Any],
) -> list[dict[str, str]]:
    """Run one generation call that returns every mention in the question."""
    user_prompt = (
        f"{MENTION_INSTRUCTIONS}\n\n"
        f"Question:\n{question}\n\n"
        f"Evidence:\n{evidence or '(none)'}"
    )
    response = build_mention_llm(config).invoke(
        [
            ("system", "You are a precise information extraction module."),
            ("human", user_prompt),
        ]
    )
    output_text = message_text(response.content)
    logger.debug("Mention extraction output: %s", output_text)
    data = parse_json_object(output_text)
    raw_mentions = data.get("mentions")
    if not isinstance(raw_mentions, list):
        raise ValueError("Mention extraction output has no 'mentions' list")

    mentions = []
    seen_texts = set()
    for item in raw_mentions:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text or text.lower() in seen_texts:
            continue
        seen_texts.add(text.lower())
        mentions.append(
            {
                "span_id": f"m{len(mentions) + 1}",
                "text": text,
                "span_type": str(item.get("span_type") or "other"),
            }
        )
    return mentions
