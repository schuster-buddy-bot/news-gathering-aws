"""
embeddings.py — text embedding generation for the news pipeline.

Providers (selected via the embedding-model string, SSM /news-pipeline/embedding-model):
  bedrock:<model-id>  AWS Bedrock embeddings (e.g.
                      "bedrock:amazon.titan-embed-text-v2:0"). True semantic
                      embeddings via bedrock-runtime InvokeModel; 256 dims,
                      L2-normalized by Bedrock itself (normalize: true).
                      Input truncated to 8000 chars (Titan V2: 8192 tokens).
  local-hashed[-N]  Deterministic feature-hashing embedder (pure stdlib).
                    Unigrams + bigrams hashed into a fixed-width vector with
                    sublinear TF weighting, L2-normalized. No network, no cost,
                    no dependency — FALLBACK when Bedrock is unavailable.
  <ollama-model>    Remote embeddings via the Ollama API (/api/embed) for a
                    self-hosted Ollama; derived from OLLAMA_ENDPOINT.

Output contract: dense float32 vector packed little-endian and Base64-encoded
for storage in DynamoDB (``embedding`` attribute). Cosine similarity is
computed against decoded vectors (see search_handler.py).

Graceful degradation: the pipeline falls back to ``local-hashed`` per article
when Bedrock fails; articles record which provider produced their vector via
the ``embedding_model`` attribute, so search/relevance can stay consistent.
"""

import base64
import hashlib
import json
import math
import re
import struct
import threading

TOKEN_RE = re.compile(r"[a-z0-9]+")

DEFAULT_DIMS = 256
BIGRAM_WEIGHT = 1.5  # cheap phrase signal

BEDROCK_PREFIX = "bedrock:"
BEDROCK_DIMS = 256          # Titan V2 supports 256/512/1024; corpus fixed at 256
BEDROCK_MAX_CHARS = 8000    # inputText cap (Titan V2 accepts <= 8192 tokens)
FALLBACK_MODEL = "local-hashed-256"

# Module-level Bedrock client (reused across warm invocations; thread-safe init)
_bedrock_client = None
_bedrock_lock = threading.Lock()


def _tokens(text: str) -> list[str]:
    """Lowercase word tokens.

    Args:
        text: Raw input text.

    Returns:
        List of lowercase alphanumeric tokens.
    """
    return TOKEN_RE.findall(text.lower())


