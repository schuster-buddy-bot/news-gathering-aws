"""llm_factory.py — Swappable LLM provider factory.

Provides a single entry point ``get_llm()`` that returns a LangChain
``BaseChatModel`` configured from SSM Parameter Store.  This keeps provider
choice (Ollama vs. Bedrock) as a config change rather than a code change,
and lazy imports inside the function avoid loading unused provider classes
on cold start.

Supported providers:
  ollama   -> ChatOllama(model, base_url, temperature, format="json")
  bedrock  -> ChatBedrockConverse(model_id, region, temperature)

Configuration:
  /news-pipeline/llm-provider    SSM parameter (default "ollama")
  /news-pipeline/ollama-model    SSM parameter (Ollama model name)
  OLLAMA_ENDPOINT env var        Ollama base URL
  SSM_API_KEY_PARAM env var      SSM name holding Ollama API key
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

logger = logging.getLogger(__name__)


def get_llm(
    provider: str | None = None,
    model: str | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """Return a configured chat-model instance.

    Args:
        provider: "ollama" or "bedrock".  When ``None``, read from SSM
            ``/news-pipeline/llm-provider`` (default: ollama).
        model: Model identifier.  When ``None``, read from the provider's
            SSM parameter (``/news-pipeline/ollama-model`` for Ollama).
        **kwargs: Extra arguments forwarded to the provider class.
            Common: ``temperature``.

    Returns:
        A LangChain ``BaseChatModel`` instance ready for structured output.

    Raises:
        ValueError: For an unsupported provider.
        RuntimeError: When required config (endpoint/model/region) is missing.
    """
    # Import ssm_get_cached here to avoid a circular import with
    # lambda_handler.py, which imports llm_factory.
    from lambda_handler import ssm_get_cached

    if provider is None:
        provider = ssm_get_cached("/news-pipeline/llm-provider", "ollama").lower().strip()

    if provider == "ollama":
        return _build_ollama(model, **kwargs)

    if provider == "bedrock":
        return _build_bedrock(model, **kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider!r}")


def _build_ollama(model: str | None, **kwargs: Any) -> BaseChatModel:
    """Build a ChatOllama instance configured for JSON output."""
    from langchain_ollama import ChatOllama

    from lambda_handler import ssm_get_cached

    if model is None:
        model = ssm_get_cached(
            os.environ.get("SSM_MODEL_PARAM", "/news-pipeline/ollama-model"),
            "deepseek-v4.1-flash",
        )

    base_url = os.environ.get("OLLAMA_ENDPOINT", "https://ollama.com/api/chat")
    # ChatOllama expects the base endpoint (e.g. https://ollama.com), not the
    # chat path.  Strip the trailing /api/chat if it was configured that way.
    if base_url.endswith("/api/chat"):
        base_url = base_url[: -len("/api/chat")]

    api_key_param = os.environ.get("SSM_API_KEY_PARAM", "/news-pipeline/ollama-api-key")
    api_key = ssm_get_cached(api_key_param, "")

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    temperature = kwargs.get("temperature", 0.0)

    logger.info("LLM factory: Ollama model=%s base_url=%s", model, base_url)
    return ChatOllama(
        model=model,
        base_url=base_url,
        temperature=temperature,
        format="json",
        headers=headers,
    )


def _build_bedrock(model: str | None, **kwargs: Any) -> BaseChatModel:
    """Build a ChatBedrockConverse instance."""
    from langchain_aws import ChatBedrockConverse

    if model is None:
        model = "anthropic.claude-3-haiku-20240307-v1:0"

    region = kwargs.pop("region", os.environ.get("AWS_REGION", "eu-central-1"))
    temperature = kwargs.pop("temperature", 0.0)

    logger.info("LLM factory: Bedrock model=%s region=%s", model, region)
    return ChatBedrockConverse(
        model_id=model,
        region_name=region,
        temperature=temperature,
        **kwargs,
    )
