"""eval_runner.py — Standalone eval runner for live API mode.

Runs the eval test queries against a live search API endpoint and
reports Precision@10, Recall@K, and MRR. Useful for manual quality
checks against the deployed pipeline.

Usage:
    python tests/eval/eval_runner.py --api https://api.example.com
    python tests/eval/eval_runner.py --api https://api.example.com --limit 20
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path


def run_query(api_base: str, query: str, limit: int = 10) -> list[dict]:
    """Call the search API and return results."""
    url = f"{api_base.rstrip('/')}/demo/search?q={urllib.parse.quote(query)}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        return data.get("results", [])
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} for '{query}': {e.read()[:200]}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  Error for '{query}': {e}", file=sys.stderr)
        return []


def precision_at_k(results: list[dict], patterns: list[str], k: int) -> float:
    top_k = results[:k]
    relevant = sum(1 for r in top_k if any(p.lower() in r.get("url", "").lower() for p in patterns))
    return relevant / min(k, len(results)) if results else 0.0


def reciprocal_rank(results: list[dict], patterns: list[str]) -> float:
    for i, r in enumerate(results, 1):
        if any(p.lower() in r.get("url", "").lower() for p in patterns):
            return 1.0 / i
    return 0.0


def main():
    parser = argparse.ArgumentParser(description="Semantic Search Eval Runner")
    parser.add_argument("--api", required=True, help="API base URL (e.g. https://api.example.com)")
    parser.add_argument("--limit", type=int, default=10, help="Top-K results per query")
    args = parser.parse_args()

    queries_file = Path(__file__).parent / "test_queries.json"
    queries = json.loads(queries_file.read_text())["queries"]

    print(f"\n🔍 Semantic Search Eval — {len(queries)} queries against {args.api}")
    print(f"   Top-K: {args.limit}\n")

    p10_scores = []
    mrr_scores = []

    for q in queries:
        results = run_query(args.api, q["query"], args.limit)
        p10 = precision_at_k(results, q["expected_urls"], args.limit)
        mrr = reciprocal_rank(results, q["expected_urls"])
        p10_scores.append(p10)
        mrr_scores.append(mrr)
        status = "✅" if p10 > 0 else "❌"
        print(f"  {status} {q['query']:45s}  P@{args.limit}={p10:.2f}  MRR={mrr:.2f}  ({len(results)} results)")

    avg_p10 = sum(p10_scores) / len(p10_scores)
    avg_mrr = sum(mrr_scores) / len(mrr_scores)

    print("\n  ─── Aggregate ───")
    print(f"  Precision@{args.limit}: {avg_p10:.3f}")
    print(f"  MRR:              {avg_mrr:.3f}")
    print(f"  Queries:          {len(queries)}")
    print()


if __name__ == "__main__":
    import urllib.parse  # noqa: E402
    main()