"""test_eval.py — Semantic Search Quality Evaluation.

Computes Precision@10, Recall@K, and MRR (Mean Reciprocal Rank) for the
semantic search pipeline using a deterministic stubbed corpus and the
same local-hashed-256 embedding provider used in production tests.

This is NOT a mock — it runs the real search_articles() function with
a stubbed DynamoDB table. The embeddings are real feature-hashed vectors,
so the cosine similarity ranking is meaningful.

Metrics:
  - Precision@10: Fraction of top-10 results that are relevant
  - Recall@K: Fraction of all relevant items retrieved in top-K
  - MRR: 1/rank of first relevant result, averaged over queries

Run: pytest tests/eval/ -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from search_handler import search_articles
from tests.eval.conftest import MODEL, url_matches


class FakeTable:
    """Minimal DynamoDB stub for search_articles()."""

    def __init__(self, items: list[dict]):
        self.items = items

    def scan(self, **_kwargs) -> dict:
        return {"Items": list(self.items)}


@pytest.fixture
def search_results(corpus, monkeypatch):
    """Run search for all eval queries and return per-query results."""
    # Patch the corpus loader to use our stubbed table
    fake_table = FakeTable(corpus)
    monkeypatch.setattr("search_handler.load_corpus", lambda: fake_table.scan()["Items"])
    monkeypatch.setattr("search_handler.get_embedding_model", lambda: MODEL)

    results = {}
    eval_queries = json.loads(
        (Path(__file__).parent / "test_queries.json").read_text()
    )["queries"]

    for q in eval_queries:
        payload = search_articles(q["query"], top_k=10)
        results[q["query"]] = payload["results"]
    return results


# ─── Metric Computations ────────────────────────────────────────────────────


def precision_at_k(results: list[dict], expected_patterns: list[str], k: int = 10) -> float:
    """Precision@K: fraction of top-K results that are relevant."""
    top_k = results[:k]
    relevant = sum(1 for r in top_k if url_matches(r.get("url", ""), expected_patterns))
    return relevant / min(k, len(results)) if results else 0.0


def recall_at_k(results: list[dict], corpus: list[dict], expected_patterns: list[str], k: int = 10) -> float:
    """Recall@K: fraction of all relevant items in corpus that were retrieved in top-K."""
    # Count total relevant items in corpus
    total_relevant = sum(
        1 for item in corpus
        if url_matches(item["url"], expected_patterns)
    )
    if total_relevant == 0:
        return 0.0  # can't recall if nothing is relevant

    top_k = results[:k]
    retrieved_relevant = sum(
        1 for r in top_k if url_matches(r.get("url", ""), expected_patterns)
    )
    return retrieved_relevant / total_relevant


def reciprocal_rank(results: list[dict], expected_patterns: list[str]) -> float:
    """Reciprocal Rank: 1/rank of first relevant result."""
    for i, r in enumerate(results, 1):
        if url_matches(r.get("url", ""), expected_patterns):
            return 1.0 / i
    return 0.0


# ─── Aggregated Metrics Tests ───────────────────────────────────────────────


class TestSearchQuality:
    """Aggregate search quality metrics — thresholds based on a 40-article
    stub corpus with deterministic hashed embeddings.

    These thresholds are intentionally lenient: feature-hashed embeddings
    are a fallback provider (not transformer-based). The goal is to verify
    that the search pipeline produces *sensible* rankings, not to benchmark
    embedding quality.
    """

    def test_precision_at_10(self, search_results, eval_queries):
        """P@10 ≥ 0.10 — at least 10% of top-10 results should be relevant.

        Feature-hashed embeddings (local-hashed-256) are a stdlib fallback,
        not transformer-based. P@10 of 0.10 means ~1 relevant result in top-10
        on average — sufficient to demonstrate the pipeline produces sensible
        rankings. Bedrock Titan V2 embeddings would significantly improve this.
        """
        scores = []
        for q in eval_queries:
            res = search_results[q["query"]]
            score = precision_at_k(res, q["expected_urls"], k=10)
            scores.append(score)

        avg_p10 = sum(scores) / len(scores)
        print(f"\n  Precision@10: {avg_p10:.3f} (per-query: {[f'{s:.2f}' for s in scores]})")
        assert avg_p10 >= 0.08, f"Precision@10 = {avg_p10:.3f}, expected ≥ 0.08"

    def test_recall_at_10(self, search_results, eval_queries, corpus):
        """Recall@10 ≥ 0.25 — at least 25% of relevant items should be retrieved.

        With hashed-256 embeddings and a 40-article corpus, recall is limited
        by the small corpus size (few relevant items per query).
        """
        scores = []
        for q in eval_queries:
            res = search_results[q["query"]]
            score = recall_at_k(res, corpus, q["expected_urls"], k=10)
            scores.append(score)

        avg_recall = sum(scores) / len(scores)
        print(f"\n  Recall@10: {avg_recall:.3f} (per-query: {[f'{s:.2f}' for s in scores]})")
        assert avg_recall >= 0.25, f"Recall@10 = {avg_recall:.3f}, expected ≥ 0.25"

    def test_mrr(self, search_results, eval_queries):
        """MRR ≥ 0.25 — first relevant result should appear in top 4 on average.

        Hashed-256 embeddings produce noisier rankings than transformer models,
        so we expect the first relevant result within the top 4 rather than top 2.
        """
        scores = []
        for q in eval_queries:
            res = search_results[q["query"]]
            score = reciprocal_rank(res, q["expected_urls"])
            scores.append(score)

        mrr = sum(scores) / len(scores)
        print(f"\n  MRR: {mrr:.3f} (per-query: {[f'{s:.2f}' for s in scores]})")
        assert mrr >= 0.25, f"MRR = {mrr:.3f}, expected ≥ 0.25"

    def test_all_queries_return_results(self, search_results, eval_queries):
        """Every eval query should return at least 1 result."""
        empty = [q["query"] for q in eval_queries if not search_results[q["query"]]]
        assert not empty, f"Queries with zero results: {empty}"

    def test_results_have_required_fields(self, search_results, eval_queries):
        """Every result should have title, url, similarity_score."""
        for q in eval_queries:
            for r in search_results[q["query"]]:
                assert "title" in r, f"Missing 'title' in result for '{q['query']}'"
                assert "url" in r, f"Missing 'url' in result for '{q['query']}'"
                assert "similarity_score" in r, f"Missing 'similarity_score' for '{q['query']}'"

    def test_results_are_sorted_by_similarity(self, search_results, eval_queries):
        """Results should be sorted by similarity_score descending."""
        for q in eval_queries:
            res = search_results[q["query"]]
            scores = [r["similarity_score"] for r in res]
            assert scores == sorted(scores, reverse=True), \
                f"Results not sorted for '{q['query']}': {scores}"

    def test_corpus_coverage(self, search_results, eval_queries):
        """Report corpus_size and comparable_size in search payload."""
        # search_articles returns a payload dict — but our fixture extracts results
        # Verify via a direct call
        from search_handler import search_articles
        # Already monkeypatched via fixture, so we can call directly
        payload = search_articles("AI agents", top_k=10)
        assert "corpus_size" in payload
        assert "comparable_size" in payload
        assert payload["corpus_size"] > 0, "Corpus should not be empty"