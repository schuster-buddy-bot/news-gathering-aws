"""
search_handler.py — semantic search Lambda (GET /search?q=...).

Loads all article items that carry an ``embedding`` attribute from the
articles table, embeds the query with the same provider the pipeline used
(SSM /news-pipeline/embedding-model), ranks by cosine similarity and
returns the top matches as JSON with CORS headers.

Environment variables (set by Terraform):
  ARTICLES_TABLE            DynamoDB articles table (PK: url_hash)
  SSM_EMBEDDING_MODEL_PARAM SSM parameter with the embedding model/provider
"""

import json
import logging
import os

import boto3

from embeddings import FALLBACK_MODEL, cosine_similarity, embed_text, unpack_base64

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ARTICLES_TABLE_NAME = os.environ["ARTICLES_TABLE"]
SSM_EMBEDDING_MODEL_PARAM = os.environ.get(
    "SSM_EMBEDDING_MODEL_PARAM", "/news-pipeline/embedding-model"
)
DEFAULT_TOP_K = 10
MAX_TOP_K = 50
MIN_SCORE = 0.01  # drop exact-zero lexical matches from hashed embeddings

dynamodb = boto3.resource("dynamodb")
ssm_client = boto3.client("ssm")
table = dynamodb.Table(ARTICLES_TABLE_NAME)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
}

_VERSION = "1.0"


def _response(status_code: int, payload: dict) -> dict:
    """Build an API Gateway proxy response with CORS headers."""
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json.dumps(payload, ensure_ascii=False, default=str),
    }


def _get_model() -> str:
    """Read the embedding model name from SSM (fallback: local provider)."""
    try:
        resp = ssm_client.get_parameter(Name=SSM_EMBEDDING_MODEL_PARAM)
        return resp["Parameter"]["Value"]
    except Exception as e:  # noqa: BLE001 — search must degrade, not fail
        logger.warning("SSM %s unavailable: %s", SSM_EMBEDDING_MODEL_PARAM, e)
        return FALLBACK_MODEL


def _load_corpus() -> list[dict]:
    """Scan all articles with an embedding (paginated).

    Returns:
        List of article items (url_hash, title, url, category, first_seen,
        source, summary, embedding).
    """
    kwargs = {
        "FilterExpression": "attribute_exists(#e)",
        "ProjectionExpression": "#h, #t, #u, #c, #d, #e, #su, #so, #em",
        "ExpressionAttributeNames": {
            "#h": "url_hash",
            "#t": "title",
            "#u": "url",
            "#c": "category",
            "#d": "first_seen",
            "#e": "embedding",
            "#su": "summary",
            "#so": "source",
            "#em": "embedding_model",
        },
    }
    items: list[dict] = []
    resp = table.scan(**kwargs)
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
    return items


def lambda_handler(event: dict, context) -> dict:
    """AWS Lambda entry point for the /search API Gateway route.

    Args:
        event: API Gateway proxy event with ``q`` (and optional ``limit``)
            query parameters.
        context: Lambda context (unused).

    Returns:
        Proxy response: {query, model, corpus_size, count, results: [...]}
        where each result carries title, url, category, date, source,
        summary and similarity_score (cosine, 0..1).
    """
    params = event.get("queryStringParameters") or {}
    multi = event.get("multiValueQueryStringParameters") or {}
    q = params.get("q") or (multi.get("q", [""])[0] or "")
    limit_raw = params.get("limit") or (multi.get("limit", [""])[0]) or ""
    try:
        top_k = min(max(int(limit_raw), 1), MAX_TOP_K) if limit_raw else DEFAULT_TOP_K
    except ValueError:
        top_k = DEFAULT_TOP_K

    q = q.strip()
    logger.info("Search request: q=%r limit=%s", q[:80], top_k)
    if not q:
        return _response(400, {
            "error": "missing_query",
            "detail": "Provide a search query: /search?q=<terms>",
        })

    model = _get_model()
    try:
        query_vec = embed_text(q, model)
    except Exception:  # noqa: BLE001 — surface as 502, keep API alive
        logger.exception("Query embedding failed")
        logger.exception("embedding generation failed")
        return _response(502, {"error": "embedding_failed", "detail": "internal_error"})

    items = _load_corpus()
    scored: list[tuple[float, dict]] = []
    comparable_count = 0
    for item in items:
        # Provider consistency: only score vectors from the same embedding
        # space as the query (cross-provider cosine is meaningless).
        if item.get("embedding_model", "") != model:
            continue
        comparable_count += 1
        try:
            vec = unpack_base64(item["embedding"])
        except (KeyError, ValueError):
            continue  # malformed embedding — skip, don't fail the search
        score = cosine_similarity(query_vec, vec)
        if score > MIN_SCORE:
            scored.append((score, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = scored[:top_k]

    results = [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "category": item.get("category", ""),
            "date": item.get("first_seen", ""),
            "source": item.get("source", ""),
            "summary": (item.get("summary") or "")[:220],
            "similarity_score": round(float(score), 4),
        }
        for score, item in top
    ]

    return _response(200, {
        "query": q,
        "model": model,
        "corpus_size": len(items),
        "comparable_size": comparable_count,
        "count": len(results),
        "results": results,
    })