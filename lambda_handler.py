"""
lambda_handler.py — Serverless news pipeline (AWS Lambda).

Port of the gateway news-gatherer pipeline:
  fetch RSS feeds -> dedup via DynamoDB (URL-hash) -> keyword filter
  -> summarize via Ollama HTTP API -> render PDF (reportlab)
  -> store PDF in S3 + metadata in DynamoDB.

Triggers:
  - EventBridge schedule (cron(0 5 * * ? *)) — normal dedup mode
  - Manual invoke: {"force": true} bypasses DynamoDB cross-run dedup
    (for testing / re-runs; in-batch dedup always applies)

Environment variables (set by Terraform):
  CONFIG_BUCKET       S3 bucket holding config + reports
  ARTICLES_TABLE      DynamoDB table for dedup (PK: url_hash)
  REPORTS_TABLE       DynamoDB table for report metadata (PK: date)
  OLLAMA_ENDPOINT     Ollama chat API endpoint
  SSM_API_KEY_PARAM   SSM parameter holding the Ollama API key (SecureString)
  SSM_MODEL_PARAM     SSM parameter holding the Ollama model name
  MAX_SUMMARIZE       Number of top articles to AI-summarize (default 10)
  ARTICLE_TTL_DAYS    DynamoDB TTL for seen-article entries (default 90)
  REPORTS_PREFIX      S3 prefix for PDF reports (default "reports")
  ARCHIVE_PREFIX      S3 prefix for digest JSON archives (default "archive")
"""

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from typing import Any

import boto3
import requests

try:
    import bleach
except ImportError:  # pragma: no cover - bleach ships in the deploy ZIP
    bleach = None  # type: ignore[assignment]

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ─── AWS clients (module-level, reused across warm invocations) ─────────────

s3_client = boto3.client("s3")
ssm_client = boto3.client("ssm")
ddb_resource = boto3.resource("dynamodb")

# ─── Configuration from environment ─────────────────────────────────────────

CONFIG_BUCKET = os.environ["CONFIG_BUCKET"]
ARTICLES_TABLE_NAME = os.environ["ARTICLES_TABLE"]
REPORTS_TABLE_NAME = os.environ["REPORTS_TABLE"]
REPORTS_PREFIX = os.environ.get("REPORTS_PREFIX", "reports")
ARCHIVE_PREFIX = os.environ.get("ARCHIVE_PREFIX", "archive")
OLLAMA_ENDPOINT = os.environ.get("OLLAMA_ENDPOINT", "https://ollama.com/api/chat")
SSM_API_KEY_PARAM = os.environ.get("SSM_API_KEY_PARAM", "/news-pipeline/ollama-api-key")
SSM_MODEL_PARAM = os.environ.get("SSM_MODEL_PARAM", "/news-pipeline/ollama-model")
MAX_SUMMARIZE = int(os.environ.get("MAX_SUMMARIZE", "10"))
ARTICLE_TTL_DAYS = int(os.environ.get("ARTICLE_TTL_DAYS", "14"))
REPORTS_TTL_DAYS = int(os.environ.get("REPORTS_TTL_DAYS", "30"))

ARTICLES_TABLE = ddb_resource.Table(ARTICLES_TABLE_NAME)
REPORTS_TABLE = ddb_resource.Table(REPORTS_TABLE_NAME)

# Defaults when config/config.json is absent from S3
DEFAULT_FETCH_CONFIG = {
    "timeout_seconds": 10,
    "max_articles_per_feed": 10,
    "retry_attempts": 1,
    "retry_delay_seconds": 5,
    "parallel_fetches": 10,
}

# ─── Source authority (ported from dedup.py) ────────────────────────────────

