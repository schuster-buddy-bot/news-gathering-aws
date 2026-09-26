"""
api_handler.py — API Gateway Lambda for the news report API.

Routes (REST API v1, stage path included):
  GET /health         -> {"status": "ok", ...}
  GET /report/latest  -> latest report metadata + presigned S3 URL
  GET /               -> service info

Environment variables (set by Terraform):
  REPORTS_TABLE        DynamoDB reports table (PK: date)
  CONFIG_BUCKET        S3 bucket holding the PDF reports
  PRESIGN_TTL_SECONDS  Presigned URL validity (default 3600)
"""

import json
import logging
import os

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REPORTS_TABLE_NAME = os.environ["REPORTS_TABLE"]
BUCKET = os.environ["CONFIG_BUCKET"]
PRESIGN_TTL = int(os.environ.get("PRESIGN_TTL_SECONDS", "3600"))

dynamodb = boto3.resource("dynamodb")
# Regional endpoint + virtual-host addressing: presigned URLs must point at
# s3.<region>.amazonaws.com — the global endpoint issues a 307 redirect that
# breaks the SigV4 host signature (403 after redirect).
s3 = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION", "eu-central-1"),
    config=Config(s3={"addressing_style": "virtual"}, signature_version="s3v4"),
)
table = dynamodb.Table(REPORTS_TABLE_NAME)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
}
VERSION = "1.0"


def _response(status_code: int, payload: dict) -> dict:
    """Build an API Gateway proxy response.

    Args:
        status_code: HTTP status code.
        payload: JSON-serializable response body.

    Returns:
        API Gateway proxy-format response dict.
    """
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json.dumps(payload, ensure_ascii=False, default=str),
    }


def _health() -> dict:
    """Health check — verifies config and DynamoDB connectivity.

    Returns:
        Proxy response with status ok or 500 on failure.
    """
    try:
        table.load()  # validates table access + existence
        return _response(200, {
            "status": "ok",
            "version": VERSION,
            "region": s3.meta.region_name,
        })
    except Exception:  # noqa: BLE001
        logger.exception("Health check failed")
        logger.exception("health check failed")
        return _response(500, {"status": "error", "detail": "internal_error"})


def _info() -> dict:
    """Service info page listing available endpoints.

    Returns:
        Proxy response with endpoint list.
    """
    return _response(200, {
        "service": "news-pipeline-api",
        "version": VERSION,
        "endpoints": {
            "GET /health": "Service health check (public)",
            "GET /search?q=&limit=": "Semantic search (x-api-key required)",
            "GET /report/latest": "Latest daily report (x-api-key required)",
        },
    })


def _latest_report() -> dict:
    """Return metadata + presigned URL for the latest report.

    Scans the reports table (one item per day, small table) and picks the
    newest date.

    Returns:
        Proxy response with report metadata and a presigned PDF URL,
        or 404 when no report exists yet.
    """
    try:
        resp = table.scan(ProjectionExpression="#d, s3_key, article_count, generated_at",
                          ExpressionAttributeNames={"#d": "date"})
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:  # tiny table, but be safe
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"],
                              ProjectionExpression="s3_key, article_count, generated_at, #d",
                              ExpressionAttributeNames={"#d": "date"})
            items.extend(resp.get("Items", []))
    except Exception:  # noqa: BLE001
        logger.exception("DynamoDB scan failed")
        logger.exception("DynamoDB unavailable")
        return _response(500, {"error": "dynamodb_unavailable", "detail": "internal_error"})

    if not items:
        return _response(404, {"error": "no_reports_yet"})

    latest = max(items, key=lambda item: item["date"])
    s3_key = latest.get("s3_key", "")
    if not s3_key:
        return _response(404, {"error": "report_missing_s3_key"})

    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": s3_key},
            ExpiresIn=PRESIGN_TTL,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Presigned URL generation failed")
        logger.exception("presign failed")
        return _response(500, {"error": "presign_failed", "detail": "internal_error"})

    return _response(200, {
        "date": latest["date"],
        "article_count": int(latest.get("article_count", 0)),
        "generated_at": latest.get("generated_at", ""),
        "s3_key": s3_key,
        "url": url,
        "expires_in": PRESIGN_TTL,
    })


def lambda_handler(event: dict, context) -> dict:
    """AWS Lambda entry point for API Gateway proxy events.

    Args:
        event: API Gateway proxy event (REST or HTTP API format).
        context: Lambda context (unused).

    Returns:
        Proxy response dict.
    """
    method = (event.get("httpMethod")
              or (event.get("requestContext", {}).get("http", {}) or {}).get("method")
              or "GET")
    path = event.get("path") or event.get("rawPath") or "/"

    logger.info("API request: %s %s", method, path)

    if method != "GET":
        return _response(405, {"error": "method_not_allowed"})

    normalized = path.rstrip("/")
    if normalized in ("/", ""):
        return _info()
    if normalized == "/health":
        return _health()
    if normalized == "/report/latest":
        return _latest_report()

    return _response(404, {"error": "not_found", "path": path})