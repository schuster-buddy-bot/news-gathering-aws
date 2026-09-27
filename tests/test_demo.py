"""test_demo.py — unit tests for the public read-only demo Lambda (Issue #8).

Covers all /demo/* routes, the shared search core, the no-write guardrail
and CORS/robustness. All AWS dependencies are stubbed: no network, no AWS.
"""

from __future__ import annotations

import base64
import json

import pytest

import api_handler
import demo_handler
import search_handler
from embeddings import hashed_embedding, pack_base64

MODEL = "local-hashed-256"


def embed(text: str, model: str) -> list[float]:
    """Deterministic stub embedding (same provider for query + corpus)."""
    assert model == MODEL
    return hashed_embedding(text, 256)


# ─── Stub AWS pieces ─────────────────────────────────────────────────────────


class FakeArticlesTable:
    """DynamoDB articles table stub (scan only, single page)."""

    def __init__(self, items: list[dict]):
        self.items = items

    def scan(self, **kwargs):
        values = kwargs.get("ExpressionAttributeValues") or {}
        out = list(self.items)
        if ":cat" in values:
            out = [item for item in out if item.get("category") == values[":cat"]]
        return {"Items": out}


class FakeBody:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self) -> bytes:
        return self.payload


class FakeTopicsS3:
    """S3 client stub for the topics config (counts get_object calls)."""

    def __init__(self, payload: dict | None = None, fail: bool = False):
        self.payload = payload
        self.fail = fail
        self.calls = 0

    def get_object(self, Bucket: str, Key: str) -> dict:
        self.calls += 1
        if self.fail:
            raise OSError("s3 unavailable")
        return {"Body": FakeBody(json.dumps(self.payload).encode("utf-8"))}


class FakeReportsTable:
    """DynamoDB reports table stub (scan only)."""

    def __init__(self, items: list[dict]):
        self.items = items

    def scan(self, **_kwargs) -> dict:
        return {"Items": list(self.items)}


class FakePresignS3:
    def generate_presigned_url(self, _op: str, Params: dict, ExpiresIn: int) -> str:
        return (f"https://s3.eu-central-1.amazonaws.com/{Params['Bucket']}"
                f"/{Params['Key']}?expires={ExpiresIn}")


# ─── Fixtures + event helpers ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_topics_cache():
    demo_handler._topics_cache = (None, 0.0)
    yield
    demo_handler._topics_cache = (None, 0.0)


@pytest.fixture
def corpus() -> list[dict]:
    """Six articles across categories with real packed embeddings."""
    articles = [
        ("AI agents take over support desks", "major", "2026-09-27", "TechDaily",
         "Agents now handle enterprise support tickets using large language models."),
        ("Machine learning powers new telescope", "major", "2026-09-27", "SciNews",
         "A machine learning pipeline classifies galaxies in real time."),
        ("EU Parliament passes AI Act amendments", "eu_parliament", "2026-09-27", "EU Press",
         "The EU Parliament tightened rules for general purpose AI models."),
        ("Bundestag debates cloud sovereignty", "bundestag", "2026-09-26", "BT Presse",
         "German lawmakers discussed European cloud architecture requirements."),
        ("UN peacekeeping adopts satellite AI", "un", "2026-09-26", "UN News",
         "UN missions use machine learning to monitor ceasefire zones."),
        ("Niche robotics startup funds DevOps tooling", "niche", "2026-09-25", "RobotWire",
         "A robotics startup open-sourced its DevOps deployment pipeline."),
    ]
    return [
        {
            "url_hash": f"hash{i}",
            "title": title,
            "url": f"https://example.com/{i}",
            "category": category,
            "first_seen": date,
            "source": source,
            "summary": summary,
            "embedding_model": MODEL,
            "embedding": pack_base64(hashed_embedding(f"{title} {summary}", 256)),
        }
        for i, (title, category, date, source, summary) in enumerate(articles)
    ]


