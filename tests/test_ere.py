"""test_ere.py — unit tests for entity/relation extraction.

Pure-function tests for schemas, entity normalization, prompt construction,
and structured-output handling.  No AWS, no network.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel

import ere
from ere import ArticleExtraction, Triplet, extract_entities_and_summary, normalize_entity


# ─── Triplet schema validation ───────────────────────────────────────────────


class TestTripletSchema:
    def test_valid_triplet(self) -> None:
        t = Triplet(
            subject="TSMC",
            relation="LOCATED_IN",
            object="Taiwan",
            subject_type="org",
            object_type="place",
        )
        assert t.subject_type == "org"
        assert t.object_type == "place"
        assert t.relation == "LOCATED_IN"

    def test_entity_types_are_normalised(self) -> None:
        t = Triplet(
            subject="Alice",
            relation="EMPLOYS",
            object="Bob",
            subject_type="Person",
            object_type="ORG",
        )
        assert t.subject_type == "person"
        assert t.object_type == "org"

    def test_invalid_entity_type_raises(self) -> None:
        with pytest.raises(ValueError):
            Triplet(
                subject="X",
                relation="AFFECTS",
                object="Y",
                subject_type="invalid",
                object_type="org",
            )

    def test_unknown_relation_normalized_to_affects(self) -> None:
        t = Triplet(
            subject="A",
            relation="MYSTERY_LINK",
            object="B",
            subject_type="org",
            object_type="org",
        )
        assert t.relation == "AFFECTS"

    def test_relation_spacing_normalized(self) -> None:
        t = Triplet(
            subject="A",
            relation="competes with",
            object="B",
            subject_type="org",
            object_type="org",
        )
        assert t.relation == "COMPETES_WITH"


# ─── ArticleExtraction schema ───────────────────────────────────────────────


class TestArticleExtractionSchema:
    def test_summary_required(self) -> None:
        with pytest.raises(ValueError):
            ArticleExtraction(summary="")

    def test_triplets_optional(self) -> None:
        a = ArticleExtraction(summary="A short summary here.")
        assert a.triplets == []


# ─── Entity normalization ────────────────────────────────────────────────────


class TestNormalizeEntity:
    def test_lowercase_and_trim(self) -> None:
        assert normalize_entity("  Taiwan  ") == "taiwan"

    def test_strips_inc(self) -> None:
        assert normalize_entity("TSMC Inc.") == "tsmc"

    def test_strips_corp(self) -> None:
        assert normalize_entity("Apple Corp.") == "apple"

    def test_strips_gmbh_and_llc(self) -> None:
        assert normalize_entity("Acme GmbH") == "acme"
        assert normalize_entity("Acme LLC") == "acme"

    def test_no_suffix_stays_intact(self) -> None:
        assert normalize_entity("Taiwan") == "taiwan"

    def test_trailing_punctuation_removed(self) -> None:
        assert normalize_entity("Taiwan.") == "taiwan"

    def test_whitespace_collapsed(self) -> None:
        assert normalize_entity("Taiwan   Semiconductor") == "taiwan semiconductor"

    def test_empty_string(self) -> None:
        assert normalize_entity("") == ""


# ─── Prompt construction ─────────────────────────────────────────────────────


class TestPromptConstruction:
    def test_prompt_contains_title_and_content(self) -> None:
        prompt = ere._build_prompt("My Title", "My content here.")
        assert "My Title" in prompt
        assert "My content here." in prompt
        assert "summary" in prompt
        assert "triplets" in prompt

    def test_prompt_includes_few_shot_examples(self) -> None:
        prompt = ere._build_prompt("T", "C")
        assert "TSMC" in prompt
        assert "EU Parliament" in prompt
        assert "Apple" in prompt


# ─── Mock LLM for structured output tests ───────────────────────────────────


class FakeLLM(BaseChatModel):
    """A tiny LangChain-compatible fake for testing structured output paths."""

    responses: list[dict] = []
    call_count: int = 0

    def __init__(self, responses: list[dict] | None = None, **kwargs):
        super().__init__(**kwargs)
        if responses is not None:
            self.responses = responses
        self.call_count = 0

    def _generate(self, *args, **kwargs):
        raise NotImplementedError

    def _llm_type(self) -> str:
        return "fake"

    @property
    def _identifying_params(self) -> dict:
        return {}

    def with_structured_output(self, schema, *, include_raw: bool = False, **kwargs):
        self.call_count = 0
        self._schema = schema
        self._include_raw = include_raw
        return self

    def invoke(self, prompt, **kwargs):
        if self.call_count >= len(self.responses):
            raise RuntimeError("No more mock responses")
        resp = self.responses[self.call_count]
        self.call_count += 1
        if include_raw := getattr(self, "_include_raw", False):
            return {"parsed": resp, "raw": resp}
        return resp


# ─── extract_entities_and_summary ───────────────────────────────────────────


class TestExtractEntitiesAndSummary:
    def test_returns_extraction_on_valid_response(self) -> None:
        fake = FakeLLM([{
            "summary": "Apple ordered more chips from TSMC in Taiwan.",
            "triplets": [
                {
                    "subject": "Apple",
                    "subject_type": "org",
                    "relation": "SUPPLIES",
                    "object": "TSMC",
                    "object_type": "org",
                },
                {
                    "subject": "TSMC",
                    "subject_type": "org",
                    "relation": "LOCATED_IN",
                    "object": "Taiwan",
                    "object_type": "place",
                },
            ],
        }])
        result = extract_entities_and_summary("Title", "Description", fake)
        assert isinstance(result, ArticleExtraction)
        assert "Apple ordered" in result.summary
        assert len(result.triplets) == 2
        assert result.triplets[0].relation == "SUPPLIES"

    def test_retries_then_falls_back_on_failure(self) -> None:
        fake = FakeLLM([
            RuntimeError("parse failure"),
            RuntimeError("parse failure again"),
        ])
        result = extract_entities_and_summary("Title", "Description text here.", fake)
        assert isinstance(result, tuple)
        assert result[1] is False
        assert "Description text here." in result[0]

    def test_retry_succeeds_on_second_attempt(self) -> None:
        fake = FakeLLM([
            RuntimeError("bad first response"),
            {
                "summary": "Summary on retry.",
                "triplets": [
                    {
                        "subject": "X",
                        "subject_type": "org",
                        "relation": "AFFECTS",
                        "object": "Y",
                        "object_type": "org",
                    },
                ],
            },
        ])
        result = extract_entities_and_summary("Title", "Desc", fake)
        assert isinstance(result, ArticleExtraction)
        assert result.summary == "Summary on retry."
        assert len(result.triplets) == 1

    def test_caps_triplets_at_ten(self) -> None:
        fake = FakeLLM([{
            "summary": "This is a deliberately longer summary to satisfy the schema.",
            "triplets": [
                {
                    "subject": f"Entity{i}",
                    "subject_type": "org",
                    "relation": "AFFECTS",
                    "object": "Other",
                    "object_type": "org",
                }
                for i in range(15)
            ],
        }])
        result = extract_entities_and_summary("Title", "Desc", fake)
        assert isinstance(result, ArticleExtraction)
        assert len(result.triplets) == 10


# ─── Fallback summary helper ─────────────────────────────────────────────────


class TestFallbackSummary:
    def test_empty_description(self) -> None:
        assert "No summary" in ere._fallback_summary("")

    def test_truncates_to_three_sentences(self) -> None:
        desc = "One. Two. Three. Four. Five."
        result = ere._fallback_summary(desc)
        assert "One. Two. Three." in result
        assert "Four" not in result
