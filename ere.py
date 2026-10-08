"""ere.py — Entity & Relation Extraction + article summarization.

Extracts typed entity-relation triplets from a news article title and
description while simultaneously producing a 2-3 sentence summary.
The LLM returns structured JSON validated against Pydantic v2 schemas.

Public API:
    extract_entities_and_summary(title, description, llm) -> ArticleExtraction
    normalize_entity(name) -> str

Entity type taxonomy (Phase 1):
    org, person, place, event, product, technology

Relation inventory:
    LOCATED_IN, DISRUPTED, MANUFACTURES, SUPPLIES, EMPLOYS, ACQUIRED,
    PARTNERS_WITH, INVESTED_IN, REGULATES, COMPETES_WITH, PRODUCES,
    EXPORTS_TO, SANCTIONS, AFFECTS
"""

from __future__ import annotations

import logging
import re
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

_RELATION_TYPES = frozenset({
    "LOCATED_IN", "DISRUPTED", "MANUFACTURES", "SUPPLIES", "EMPLOYS",
    "ACQUIRED", "PARTNERS_WITH", "INVESTED_IN", "REGULATES", "COMPETES_WITH",
    "PRODUCES", "EXPORTS_TO", "SANCTIONS", "AFFECTS",
})

_ENTITY_TYPES = frozenset({"org", "person", "place", "event", "product", "technology"})

_CORPORATE_SUFFIXES = (
    "inc", "inc.", "corp", "corp.", "co", "co.", "ltd", "ltd.",
    "gmbh", "llc", "ag", "s.a.", "bv", "plc", "limited", "corporation",
    "company",
)


class Triplet(BaseModel):
    """A single subject-relation-object fact extracted from an article."""

    subject: str = Field(..., min_length=1, description="Entity name (display form)")
    relation: str = Field(..., min_length=1, description="Relationship type")
    object: str = Field(..., min_length=1, description="Target entity name (display form)")
    subject_type: str = Field(
        ..., description="Entity type of the subject",
    )
    object_type: str = Field(
        ..., description="Entity type of the object",
    )

    @field_validator("subject_type", "object_type")
    @classmethod
    def _valid_entity_type(cls, v: str) -> str:
        if v.lower() not in _ENTITY_TYPES:
            raise ValueError(f"Invalid entity type {v!r}; must be one of {_ENTITY_TYPES}")
        return v.lower()

    @field_validator("relation")
    @classmethod
    def _valid_relation(cls, v: str) -> str:
        upper = v.upper().replace(" ", "_")
        if upper not in _RELATION_TYPES:
            # Keep the value but log a warning later; normalising unknown
            # relations to AFFECTS keeps the graph schema stable.
            return "AFFECTS"
        return upper


class ArticleExtraction(BaseModel):
    """Structured output of the ERE LLM call."""

    summary: str = Field(
        ..., min_length=10,
        description="A 2-3 sentence summary of the article",
    )
    triplets: list[Triplet] = Field(
        default_factory=list,
        description="Up to 10 typed entity-relation triplets extracted from the article",
    )


EXTRACTION_PROMPT_TEMPLATE = """You are a precise information extraction system. Read the news article below and output valid JSON with two fields:
- summary: a concise 2-3 sentence summary of the article
- triplets: an array of up to 10 entity-relation-object triplets

Use these entity types: org, person, place, event, product, technology.
Use these relation types: LOCATED_IN, DISRUPTED, MANUFACTURES, SUPPLIES, EMPLOYS, ACQUIRED, PARTNERS_WITH, INVESTED_IN, REGULATES, COMPETES_WITH, PRODUCES, EXPORTS_TO, SANCTIONS, AFFECTS.

Return ONLY the JSON. No markdown, no explanation.

Example 1:
Title: TSMC halts some chipmaking after Taiwan earthquake
Content: The world's largest contract chipmaker suspended production at several plants after a magnitude 7.4 quake struck Taiwan.
Output:
{{
  "summary": "TSMC suspended production at several plants following a major earthquake in Taiwan.",
  "triplets": [
    {{"subject": "TSMC", "subject_type": "org", "relation": "LOCATED_IN", "object": "Taiwan", "object_type": "place"}},
    {{"subject": "Earthquake", "subject_type": "event", "relation": "DISRUPTED", "object": "TSMC", "object_type": "org"}},
    {{"subject": "TSMC", "subject_type": "org", "relation": "MANUFACTURES", "object": "chips", "object_type": "technology"}}
  ]
}}

Example 2:
Title: Apple expands AI chip orders with TSMC
Content: Apple has increased orders for TSMC's advanced packaging capacity to support its upcoming AI servers.
Output:
{{
  "summary": "Apple increased orders for TSMC's advanced packaging capacity to power its AI server roadmap.",
  "triplets": [
    {{"subject": "Apple", "subject_type": "org", "relation": "SUPPLIES", "object": "TSMC", "object_type": "org"}},
    {{"subject": "Apple", "subject_type": "org", "relation": "PRODUCES", "object": "AI servers", "object_type": "product"}},
    {{"subject": "TSMC", "subject_type": "org", "relation": "MANUFACTURES", "object": "AI chips", "object_type": "technology"}}
  ]
}}

Example 3:
Title: EU Parliament passes new AI Act amendments
Content: Lawmakers approved stricter rules for general-purpose AI models, requiring more transparency from developers.
Output:
{{
  "summary": "The EU Parliament approved stricter transparency rules for general-purpose AI models.",
  "triplets": [
    {{"subject": "EU Parliament", "subject_type": "org", "relation": "REGULATES", "object": "AI models", "object_type": "technology"}},
    {{"subject": "AI Act", "subject_type": "event", "relation": "AFFECTS", "object": "AI developers", "object_type": "org"}}
  ]
}}

Now extract from this article:
Title: {title}
Content: {content}
Output:
"""


