"""
embeddings.py — text embedding generation for the news pipeline.

Providers (selected via the embedding-model string, SSM /news-pipeline/embedding-model):
  local-hashed[-N]  Deterministic feature-hashing embedder (pure stdlib).
                    Unigrams + bigrams hashed into a fixed-width vector with
                    sublinear TF weighting, L2-normalized. No network, no cost,
                    no dependency — default for the serverless pipeline.
  <ollama-model>    Remote embeddings via the Ollama API (/api/embed) for a
                    self-hosted Ollama; derived from OLLAMA_ENDPOINT.

Output contract: dense float32 vector packed little-endian and Base64-encoded
for storage in DynamoDB (``embedding`` attribute). Cosine similarity is
computed against decoded vectors (see search_handler.py).

Note (Sprint W41): ollama.com's hosted API exposes chat models only
(/api/embeddings 404, /api/embed 401), so the pipeline defaults to the local
hashed provider. Switching to a neural embedder later = set the SSM parameter
to the desired Ollama model name + point OLLAMA_ENDPOINT at an /api/embed-capable host.
"""

import base64
import hashlib
import math
import re
import struct

TOKEN_RE = re.compile(r"[a-z0-9]+")

DEFAULT_DIMS = 256
BIGRAM_WEIGHT = 1.5  # cheap phrase signal


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


def embed_text(text: str, model: str) -> list[float]:
    """Generate an embedding for ``text`` with the configured provider.

    Args:
        text: Input text (title + summary recommended).
        model: Model identifier. ``local-hashed[-N]`` uses local feature
            hashing; anything else calls the Ollama embed API.

    Returns:
        Dense float vector.

    Raises:
        RuntimeError: On remote-provider failure (network/HTTP/API errors).
    """
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