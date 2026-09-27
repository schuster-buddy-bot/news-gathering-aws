"""test_core.py — unit tests for the pure functions of the news pipeline.

Covers dedup/fingerprinting, keyword filtering, summary cleaning,
embeddings (hashing + Base64 packing + cosine similarity) and the PDF
text cleaner. All functions are pure: no AWS, no network.
"""

from __future__ import annotations

import base64
import struct

import pytest

from embeddings import DEFAULT_DIMS, cosine_similarity, hashed_embedding, pack_base64, unpack_base64
from lambda_handler import article_fingerprint, clean_summary, matches_filters, normalize_title
from pdf_generator import _clean_text as _clean_for_pdf


# ─── article_fingerprint ─────────────────────────────────────────────────────

class TestArticleFingerprint:
    def test_deterministic(self) -> None:
        assert article_fingerprint("Some Title", "https://x.example/a") == \
            article_fingerprint("Some Title", "https://x.example/a")

    def test_different_title_different_hash(self) -> None:
        assert article_fingerprint("Title A", "https://x.example/a") != \
            article_fingerprint("Title B", "https://x.example/a")

    def test_different_url_different_hash(self) -> None:
        assert article_fingerprint("Same Title", "https://x.example/a") != \
            article_fingerprint("Same Title", "https://x.example/b")

    def test_case_and_whitespace_insensitive(self) -> None:
        # normalize_title lowercases + strips whitespace, so these collide.
        assert article_fingerprint("  Same TITLE  ", "https://x.example/a") == \
            article_fingerprint("same title", "https://x.example/a")

    def test_sha256_hex_digest(self) -> None:
        fp = article_fingerprint("Title", "https://x.example/a")
        assert len(fp) == 64
        int(fp, 16)  # raises ValueError if not hex


# ─── normalize_title ────────────────────────────────────────────────────────

class TestNormalizeTitle:
    def test_strips_whitespace(self) -> None:
        assert normalize_title("   padded title   ") == "padded title"

    def test_lowercases(self) -> None:
        assert normalize_title("Mixed CASE Title") == "mixed case title"

    def test_removes_punctuation(self) -> None:
        assert normalize_title("Hello, World! (2026)") == "hello world 2026"

    def test_collapses_whitespace(self) -> None:
        assert normalize_title("a\tb\n\nc   d") == "a b c d"

    def test_empty_string(self) -> None:
        assert normalize_title("") == ""


# ─── matches_filters ────────────────────────────────────────────────────────

class TestMatchesFilters:
    def test_include_match_passes(self, filters: dict) -> None:
        article = {"title": "New AI model released", "description": "It is fast."}
        assert matches_filters(article, filters) is True

    def test_include_in_description_passes(self, filters: dict) -> None:
        article = {"title": "Something happened", "description": "Advances in machine learning continue."}
        assert matches_filters(article, filters) is True

    def test_no_include_match_fails(self, filters: dict) -> None:
        article = {"title": "Local sports results", "description": "The team won."}
        assert matches_filters(article, filters) is False

    def test_exclude_keyword_wins(self, filters: dict) -> None:
        article = {"title": "AI casino giveaway", "description": "Free chips!"}
        assert matches_filters(article, filters) is False

    def test_exclude_case_insensitive(self, filters: dict) -> None:
        article = {"title": "AI GIVEAWAY", "description": ""}
        assert matches_filters(article, filters) is False

    def test_empty_include_is_passthrough(self) -> None:
        article = {"title": "Anything at all", "description": "No constraints."}
        assert matches_filters(article, {"include_keywords": [], "exclude_keywords": []}) is True

    def test_empty_include_still_respects_exclude(self) -> None:
        article = {"title": "Spam casino spam", "description": ""}
        assert matches_filters(article, {"include_keywords": [], "exclude_keywords": ["casino"]}) is False

    def test_missing_include_key_is_passthrough(self) -> None:
        article = {"title": "Anything", "description": ""}
        assert matches_filters(article, {"exclude_keywords": []}) is True


# ─── clean_summary ──────────────────────────────────────────────────────────

