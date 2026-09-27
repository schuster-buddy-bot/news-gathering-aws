"""
demo_handler.py — public READ-ONLY demo Lambda (CQRS read side / BFF).

Thin facade over the existing private-API logic. No auth (no API key on the
/demo/* gateway routes), but every operation below is read-only (Scan /
GetObject / GetParameter / presign) and the demo IAM role grants no write
actions at all — write operations are impossible by construction.

Routes (REST API v1, apiKeyRequired = false, throttled at the gateway):
  GET  /demo/search?q=&limit=           semantic search (reuses search core)
  GET  /demo/report/latest              latest PDF report + presigned URL
  GET  /demo/topics                     predefined topics + category list
  POST /demo/search-by-topics           multi-topic search: top-K per topic
  GET  /demo/browse                     article counts per category
  GET  /demo/articles?category=&limit=  recent articles for one category

Environment variables (set by Terraform):
  ARTICLES_TABLE            DynamoDB articles table (PK: url_hash)
  REPORTS_TABLE             DynamoDB reports table (PK: date)   [api_handler]
  CONFIG_BUCKET             S3 bucket holding config/ + reports
  SSM_EMBEDDING_MODEL_PARAM SSM parameter with the embedding model/provider
  TOPICS_CONFIG_KEY         S3 key of the demo topics config (default
                            config/demo_topics.json), editable without redeploy
"""

import base64
import json
import logging
import os
import time
from collections import Counter

import boto3
from botocore.config import Config

from api_handler import latest_report
from search_handler import (
    EmbeddingError,
    MIN_SCORE,
    get_embedding_model,
    load_corpus,
    parse_search_params,
    search_articles,
)
from embeddings import cosine_similarity, embed_text, unpack_base64

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ARTICLES_TABLE_NAME = os.environ["ARTICLES_TABLE"]
CONFIG_BUCKET = os.environ["CONFIG_BUCKET"]
TOPICS_CONFIG_KEY = os.environ.get("TOPICS_CONFIG_KEY", "config/demo_topics.json")
TOPICS_CACHE_TTL = int(os.environ.get("TOPICS_CACHE_TTL_SECONDS", "300"))

MAX_TOPICS = 10
DEFAULT_PER_TOPIC = 5
MAX_PER_TOPIC = 10
DEFAULT_ARTICLE_LIMIT = 20
MAX_ARTICLE_LIMIT = 50

dynamodb = boto3.resource("dynamodb")
articles_table = dynamodb.Table(ARTICLES_TABLE_NAME)
# Virtual-host addressing: same SigV4 host-signature constraint as api_handler.
s3_client = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION", "eu-central-1"),
    config=Config(s3={"addressing_style": "virtual"}, signature_version="s3v4"),
)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
}

VERSION = "1.0"

DEFAULT_TOPICS = [
    "AI agents",
    "machine learning",
    "cloud architecture",
    "security",
    "DevOps",
    "EU AI Act",
]

DEFAULT_CATEGORIES = [
    {"id": "un", "label": "UN"},
    {"id": "eu_parliament", "label": "EU Parliament"},
    {"id": "bundestag", "label": "Bundestag"},
    {"id": "major", "label": "Major AI"},
    {"id": "niche", "label": "Niche"},
    {"id": "european", "label": "European"},
    {"id": "asian", "label": "Asian"},
]

# Warm-start cache for the topics config (module globals survive warm
# invocations; avoids an S3 round-trip on every /demo/topics call).
_topics_cache: tuple[dict | None, float] = (None, 0.0)


def _response(status_code: int, payload: dict) -> dict:
    """Build an API Gateway proxy response with CORS headers."""
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json.dumps(payload, ensure_ascii=False, default=str),
    }


def _route(event: dict) -> tuple[str, str]:
    """Extract (method, normalized_path) from a proxy event.

    Normalizes away a leading stage segment (``/v1``) so the handler works
    identically for REST proxy events and HTTP-API rawPath events.
    """
    method = (event.get("httpMethod")
              or (event.get("requestContext", {}).get("http", {}) or {}).get("method")
              or "GET")
    path = event.get("path") or event.get("rawPath") or "/"
    normalized = path.rstrip("/")
    if normalized.startswith("/v1/demo"):
        normalized = normalized[len("/v1"):]
    return method, normalized


def _article_result(item: dict, score: float | None = None) -> dict:
    """Shape an article item into the public result card (no internals)."""
    card = {
        "title": item.get("title", ""),
        "url": item.get("url", ""),
        "category": item.get("category", ""),
        "date": item.get("first_seen", ""),
        "source": item.get("source", ""),
        "summary": (item.get("summary") or "")[:220],
    }
    if score is not None:
        card["similarity_score"] = round(float(score), 4)
    return card