@pytest.fixture
def patch_search(monkeypatch, corpus):
    """Stub the shared search core in BOTH namespaces (private + demo)."""
    for module in (search_handler, demo_handler):
        monkeypatch.setattr(module, "get_embedding_model", lambda: MODEL)
        monkeypatch.setattr(module, "load_corpus", lambda: corpus)
        monkeypatch.setattr(module, "embed_text", embed)


@pytest.fixture
def patch_reports(monkeypatch):
    monkeypatch.setattr(api_handler, "table", FakeReportsTable([
        {"date": "2026-09-27", "s3_key": "reports/2026-09-27-report.pdf",
         "article_count": 12, "generated_at": "2026-09-27T05:00:00Z"},
    ]))
    monkeypatch.setattr(api_handler, "s3", FakePresignS3())


def get_event(path: str = "/demo/search", qs: dict | None = None) -> dict:
    return {"httpMethod": "GET", "path": path, "queryStringParameters": qs or {}}


def post_event(path: str, payload, b64: bool = False) -> dict:
    body = json.dumps(payload) if not isinstance(payload, str) else payload
    if b64:
        body = base64.b64encode(body.encode()).decode("ascii")
    return {
        "httpMethod": "POST",
        "path": path,
        "body": body,
        "isBase64Encoded": b64,
        "headers": {"Content-Type": "application/json"},
    }


def body_of(resp: dict) -> dict:
    return json.loads(resp["body"])


def assert_cors(resp: dict) -> None:
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"
    assert resp["headers"]["Content-Type"] == "application/json"


# ─── GET /demo/search ────────────────────────────────────────────────────────


class TestDemoSearch:
    def test_missing_query_400(self, patch_search) -> None:
        resp = demo_handler.lambda_handler(get_event(), None)
        assert resp["statusCode"] == 400
        assert body_of(resp)["error"] == "missing_query"
        assert_cors(resp)

    def test_search_returns_results_no_key(self, patch_search) -> None:
        resp = demo_handler.lambda_handler(
            get_event(qs={"q": "machine learning telescope", "limit": "3"}), None)
        assert resp["statusCode"] == 200
        payload = body_of(resp)
        assert payload["count"] == len(payload["results"])
        assert payload["count"] >= 1
        assert payload["model"] == MODEL
        top = payload["results"][0]
        assert "Machine learning" in top["title"] or top["similarity_score"] > 0
        for key in ("title", "url", "category", "date", "source", "summary",
                    "similarity_score"):
            assert key in top
        assert_cors(resp)

    def test_same_format_as_private_search(self, patch_search) -> None:
        private = search_handler.lambda_handler(
            {"queryStringParameters": {"q": "AI agents", "limit": "5"}}, None)
        demo = demo_handler.lambda_handler(
            get_event(qs={"q": "AI agents", "limit": "5"}), None)
        assert demo["statusCode"] == 200
        assert body_of(demo) == body_of(private)

    def test_embedding_failure_502(self, patch_search, monkeypatch) -> None:
        def boom(text, model):
            raise RuntimeError("provider down")

        monkeypatch.setattr(search_handler, "embed_text", boom)
        resp = demo_handler.lambda_handler(get_event(qs={"q": "anything"}), None)
        assert resp["statusCode"] == 502
        assert body_of(resp)["error"] == "embedding_failed"

    def test_non_numeric_limit_falls_back(self, patch_search) -> None:
        resp = demo_handler.lambda_handler(
            get_event(qs={"q": "AI agents", "limit": "abc"}), None)
        assert resp["statusCode"] == 200
        payload = body_of(resp)
        assert payload["count"] == len(payload["results"])
        assert payload["count"] <= 10  # DEFAULT_TOP_K applied, no crash


# ─── GET /demo/report/latest ─────────────────────────────────────────────────