SOURCE_AUTHORITY = {
    "OpenAI News": 1.0,
    "Google AI Blog": 1.0,
    "MIT Technology Review AI": 0.95,
    "Hugging Face Blog": 0.9,
    "arXiv cs.AI": 0.85,
    "VentureBeat AI": 0.8,
    "The Verge AI": 0.75,
    "Ahead of AI (Raschka)": 0.9,
    "The Gradient": 0.85,
    "Simon Willison's Blog": 0.85,
    "NVIDIA Technical Blog": 0.8,
    "Apple ML Research": 0.8,
    "Last Week in AI": 0.7,
    "KDnuggets": 0.7,
    "Distill": 0.85,
    "Roboflow Blog": 0.65,
    "LangChain Blog": 0.7,
    "MarkTechPost": 0.6,
    "HEISE AI (Germany)": 0.6,
    "The Decoder (Germany)": 0.65,
    "Silicon UK AI": 0.6,
    "France 24 AI": 0.5,
    "AI Business (UK)": 0.6,
    "InfoQ AI/ML/Data": 0.7,
    "Pandaily (China Tech)": 0.5,
    "Synced (China AI)": 0.6,
    "SCMP Tech": 0.6,
    "Japan Times Tech": 0.5,
    "Analytics India Mag": 0.5,
    "AI China": 0.4,
}
DEFAULT_AUTHORITY = 0.5

# Allowed HTML tags for description sanitization (kept small and safe)
_ALLOWED_TAGS = ["p", "br", "a", "ul", "ol", "li", "b", "strong", "i", "em"]
_ALLOWED_ATTRIBUTES = {"a": ["href", "title"]}


# ─── Config loading (S3 + SSM) ───────────────────────────────────────────────

def s3_get_json(bucket: str, key: str, required: bool = True) -> dict[str, Any] | None:
    """Load a JSON document from S3.

    Args:
        bucket: S3 bucket name.
        key: Object key.
        required: If ``True``, missing objects raise; otherwise ``None`` is
            returned so callers can fall back to defaults.

    Returns:
        Parsed JSON dict, or ``None`` when not required and missing.
    """
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except s3_client.exceptions.NoSuchKey:
        if required:
            raise
        logger.info("Optional S3 object absent: s3://%s/%s", bucket, key)
        return None


def ssm_get(name: str, default: str = "") -> str:
    """Read a parameter from SSM Parameter Store.

    Args:
        name: Parameter name (SecureString values are decrypted).
        default: Value to return when the parameter is missing.

    Returns:
        Parameter value or the default.
    """
    try:
        resp = ssm_client.get_parameter(Name=name, WithDecryption=True)
        return resp["Parameter"]["Value"]
    except Exception as e:
        logger.warning("SSM parameter %s unavailable: %s", name, e)
        return default


# ─── Helpers (ported from news-gatherer.py) ──────────────────────────────────