def _load_topics_config() -> dict:
    """Load demo topics + categories from S3 (cached), with safe defaults.

    Returns:
        Payload dict: {topics, categories, source, updated_at}.
    """
    global _topics_cache
    cached, cached_at = _topics_cache
    if cached is not None and (time.monotonic() - cached_at) < TOPICS_CACHE_TTL:
        return dict(cached)

    source = "default"
    updated_at = ""
    topics: list[str] = list(DEFAULT_TOPICS)
    categories: list[dict] = [dict(c) for c in DEFAULT_CATEGORIES]

    try:
        resp = s3_client.get_object(Bucket=CONFIG_BUCKET, Key=TOPICS_CONFIG_KEY)
        data = json.loads(resp["Body"].read().decode("utf-8"))
        raw_topics = data.get("topics")
        if isinstance(raw_topics, list):
            topics = [str(t).strip() for t in raw_topics if str(t).strip()]
        raw_cats = data.get("categories")
        if isinstance(raw_cats, list) and raw_cats:
            categories = [_normalize_category(c) for c in raw_cats if _normalize_category(c)]
        source = "s3"
        updated_at = str(data.get("updated_at", ""))
        if not topics:
            raise ValueError("topics list empty in config")
    except Exception as e:  # noqa: BLE001 — demo must degrade, not fail
        logger.warning("Topics config %s unavailable, using defaults: %s",
                       TOPICS_CONFIG_KEY, e)
        source = "default"

    payload = {
        "topics": topics[:MAX_TOPICS],
        "categories": categories,
        "source": source,
        "updated_at": updated_at,
        "version": VERSION,
    }
    _topics_cache = (payload, time.monotonic())
    return dict(payload)


def _normalize_category(raw) -> dict | None:
    """Accept either ``"id"`` strings or {id, label} objects from config."""
    if isinstance(raw, dict):
        cid = str(raw.get("id", "")).strip()
        if not cid:
            return None
        return {"id": cid, "label": str(raw.get("label") or cid)}
    if isinstance(raw, str) and raw.strip():
        cid = raw.strip()
        return {"id": cid, "label": cid}
    return None