class TestDemoReportLatest:
    def test_returns_presigned_url_no_key(self, patch_reports) -> None:
        resp = demo_handler.lambda_handler(get_event("/demo/report/latest"), None)
        assert resp["statusCode"] == 200
        payload = body_of(resp)
        assert payload["date"] == "2026-09-27"
        assert "s3.eu-central-1.amazonaws.com" in payload["url"]
        assert payload["expires_in"] == api_handler.PRESIGN_TTL
        assert_cors(resp)

    def test_404_when_no_reports(self, monkeypatch) -> None:
        monkeypatch.setattr(api_handler, "table", FakeReportsTable([]))
        resp = demo_handler.lambda_handler(get_event("/demo/report/latest"), None)
        assert resp["statusCode"] == 404
        assert body_of(resp)["error"] == "no_reports_yet"

    def test_reuses_api_handler_logic(self, patch_reports) -> None:
        demo = demo_handler.lambda_handler(get_event("/demo/report/latest"), None)
        private = api_handler.lambda_handler({"path": "/report/latest"}, None)
        assert body_of(demo) == body_of(private)


# ─── GET /demo/topics ────────────────────────────────────────────────────────


class TestDemoTopics:
    def test_topics_from_s3(self, monkeypatch) -> None:
        config = {"topics": ["AI", "climate"], "categories": ["un", "major"],
                  "updated_at": "2026-09-27"}
        monkeypatch.setattr(demo_handler, "s3_client", FakeTopicsS3(config))
        resp = demo_handler.lambda_handler(get_event("/demo/topics"), None)
        assert resp["statusCode"] == 200
        payload = body_of(resp)
        assert payload["topics"] == ["AI", "climate"]
        assert payload["source"] == "s3"
        assert payload["categories"][0] == {"id": "un", "label": "un"}
        assert_cors(resp)

    def test_topics_fallback_on_s3_error(self, monkeypatch) -> None:
        monkeypatch.setattr(demo_handler, "s3_client", FakeTopicsS3(fail=True))
        resp = demo_handler.lambda_handler(get_event("/demo/topics"), None)
        assert resp["statusCode"] == 200
        payload = body_of(resp)
        assert payload["source"] == "default"
        assert payload["topics"] == demo_handler.DEFAULT_TOPICS
        assert len(payload["categories"]) == 7

    def test_topics_are_cached_across_calls(self, monkeypatch) -> None:
        fake = FakeTopicsS3({"topics": ["AI", "climate"]})
        monkeypatch.setattr(demo_handler, "s3_client", fake)
        demo_handler.lambda_handler(get_event("/demo/topics"), None)
        demo_handler.lambda_handler(get_event("/demo/topics"), None)
        assert fake.calls == 1  # second call served from warm cache

    def test_topics_capped_at_max(self, monkeypatch) -> None:
        fake = FakeTopicsS3({"topics": [f"topic{i}" for i in range(15)]})
        monkeypatch.setattr(demo_handler, "s3_client", fake)
        resp = demo_handler.lambda_handler(get_event("/demo/topics"), None)
        assert len(body_of(resp)["topics"]) == demo_handler.MAX_TOPICS

    def test_invalid_category_entries_dropped(self, monkeypatch) -> None:
        config = {"topics": ["AI"], "categories": [{"id": "un", "label": "UN"}, "", {"nope": 1}]}
        monkeypatch.setattr(demo_handler, "s3_client", FakeTopicsS3(config))
        resp = demo_handler.lambda_handler(get_event("/demo/topics"), None)
        cats = body_of(resp)["categories"]
        assert {"id": "un", "label": "UN"} in cats
        assert all(isinstance(c.get("id"), str) and c["id"] for c in cats)


# ─── POST /demo/search-by-topics ─────────────────────────────────────────────