def normalize_title(title: str) -> str:
    """Normalize a title for deduplication.

    Args:
        title: Raw article title.

    Returns:
        Lowercased, punctuation-free title with normalized whitespace.
    """
    t = title.lower().strip()
    t = re.sub(r"[^a-z0-9\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def article_fingerprint(title: str, url: str) -> str:
    """Create a stable fingerprint for an article.

    Args:
        title: Article title.
        url: Article URL.

    Returns:
        SHA-256 hex digest of the normalized title and URL.
    """
    raw = f"{normalize_title(title)[:80]}|{url}"
    return hashlib.sha256(raw.encode()).hexdigest()


def sanitize_html(raw_html: str) -> str:
    """Sanitize HTML content from RSS descriptions.

    Uses bleach when available; falls back to regex stripping otherwise.

    Args:
        raw_html: Raw HTML string.

    Returns:
        Clean plain-ish text.
    """
    if not raw_html:
        return ""
    if bleach is not None:
        cleaned = bleach.clean(
            raw_html,
            tags=_ALLOWED_TAGS,
            attributes=_ALLOWED_ATTRIBUTES,
            strip=True,
        )
        return unescape(cleaned).strip()
    text = re.sub(r"<script[^>]*>.*?</script>", "", raw_html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def fetch_rss(url: str, timeout: int = 10, max_items: int = 10) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch and parse an RSS/Atom feed (stateless — no ETag caching).

    Ported from ``news-gatherer.py``; the ETag/Last-Modified cache is dropped
    because Lambda does not persist state between runs.

    Args:
        url: Feed URL.
        timeout: Request timeout in seconds.
        max_items: Maximum number of items to return.

    Returns:
        Tuple of (articles, meta) where meta carries status/error info.
    """
    articles: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"url": url}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsPipelineAWS/1.0)"}
    req = urllib.request.Request(url, headers=headers)

    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        meta["status"] = str(resp.status)
        raw = resp.read()
        root = ET.fromstring(raw)
    except urllib.error.HTTPError as e:
        meta.update(status=str(e.code), error=str(e), error_type=f"HTTP_{e.code}")
        logger.warning("Feed failed url=%s status=%s type=HTTP_%s", url, e.code, e.code)
        return articles, meta
    except urllib.error.URLError as e:
        meta.update(status="N/A", error=str(e), error_type="URL_ERROR")
        logger.warning("Feed failed url=%s type=URL_ERROR detail=%s", url, e.reason)
        return articles, meta
    except ET.ParseError as e:
        meta.update(status="N/A", error=str(e), error_type="XML_PARSE_ERROR")
        logger.warning("Feed failed url=%s type=XML_PARSE_ERROR", url)
        return articles, meta
    except Exception as e:  # noqa: BLE001 — surface any fetch failure as a warning
        meta.update(status="N/A", error=str(e), error_type=type(e).__name__)
        logger.warning("Feed failed url=%s type=%s detail=%s", url, type(e).__name__, e)
        return articles, meta

    items = root.findall(".//item")
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//atom:entry", ns)

    for item in items[:max_items]:
        # RSS 2.0 fields
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = item.findtext("pubDate") or ""

        # Atom fallbacks
        if not title:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            title_el = item.find("atom:title", ns)
            if title_el is not None and title_el.text:
                title = title_el.text.strip()
            link_el = item.find("atom:link", ns)
            if link_el is not None:
                link = link_el.get("href", "") or link
            summary_el = item.find("atom:summary", ns)
            if summary_el is not None and summary_el.text:
                desc = summary_el.text.strip()
            pub_el = item.find("atom:published", ns)
            if pub_el is not None:
                pub = pub_el.text or pub
            updated_el = item.find("atom:updated", ns)
            if not pub and updated_el is not None:
                pub = updated_el.text or ""

        desc = sanitize_html(desc)[:500]

        if title and link:
            articles.append({
                "title": unescape(title).strip(),
                "url": link,
                "description": desc,
                "published": pub,
                "fingerprint": article_fingerprint(title, link),
            })

    return articles, meta


def matches_filters(article: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Check whether an article passes include/exclude keyword filters.

    Args:
        article: Article dict with ``title`` and ``description`` keys.
        filters: Filter config with ``include_keywords`` / ``exclude_keywords``.

    Returns:
        True when the article passes the filters.
    """
    text = f"{article['title']} {article['description']}".lower()

    for kw in filters.get("exclude_keywords", []):
        if kw.lower() in text:
            return False

    for kw in filters.get("include_keywords", []):
        if kw.lower() in text:
            return True

    return False


# ─── DynamoDB dedup ──────────────────────────────────────────────────────────

def get_seen_hashes(fingerprints: list[str]) -> set[str]:
    """Look up which article fingerprints were already processed on prior runs.

    Uses BatchGetItem in chunks of 100 (single-key table: PK = url_hash).

    Args:
        fingerprints: Candidate fingerprint hex digests.

    Returns:
        Set of fingerprints already present in the articles table.
    """
    seen: set[str] = set()
    keys = [{"url_hash": fp} for fp in fingerprints]

    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        request_items = {ARTICLES_TABLE_NAME: {"Keys": chunk}}
        for attempt in range(3):  # retry UnprocessedKeys up to 3 times
            try:
                resp = ddb_resource.batch_get_item(RequestItems=request_items)
            except Exception as e:
                logger.warning("BatchGetItem failed (attempt %s): %s", attempt + 1, e)
                break
            seen.update(
                item["url_hash"]
                for item in resp.get("Responses", {}).get(ARTICLES_TABLE_NAME, [])
            )
            unprocessed = resp.get("UnprocessedKeys", {}).get(ARTICLES_TABLE_NAME)
            if not unprocessed:
                break
            request_items = {ARTICLES_TABLE_NAME: unprocessed}
            time.sleep(0.2 * (attempt + 1))

    return seen


def record_articles(articles: list[dict[str, Any]], today: str) -> int:
    """Persist article fingerprints to DynamoDB with a TTL.

    Uses per-item ``PutItem`` — BatchWriteItem avoided for least privilege.
    Entries expire after ARTICLE_TTL_DAYS so the dedup table self-cleans.

    Args:
        articles: Articles to record (all fetched, not just filtered).
        today: ISO date string (YYYY-MM-DD).

    Returns:
        Number of articles recorded.
    """
    ttl_epoch = int(time.time()) + ARTICLE_TTL_DAYS * 86400
    count = 0
    for a in articles:
        ARTICLES_TABLE.put_item(Item={
            "url_hash": a["fingerprint"],
            "title": a["title"][:300],
            "url": a["url"],
            "source": a.get("source", ""),
            "category": a.get("category", ""),
            "first_seen": today,
            "ttl": ttl_epoch,
        })
        count += 1
    return count


# ─── Ollama summarization (ported from summarizer.py) ────────────────────────

def clean_summary(raw: str) -> str:
    """Clean model output to extract just the summary.

    Args:
        raw: Raw text output from the summarization model.

    Returns:
        Cleaned summary string (may be empty when unusable).
    """
    raw = re.sub(r"<thinking>.*?</thinking>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"^Thinking\.\.\..*$", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"^We need to.*?\n", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw)
    raw = raw.strip()

    sentences = [s.strip() for s in raw.replace("\n", " ").split(".") if s.strip()]
    meta_words = {
        "we need", "the article", "the user", "the title", "the content",
        "based on", "given the", "in order to", "let me",
    }
    clean_sentences = [
        s for s in sentences if not any(w in s.lower() for w in meta_words)
    ]
    if clean_sentences:
        return ". ".join(clean_sentences[-3:])[:300]
    return ""


def summarize_article(title: str, description: str, model: str, api_key: str) -> tuple[str, bool]:
    """Summarize one article via the Ollama HTTP API.

    Args:
        title: Article title.
        description: Article description/body text.
        model: Ollama model name.
        api_key: Bearer token for the Ollama API.

    Returns:
        Tuple of (summary, used_ai). Falls back to a truncated description
        on failure; the fallback summary may be empty when the article has
        no description.
    """
    desc = description[:800]
    prompt = (
        "Summarize this AI/data-science article in 2-3 concise sentences:\n\n"
        f"Title: {title}\n"
        f"Content: {desc}\n\n"
        "Summary:"
    )
    try:
        resp = requests.post(
            OLLAMA_ENDPOINT,
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        content = (data.get("message", {}) or {}).get("content") or data.get("response") or ""
        summary = clean_summary(content)
        if summary and len(summary) > 20:
            return summary, True
    except Exception as e:  # noqa: BLE001 — summarization must never break the pipeline
        logger.warning("Summarization failed for '%s': %s", title[:50], e)

    return desc[:200], False


def summarize_top(articles: list[dict[str, Any]]) -> tuple[int, bool]:
    """AI-summarize the top articles by source authority, in parallel.

    Args:
        articles: Filtered article list (updated in-place with ``summary``).

    Returns:
        Tuple of (ai_summary_count, ai_enabled).
    """
    if not articles:
        return 0, False

    model = ssm_client.get_parameter(Name=SSM_MODEL_PARAM)["Parameter"]["Value"]
    api_key = ssm_client.get_parameter(Name=SSM_API_KEY_PARAM, WithDecryption=True)["Parameter"]["Value"]

    placeholder = not api_key or api_key == "PLACEHOLDER_SET_BY_MASTER"
    if placeholder:
        logger.warning("Ollama API key not configured (%s) — using description fallback", SSM_API_KEY_PARAM)
        for a in articles:
            a["summary"] = a["description"][:200]
        return 0, False

    scored = sorted(articles, key=lambda a: SOURCE_AUTHORITY.get(a.get("source", ""), DEFAULT_AUTHORITY), reverse=True)
    top = scored[:MAX_SUMMARIZE]
    rest = scored[MAX_SUMMARIZE:]

    ai_success = 0

    def _summarize(article: dict[str, Any]) -> None:
        nonlocal ai_success
        summary, used_ai = summarize_article(article["title"], article["description"], model, api_key)
        article["summary"] = summary
        article["ai_summary"] = used_ai
        if used_ai:
            ai_success += 1

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_summarize, a) for a in top]
        for future in as_completed(futures):
            future.result()

    for a in rest:
        a["summary"] = a["description"][:200]
        a["ai_summary"] = False

    return ai_success, True


# ─── Pipeline ────────────────────────────────────────────────────────────────

def run_pipeline(force: bool = False) -> dict[str, Any]:
    """Run the full news pipeline.

    Args:
        force: When ``True``, skip DynamoDB cross-run dedup (manual re-runs).
            In-batch dedup always applies.

    Returns:
        Pipeline statistics dict.
    """
    started = datetime.now(timezone.utc)
    logger.info("News pipeline (AWS) starting — force=%s", force)

    # Step 1: Load config from S3
    sources_data = s3_get_json(CONFIG_BUCKET, "config/sources.json")
    filters = s3_get_json(CONFIG_BUCKET, "config/filters.json")
    pipeline_config = s3_get_json(CONFIG_BUCKET, "config/config.json", required=False) or {}
    fetch_cfg = {**DEFAULT_FETCH_CONFIG, **(pipeline_config.get("fetch", {}))}

    enabled_sources = [s for s in sources_data["sources"] if s.get("enabled", True)]
    logger.info("Loaded %s enabled sources", len(enabled_sources))

    # Step 2: Fetch all feeds in parallel
    all_articles: list[dict[str, Any]] = []
    failed_sources: list[str] = []
    source_warnings: list[dict[str, str]] = []

    def fetch_source(source: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None, dict[str, str] | None]:
        name, url, cat = source["name"], source["url"], source["category"]
        try:
            articles, meta = fetch_rss(url, fetch_cfg["timeout_seconds"], fetch_cfg["max_articles_per_feed"])
            if meta.get("error_type"):
                return [], name, {
                    "source": name, "url": url,
                    "status": meta.get("status", "N/A"),
                    "error_type": meta.get("error_type", "UNKNOWN"),
                    "detail": meta.get("error", ""),
                }
            for a in articles:
                a["source"] = name
                a["category"] = cat
            logger.info("  OK %s: %s articles", name, len(articles))
            return articles, None, None
        except Exception as e:  # noqa: BLE001
            return [], name, {
                "source": name, "url": url, "status": "N/A",
                "error_type": type(e).__name__, "detail": str(e),
            }

    parallel = min(int(fetch_cfg["parallel_fetches"]), len(enabled_sources) or 1)
    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = {executor.submit(fetch_source, s): s for s in enabled_sources}
        for future in as_completed(futures):
            articles, failed, warning = future.result()
            all_articles.extend(articles)
            if failed:
                failed_sources.append(failed)
            if warning:
                source_warnings.append(warning)

    # Retry failed sources once
    if failed_sources and int(fetch_cfg["retry_attempts"]) > 0:
        logger.info("Retrying %s failed sources...", len(failed_sources))
        time.sleep(int(fetch_cfg["retry_delay_seconds"]))
        retry_set = [s for s in enabled_sources if s["name"] in failed_sources]
        still_failed = []
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {executor.submit(fetch_source, s): s for s in retry_set}
            for future in as_completed(futures):
                articles, failed, warning = future.result()
                all_articles.extend(articles)
                if failed:
                    still_failed.append(failed)
                if warning:
                    source_warnings.append(warning)
        failed_sources = still_failed

    logger.info("Total raw articles: %s (failed sources: %s)", len(all_articles), failed_sources)

    # Step 3: In-batch dedup (URL + fingerprint)
    seen_in_batch: set[str] = set()
    unique_articles = []
    for a in all_articles:
        dedup_key = a["fingerprint"]
        if a["url"] in seen_in_batch or a["fingerprint"] in seen_in_batch:
            continue
        seen_in_batch.add(a["url"])
        seen_in_batch.add(a["fingerprint"])
        unique_articles.append(a)
    logger.info("After in-batch dedup: %s articles", len(unique_articles))

    # Step 4: Cross-run dedup via DynamoDB
    if force:
        logger.info("Cross-run dedup skipped (force mode)")
        new_articles = unique_articles
        skipped_seen = 0
    else:
        fingerprints = [a["fingerprint"] for a in unique_articles]
        seen_hashes = get_seen_hashes(fingerprints)
        new_articles = [a for a in unique_articles if a["fingerprint"] not in seen_hashes]
        skipped_seen = len(unique_articles) - len(new_articles)
        logger.info("Cross-run dedup: %s new / %s already seen", len(new_articles), skipped_seen)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Record everything fetched as seen (also filtered-out items — they must
    # not resurface later). Only when not forced, so test runs stay repeatable
    # with respect to later daily runs is NOT desired: force runs DO record.
    if unique_articles:
        recorded = record_articles(new_articles, today)
        logger.info("Recorded %s article hashes in DynamoDB", recorded)

    # Step 5: Keyword filter
    filtered = [a for a in new_articles if matches_filters(a, filters)]
    logger.info("After filtering: %s articles", len(filtered))

    # Step 6: AI summarization (top articles; fallback for the rest)
    ai_count, ai_enabled = summarize_top(filtered)

    # Step 7: Categorize
    by_category: dict[str, list[dict[str, Any]]] = {
        "major": [], "niche": [], "european": [], "asian": [],
    }
    for a in filtered:
        cat = a.get("category", "niche")
        by_category.setdefault(cat if cat in by_category else "niche", []).append(a)

    # Step 8: Generate PDF
    from pdf_generator import generate_report_pdf

    stats = {
        "sources_fetched": len(enabled_sources),
        "total_articles": len(all_articles),
        "after_dedup": len(new_articles),
        "curated": len(filtered),
    }
    pdf_bytes = generate_report_pdf(
        by_category, stats, today,
        failed_sources=failed_sources,
        source_warnings=source_warnings,
    )

    # Step 9: Store PDF + digest archive in S3
    pdf_key = f"{REPORTS_PREFIX}/{today}-report.pdf"
    s3_client.put_object(
        Bucket=CONFIG_BUCKET, Key=pdf_key, Body=pdf_bytes,
        ContentType="application/pdf", CacheControl="no-cache",
    )
    digest_key = f"{ARCHIVE_PREFIX}/{today}-digest.json"
    digest_data = {
        "date": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "failed_sources": failed_sources,
        "source_warnings": source_warnings,
        "ai_summaries": ai_count,
        "articles": filtered,
    }
    s3_client.put_object(
        Bucket=CONFIG_BUCKET, Key=digest_key,
        Body=json.dumps(digest_data, ensure_ascii=False, default=str).encode("utf-8"),
        ContentType="application/json",
    )

    # Step 10: Store report metadata in DynamoDB (TTL: REPORTS_TTL_DAYS)
    REPORTS_TABLE.put_item(Item={
        "date": today,
        "s3_key": pdf_key,
        "article_count": len(filtered),
        "sources_fetched": len(enabled_sources),
        "total_raw": len(all_articles),
        "ai_summaries": ai_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ttl": int(time.time()) + REPORTS_TTL_DAYS * 86400,
    })

    result = {
        "date": today,
        "sources_fetched": len(enabled_sources),
        "total_raw": len(all_articles),
        "after_in_batch_dedup": len(unique_articles),
        "skipped_already_seen": skipped_seen,
        "curated": len(filtered),
        "ai_summaries": ai_count,
        "ai_enabled": ai_enabled,
        "failed_sources": failed_sources,
        "source_warning_count": len(source_warnings),
        "pdf_key": pdf_key,
        "digest_key": digest_key,
        "force": force,
    }
    logger.info("Pipeline complete: %s", json.dumps(result))
    return result


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point.

    Args:
        event: EventBridge scheduled event, manual invoke payload, or ``{}``.
            Supports ``{"force": true}`` to bypass cross-run dedup and
            ``{"test_ai": true}`` to run a single Ollama API probe without
            touching storage (used for debugging).
        context: Lambda context (unused).

    Returns:
        HTTP-style response with pipeline statistics.
    """
    logger.info("Invocation event: %s", json.dumps(event, default=str)[:500])
    force = bool(event.get("force") or event.get("rerun"))

    if event.get("test_ai"):
        model = ssm_client.get_parameter(Name=SSM_MODEL_PARAM)["Parameter"]["Value"]
        api_key = ssm_client.get_parameter(Name=SSM_API_KEY_PARAM, WithDecryption=True)["Parameter"]["Value"]
        import requests as _requests
        try:
            resp = _requests.post(
                OLLAMA_ENDPOINT,
                json={"model": model, "messages": [{"role": "user", "content": "Say OK"}], "stream": False},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=60,
            )
            body_snippet = resp.text[:400]
        except Exception as e:  # noqa: BLE001
            body_snippet = f"EXCEPTION: {e}"
        return {
            "statusCode": 200,
            "body": json.dumps({"endpoint": OLLAMA_ENDPOINT, "model": model,
                                "status": getattr(resp, "status_code", None),
                                "body": body_snippet}),
        }

    try:
        stats = run_pipeline(force=force)
        return {
            "statusCode": 200,
            "body": json.dumps(stats, ensure_ascii=False, default=str),
        }
    except Exception:
        logger.exception("Pipeline failed")
        raise