class TestCleanSummary:
    def test_strips_html(self) -> None:
        raw = "<p>This is a solid finding.</p> Another point stands."
        result = clean_summary(raw)
        assert "<p>" not in result
        assert "This is a solid finding" in result

    def test_removes_thinking_block(self) -> None:
        raw = "<thinking>internal monologue</thinking>The model works well. It is fast."
        result = clean_summary(raw)
        assert "internal monologue" not in result
        assert "model works well" in result

    def test_truncates_to_300_chars(self) -> None:
        raw = ". ".join([f"Sentence number {i} is here." for i in range(40)])
        result = clean_summary(raw)
        assert len(result) <= 300

    def test_drops_meta_sentences(self) -> None:
        raw = "We need to consider the article. The result holds. It is verified."
        result = clean_summary(raw)
        assert "We need" not in result
        assert "It is verified" in result

    def test_unusable_input_returns_empty(self) -> None:
        assert clean_summary("We need to know the user. The user asked.") == ""


# ─── hashed_embedding ───────────────────────────────────────────────────────

class TestHashedEmbedding:
    def test_deterministic(self) -> None:
        v1 = hashed_embedding("alpha beta gamma")
        v2 = hashed_embedding("alpha beta gamma")
        assert v1 == v2

    def test_dimension_default_256(self) -> None:
        assert len(hashed_embedding("hello world")) == DEFAULT_DIMS

    def test_dimension_override(self) -> None:
        assert len(hashed_embedding("hello world", dims=64)) == 64

    def test_empty_string_returns_zero_vector(self) -> None:
        vec = hashed_embedding("")
        assert len(vec) == DEFAULT_DIMS
        assert all(v == 0.0 for v in vec)

    def test_whitespace_only_returns_zero_vector(self) -> None:
        vec = hashed_embedding("   \t\n  ")
        assert all(v == 0.0 for v in vec)

    def test_nonempty_input_is_unit_vector(self) -> None:
        vec = hashed_embedding("alpha beta")
        norm = sum(v * v for v in vec) ** 0.5
        assert norm == pytest.approx(1.0)


# ─── pack_base64 / unpack_base64 ────────────────────────────────────────────

class TestBase64RoundTrip:
    def test_round_trip(self) -> None:
        vec = [0.0, 1.5, -2.25, 3.125, 1000.0, -0.001]
        assert unpack_base64(pack_base64(vec)) == pytest.approx(vec, rel=1e-6)

    def test_produces_valid_base64(self) -> None:
        blob = pack_base64([0.5, 0.5])
        base64.b64decode(blob, validate=True)  # raises on invalid input

    def test_pack_is_little_endian_float32(self) -> None:
        blob = pack_base64([1.0])
        assert blob == base64.b64encode(struct.pack("<f", 1.0)).decode("ascii")

    def test_unpack_rejects_malformed(self) -> None:
        with pytest.raises(ValueError):
            unpack_base64(base64.b64encode(b"abc").decode("ascii"))  # 3 bytes, not %4

    def test_unpack_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            unpack_base64("")


# ─── cosine_similarity ──────────────────────────────────────────────────────

class TestCosineSimilarity:
    def test_orthogonal_vectors_zero(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_identical_vectors_one(self) -> None:
        assert cosine_similarity([0.6, 0.8], [0.6, 0.8]) == pytest.approx(1.0)

    def test_opposite_vectors_minus_one(self) -> None:
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_length_mismatch_returns_zero(self) -> None:
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0

    def test_empty_vectors_return_zero(self) -> None:
        assert cosine_similarity([], []) == 0.0


# ─── _clean_for_pdf ─────────────────────────────────────────────────────────

class TestCleanForPdf:
    def test_strips_html_tags(self) -> None:
        assert _clean_for_pdf("<p>Hello <b>world</b></p>") == "Hello world"

    def test_collapses_whitespace(self) -> None:
        assert _clean_for_pdf("a   b\t\tc") == "a b c"

    def test_plain_text_untouched(self) -> None:
        assert _clean_for_pdf("no markup here") == "no markup here"

    def test_empty_string(self) -> None:
        assert _clean_for_pdf("") == ""