class TestDemoSearchByTopics:
    def test_multi_topic_aggregation(self, patch_search) -> None:
        resp = demo_handler.lambda_handler(
            post_event("/demo/search-by-topics", {"topics": ["AI agents", "Bundestag cloud"]}),
            None)
        assert resp["statusCode"] == 200
        payload = body_of(resp)
        assert payload["model"] == MODEL
        assert [g["topic"] for g in payload["topics"]] == ["AI agents", "Bundestag cloud"]
        for group in payload["topics"]:
            assert group["count"] == len(group["results"])
            assert group["count"] >= 1
            assert all("similarity_score" in r for r in group["results"])
        assert payload["total_results"] == sum(g["count"] for g in payload["topics"])
        assert_cors(resp)

    def test_per_topic_top_k_respected(self, patch_search) -> None:
        resp = demo_handler.lambda_handler(
            post_event("/demo/search-by-topics", {"topics": ["machine learning", "AI"],
                                                  "per_topic": 1}),
            None)
        payload = body_of(resp)
        assert all(len(g["results"]) <= 1 for g in payload["topics"])

    def test_dedupes_case_insensitive(self, patch_search) -> None:
        resp = demo_handler.lambda_handler(
            post_event("/demo/search-by-topics", {"topics": ["AI", " ai", "ai"]}), None)
        payload = body_of(resp)
        assert len(payload["topics"]) == 1
        assert payload["topics"][0]["topic"] == "AI"

    def test_missing_body_topics_400(self, patch_search) -> None:
        resp = demo_handler.lambda_handler(post_event("/demo/search-by-topics", {}), None)
        assert resp["statusCode"] == 400
        assert body_of(resp)["error"] == "missing_topics"

    def test_invalid_json_400(self, patch_search) -> None:
        resp = demo_handler.lambda_handler(post_event("/demo/search-by-topics", "{not json"),
                                           None)
        assert resp["statusCode"] == 400
        assert body_of(resp)["error"] == "invalid_json"

    def test_base64_body_accepted(self, patch_search) -> None:
        resp = demo_handler.lambda_handler(
            post_event("/demo/search-by-topics", {"topics": ["AI agents"]}, b64=True), None)
        assert resp["statusCode"] == 200
        assert body_of(resp)["topics"][0]["topic"] == "AI agents"

    def test_empty_topics_400(self, patch_search) -> None:
        resp = demo_handler.lambda_handler(post_event("/demo/search-by-topics",
                                                       {"topics": ["  ", ""]}), None)
        assert resp["statusCode"] == 400

    def test_too_many_topics_400(self, patch_search) -> None:
        resp = demo_handler.lambda_handler(
            post_event("/demo/search-by-topics",
                       {"topics": [f"t{i}" for i in range(demo_handler.MAX_TOPICS + 1)]}),
            None)
        assert resp["statusCode"] == 400
        assert body_of(resp)["error"] == "too_many_topics"

    def test_embedding_failure_502(self, patch_search, monkeypatch) -> None:
        def boom(text, model):
            raise RuntimeError("provider down")

        monkeypatch.setattr(demo_handler, "embed_text", boom)
        resp = demo_handler.lambda_handler(
            post_event("/demo/search-by-topics", {"topics": ["AI"]}), None)
        assert resp["statusCode"] == 502


# ─── GET /demo/browse + /demo/articles ───────────────────────────────────────


class TestDemoBrowse:
    def test_counts_per_category(self, corpus, monkeypatch) -> None:
        monkeypatch.setattr(demo_handler, "articles_table", FakeArticlesTable(corpus))
        resp = demo_handler.lambda_handler(get_event("/demo/browse"), None)
        assert resp["statusCode"] == 200
        payload = body_of(resp)
        by_id = {c["id"]: c["count"] for c in payload["categories"]}
        assert by_id["un"] == 1
        assert by_id["bundestag"] == 1
        assert payload["total"] == len(corpus)
        assert_cors(resp)

    def test_counts_include_unknown_categories(self, corpus, monkeypatch) -> None:
        extra = dict(corpus[0], category="weird_cat")
        monkeypatch.setattr(demo_handler, "articles_table", FakeArticlesTable(corpus + [extra]))
        resp = demo_handler.lambda_handler(get_event("/demo/browse"), None)
        by_id = {c["id"]: c["count"] for c in body_of(resp)["categories"]}
        assert by_id["weird_cat"] == 1