def normalize_entity(name: str) -> str:
    """Normalize an entity name for use as a DynamoDB graph key.

    Steps:
      1. Lowercase.
      2. Remove common corporate suffixes (Inc, Corp, GmbH, LLC, ...).
      3. Collapse whitespace.
      4. Strip trailing punctuation.

    Args:
        name: Raw entity name from the LLM.

    Returns:
        Normalized entity key.
    """
    if not name:
        return ""
    n = name.lower().strip()
    # Remove corporate/legal suffixes as whole tokens.
    tokens = n.split()
    cleaned_tokens: list[str] = []
    for tok in tokens:
        bare = tok.rstrip(".,;:!?")
        if bare in _CORPORATE_SUFFIXES:
            continue
        cleaned_tokens.append(tok)
    n = " ".join(cleaned_tokens)
    # Collapse whitespace and strip trailing punctuation one more time.
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"[^a-z0-9\s]+$", "", n)
    return n.strip()


def _build_prompt(title: str, content: str) -> str:
    """Render the few-shot extraction prompt."""
    return EXTRACTION_PROMPT_TEMPLATE.format(title=title, content=content)


def _clean_summary(raw: str) -> str:
    """Trim and clean the summary returned by the LLM."""
    raw = re.sub(r"<thinking>.*?</thinking>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = raw.strip()
    if len(raw) > 300:
        raw = raw[:300]
    return raw


def _fallback_summary(description: str) -> str:
    """Return a conservative fallback summary when ERE fails."""
    desc = (description or "").strip()
    if not desc:
        return "No summary available."
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", desc) if s.strip()]
    return " ".join(sentences[:3])[:300]


def extract_entities_and_summary(
    title: str,
    description: str,
    llm: BaseChatModel,
) -> ArticleExtraction | tuple[str, bool]:
    """Run structured ERE on a single article.

    Args:
        title: Article title.
        description: Article description/body.
        llm: A LangChain chat model configured for structured output.

    Returns:
        On success: an ``ArticleExtraction`` with summary + triplets.
        On failure: a tuple ``(summary_text, False)`` so the pipeline can
        keep running with a summary-only fallback.
    """
    content = (description or "")[:800]
    prompt = _build_prompt(title, content)

    structured_llm = llm.with_structured_output(
        ArticleExtraction,
        include_raw=True,
        method="json_mode",
    )

    for attempt in range(2):
        try:
            response = structured_llm.invoke(prompt)
            if isinstance(response, dict):
                parsed = response.get("parsed")
                if parsed is None and "raw" in response:
                    raw = response.get("raw")
                    logger.warning("ERE parsed None for '%s...' (attempt %s)", title[:40], attempt + 1)
                    if raw:
                        logger.debug("Raw response: %s", raw)
                    continue
            else:
                parsed = response

            if isinstance(parsed, ArticleExtraction):
                parsed.summary = _clean_summary(parsed.summary)
                parsed.triplets = parsed.triplets[:10]
                return parsed

            # If the LLM returned a plain dict, try to coerce it.
            if isinstance(parsed, dict):
                parsed = ArticleExtraction(**parsed)
                parsed.summary = _clean_summary(parsed.summary)
                parsed.triplets = parsed.triplets[:10]
                return parsed

            logger.warning("ERE returned unexpected type %s for '%s...'", type(parsed).__name__, title[:40])
        except Exception as e:  # noqa: BLE001 — ERE must never break the pipeline
            logger.warning(
                "ERE failed for '%s...' (attempt %s): %s",
                title[:40], attempt + 1, e,
            )

    # Final fallback: return a plain summary and mark AI extraction as failed.
    return _fallback_summary(description), False