def _bucket(key: tuple[str, str], dims: int) -> int:
    """Hash a term key into a bucket index (stable across runs/invocations).

    Args:
        key: (kind, term) tuple — kind is "u" (unigram) or "b" (bigram).
        dims: Vector width.

    Returns:
        Bucket index in [0, dims).
    """
    digest = hashlib.blake2b(f"{key[0]}|{key[1]}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "little") % dims


def hashed_embedding(text: str, dims: int = DEFAULT_DIMS) -> list[float]:
    """Deterministic bag-of-ngrams embedding via the hashing trick.

    Unigrams and bigrams are hashed into ``dims`` buckets, weighted by
    1 + ln(count), then the vector is L2-normalized so cosine similarity
    reduces to a dot product.

    Args:
        text: Input text.
        dims: Vector width (16..2048).

    Returns:
        List of ``dims`` floats (unit length when text is non-empty).
    """
    vec = [0.0] * dims
    tokens = _tokens(text)
    if not tokens:
        vec[0] = 1.0  # stable unit vector for empty inputs
        return vec

    counts: dict[tuple[str, str], int] = {}
    for tok in tokens:
        key = ("u", tok)
        counts[key] = counts.get(key, 0) + 1
    for a, b in zip(tokens, tokens[1:]):
        key = ("b", f"{a}_{b}")
        counts[key] = counts.get(key, 0) + 1

    for key, count in counts.items():
        weight = (1.0 + math.log(count)) * (BIGRAM_WEIGHT if key[0] == "b" else 1.0)
        vec[_bucket(key, dims)] += weight

    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def pack_base64(vector: list[float]) -> str:
    """Pack a float vector as little-endian float32 + Base64 (DynamoDB form).

    Args:
        vector: Dense float vector.

    Returns:
        Base64 string.
    """
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def unpack_base64(blob: str) -> list[float]:
    """Inverse of pack_base64.

    Args:
        blob: Base64-encoded little-endian float32 vector.

    Returns:
        List of floats.

    Raises:
        ValueError: On malformed Base64 or invalid float data.
    """
    raw = base64.b64decode(blob, validate=True)
    if len(raw) % 4 != 0 or not raw:
        raise ValueError(f"embedding blob size invalid: {len(raw)} bytes")
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Similarity in [-1, 1]; 0.0 on length mismatch (defensive).
    """
    if len(a) != len(b) or not a:
        return 0.0
    return sum(x * y for x, y in zip(a, b))  # vectors are L2-normalized at build time


def bedrock_embedding(text: str, model_id: str, dims: int = BEDROCK_DIMS) -> list[float]:
    """Generate an embedding via AWS Bedrock (InvokeModel).

    Args:
        text: Input text (truncated to BEDROCK_MAX_CHARS; empty/blank input
            is replaced with a placeholder so Titan never sees an empty string).
        model_id: Bedrock model ID, e.g. ``amazon.titan-embed-text-v2:0``.
        dims: Output dimensions (Titan V2: 256/512/1024).

    Returns:
        Dense float vector, L2-normalized (Bedrock ``normalize: true``).

    Raises:
        RuntimeError: On Bedrock API failure (throttling, access, payload).
    """
    if not text or not text.strip():
        text = "(empty)"  # Titan rejects empty inputText
    text = text[:BEDROCK_MAX_CHARS]

    body = json.dumps({
        "inputText": text,
        "dimensions": dims,
        "normalize": True,  # cosine similarity reduces to a dot product
    })
    resp = _get_bedrock_client().invoke_model(
        modelId=model_id,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(resp["body"].read())
    embedding = result.get("embedding")
    if not embedding:
        raise RuntimeError(f"no embedding in Bedrock response: {str(result)[:200]}")
    return [float(x) for x in embedding]


def _get_bedrock_client():
    """Return a cached boto3 bedrock-runtime client (adaptive throttling retries).

    Adaptive retry mode absorbs Titan's tight on-demand TPS quota (new AWS
    accounts are throttled aggressively). The client is built once per
    container and reused across warm invocations.

    Returns:
        boto3 ``bedrock-runtime`` client.
    """
    global _bedrock_client
    if _bedrock_client is None:
        with _bedrock_lock:
            if _bedrock_client is None:
                import os

                import boto3
                from botocore.config import Config

                _bedrock_client = boto3.client(
                    "bedrock-runtime",
                    region_name=os.environ.get("AWS_REGION", "eu-central-1"),
                    config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
                )
    return _bedrock_client


def embed_text(text: str, model: str) -> list[float]:
    """Generate an embedding for ``text`` with the configured provider.

    Args:
        text: Input text (title + summary recommended).
        model: Model identifier. ``bedrock:<id>`` calls AWS Bedrock;
            ``local-hashed[-N]`` uses local feature hashing; anything else
            calls the Ollama embed API.

    Returns:
        Dense float vector.

    Raises:
        RuntimeError: On remote-provider failure (network/HTTP/API errors).
    """
    if model.startswith(BEDROCK_PREFIX):
        return bedrock_embedding(text, model[len(BEDROCK_PREFIX):])

    if model.startswith("local-hashed"):
        dims = DEFAULT_DIMS
        parts = model.split("-")
        if len(parts) >= 3 and parts[2].isdigit():  # local-hashed-<dims>
            dims = max(16, min(int(parts[2]), 2048))
        return hashed_embedding(text, dims)

    # Remote Ollama: derive https://<host>/api/embed from OLLAMA_ENDPOINT
    import os

    import requests

    base = os.environ.get("OLLAMA_ENDPOINT", "https://ollama.com/api/chat")
    embed_url = base.rsplit("/", 1)[0] + "/embed"
    resp = requests.post(embed_url, json={"model": model, "input": text}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    embeddings = data.get("embeddings") or []
    if not embeddings:
        raise RuntimeError(f"no embeddings in response: {str(data)[:200]}")
    return embeddings[0]