class TestDemoArticles:
    def test_filtered_sorted_limited(self, corpus, monkeypatch) -> None:
        monkeypatch.setattr(demo_handler, "articles_table", FakeArticlesTable(corpus))
        resp = demo_handler.lambda_handler(
            get_event("/demo/articles", qs={"category": "major", "limit": "1"}), None)
        assert resp["statusCode"] == 200
        payload = body_of(resp)
        assert payload["category"] == "major"
        assert payload["count"] == 1
        assert "similarity_score" not in payload["articles"][0]
        # latest first_seen wins among both "major" items (2026-09-27)
        assert payload["articles"][0]["date"] == "2026-09-27"

    def test_missing_category_400(self, monkeypatch) -> None:
        monkeypatch.setattr(demo_handler, "articles_table", FakeArticlesTable([]))
        resp = demo_handler.lambda_handler(get_event("/demo/articles"), None)
        assert resp["statusCode"] == 400
        assert body_of(resp)["error"] == "missing_category"

    def test_limit_clamped_to_max(self, corpus, monkeypatch) -> None:
        monkeypatch.setattr(demo_handler, "articles_table", FakeArticlesTable(corpus))
        resp = demo_handler.lambda_handler(
            get_event("/demo/articles", qs={"category": "major", "limit": "99999"}), None)
        assert resp["statusCode"] == 200  # clamped, not a crash


# ─── Routing + read-only guardrail ───────────────────────────────────────────


class TestRouting:
    def test_unknown_path_404(self, patch_search) -> None:
        resp = demo_handler.lambda_handler(get_event("/demo/nope"), None)
        assert resp["statusCode"] == 404

    def test_non_get_post_405(self, patch_search) -> None:
        resp = demo_handler.lambda_handler(
            {"httpMethod": "DELETE", "path": "/demo/search"}, None)
        assert resp["statusCode"] == 405

    def test_v1_prefix_normalized(self, monkeypatch) -> None:
        fake = FakeTopicsS3({"topics": ["AI"]})
        monkeypatch.setattr(demo_handler, "s3_client", fake)
        resp = demo_handler.lambda_handler(get_event("/v1/demo/topics"), None)
        assert resp["statusCode"] == 200
        assert body_of(resp)["topics"] == ["AI"]


class TestReadOnlyGuardrail:
    """The demo surface must be read-only by construction (Issue #8)."""

    FORBIDDEN = (
        "put_item", "put_object", "delete_item", "update_item",
        "batch_write_item", "delete_object", "copy_object",
        "put_parameter", "delete_parameter",
    )

    def test_no_write_operations_in_demo_module(self) -> None:
        with open(demo_handler.__file__, encoding="utf-8") as fh:
            source = fh.read().lower()
        for token in self.FORBIDDEN:
            assert token not in source, f"write op found in demo_handler: {token}"

    def test_demo_lambda_handler_only_reads(self, patch_search, patch_reports, monkeypatch) -> None:
        # Smoke: every route callable returns via read-only stubs (no writes
        # possible because the stubs implement none) — full-route sweep.
        monkeypatch.setattr(demo_handler, "articles_table", FakeArticlesTable([]))
        monkeypatch.setattr(demo_handler, "s3_client", FakeTopicsS3({"topics": ["AI"]}))
        for event in (
            get_event("/demo/search", qs={"q": "AI"}),
            get_event("/demo/report/latest"),
            get_event("/demo/topics"),
            post_event("/demo/search-by-topics", {"topics": ["AI"]}),
            get_event("/demo/browse"),
            get_event("/demo/articles", qs={"category": "un"}),
        ):
            resp = demo_handler.lambda_handler(event, None)
            assert resp["statusCode"] in (200, 404), (event["path"], resp["statusCode"])
            assert_cors(resp)