def _parse_json_body(event: dict) -> dict | None:
    """Parse the request body (plain or Base64) into a dict.

    Returns:
        Parsed dict, None on malformed JSON, {} when body absent.
    """
    body = event.get("body")
    if body is None:
        return {}
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clean_topics(raw: list) -> list[str]:
    """Strip/empty-filter + case-insensitive dedupe (keeps first spelling)."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for entry in raw:
        topic = str(entry).strip()
        key = topic.casefold()
        if topic and key not in seen:
            seen.add(key)
            cleaned.append(topic)
    return cleaned


def _search_by_topics(topics: list[str], per_topic: int) -> dict:
    """Score the corpus per topic and return the top-K results per topic.

    Vectors are decoded once and reused across topics (each topic is one
    extra Bedrock embed, not one extra corpus scan).

    Raises:
        EmbeddingError: Topic embedding provider failed.
    """
    model = get_embedding_model()
    try:
        topic_vecs = [(topic, embed_text(topic, model)) for topic in topics]
    except Exception as e:  # noqa: BLE001 — surface as 502, keep API alive
        logger.exception("Topic embedding failed")
        raise EmbeddingError("embedding generation failed") from e

    items = load_corpus()
    comparable: list[tuple[list[float], dict]] = []
    for item in items:
        # Provider consistency (same rule as /search): skip foreign vectors.
        if item.get("embedding_model", "") != model:
            continue
        try:
            vec = unpack_base64(item["embedding"])
        except (KeyError, ValueError):
            continue  # malformed embedding — skip, don't fail the search
        comparable.append((vec, item))

    groups: list[dict] = []
    total = 0
    for topic, tvec in topic_vecs:
        scored = [(cosine_similarity(tvec, vec), item) for vec, item in comparable]
        scored = [pair for pair in scored if pair[0] > MIN_SCORE]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = [_article_result(item, score) for score, item in scored[:per_topic]]
        total += len(results)
        groups.append({"topic": topic, "count": len(results), "results": results})

    return {
        "model": model,
        "corpus_size": len(items),
        "comparable_size": len(comparable),
        "total_results": total,
        "topics": groups,
    }


def _browse_counts() -> dict:
    """Article counts per category (cheap scan, category attribute only)."""
    kwargs = {
        "ProjectionExpression": "#c",
        "ExpressionAttributeNames": {"#c": "category"},
    }
    counts: Counter[str] = Counter()
    resp = articles_table.scan(**kwargs)
    for item in resp.get("Items", []):
        counts[str(item.get("category") or "niche").strip() or "niche"] += 1
    while "LastEvaluatedKey" in resp:
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        resp = articles_table.scan(**kwargs)
        for item in resp.get("Items", []):
            counts[str(item.get("category") or "niche").strip() or "niche"] += 1

    remaining = dict(counts)
    categories: list[dict] = []
    for cat in DEFAULT_CATEGORIES:
        categories.append({**cat, "count": remaining.pop(cat["id"], 0)})
    for extra_id, count in sorted(remaining.items()):
        categories.append({"id": extra_id, "label": extra_id, "count": count})

    return {
        "categories": categories,
        "total": sum(counts.values()),
        "version": VERSION,
    }


def _articles_by_category(category: str, limit: int) -> dict:
    """Recent articles for one category (first_seen desc, title asc)."""
    kwargs = {
        "FilterExpression": "#c = :cat",
        "ProjectionExpression": "#h, #t, #u, #c, #d, #so, #su",
        "ExpressionAttributeNames": {
            "#c": "category",
            "#h": "url_hash",
            "#t": "title",
            "#u": "url",
            "#d": "first_seen",
            "#so": "source",
            "#su": "summary",
        },
        "ExpressionAttributeValues": {":cat": category},
    }
    items: list[dict] = []
    resp = articles_table.scan(**kwargs)
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        resp = articles_table.scan(**kwargs)
        items.extend(resp.get("Items", []))

    # Stable two-pass sort: title asc (secondary), then first_seen desc (primary).
    items.sort(key=lambda item: item.get("title", ""))
    items.sort(key=lambda item: str(item.get("first_seen", "")), reverse=True)
    selected = items[:limit]

    return {
        "category": category,
        "count": len(selected),
        "articles": [_article_result(item) for item in selected],
        "version": VERSION,
    }


def lambda_handler(event: dict, context) -> dict:
    """AWS Lambda entry point for the public /demo/* API Gateway routes.

    Args:
        event: API Gateway proxy event.
        context: Lambda context (unused).

    Returns:
        Proxy response dict (CORS headers on every response, including errors).
    """
    method, path = _route(event)
    logger.info("Demo request: %s %s", method, path)

    if method not in ("GET", "POST"):
        return _response(405, {"error": "method_not_allowed"})

    if method == "GET" and path == "/demo/search":
        q, top_k = parse_search_params(event)
        if not q:
            return _response(400, {
                "error": "missing_query",
                "detail": "Provide a search query: /demo/search?q=<terms>",
            })
        try:
            payload = search_articles(q, top_k)
        except EmbeddingError:
            return _response(502, {"error": "embedding_failed", "detail": "internal_error"})
        return _response(200, payload)

    if method == "GET" and path == "/demo/report/latest":
        return latest_report()

    if method == "GET" and path == "/demo/topics":
        return _response(200, _load_topics_config())

    if method == "POST" and path == "/demo/search-by-topics":
        return _handle_search_by_topics(event)

    if method == "GET" and path == "/demo/browse":
        return _response(200, _browse_counts())

    if method == "GET" and path == "/demo/articles":
        params = event.get("queryStringParameters") or {}
        category = (params.get("category") or "").strip()
        if not category:
            return _response(400, {
                "error": "missing_category",
                "detail": "Provide a category: /demo/articles?category=<id>",
            })
        try:
            limit = min(max(int(params.get("limit") or ""), 1), MAX_ARTICLE_LIMIT)
        except ValueError:
            limit = DEFAULT_ARTICLE_LIMIT
        return _response(200, _articles_by_category(category, limit))

    return _response(404, {"error": "not_found", "path": path})


def _handle_search_by_topics(event: dict) -> dict:
    """Validate the POST body and run the multi-topic search."""
    body = _parse_json_body(event)
    if body is None:
        return _response(400, {"error": "invalid_json", "detail": "Body must be JSON"})

    raw_topics = body.get("topics")
    if not isinstance(raw_topics, list):
        return _response(400, {
            "error": "missing_topics",
            "detail": 'Provide a JSON body: {"topics": ["AI", "climate", ...]}',
        })
    topics = _clean_topics(raw_topics)
    if not topics:
        return _response(400, {"error": "missing_topics", "detail": "No usable topics"})
    if len(topics) > MAX_TOPICS:
        return _response(400, {
            "error": "too_many_topics",
            "detail": f"Maximum {MAX_TOPICS} topics per request",
        })

    try:
        per_topic = min(max(int(body.get("per_topic") or DEFAULT_PER_TOPIC), 1), MAX_PER_TOPIC)
    except (TypeError, ValueError):
        per_topic = DEFAULT_PER_TOPIC

    try:
        payload = _search_by_topics(topics, per_topic)
    except EmbeddingError:
        return _response(502, {"error": "embedding_failed", "detail": "internal_error"})
    return _response(200, payload)