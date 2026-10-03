"""conftest.py — fixtures for the eval harness.

Reuses the stubbed embedding + corpus pattern from the main test suite.
The eval harness is fully deterministic: same stubbed corpus, same
hashed-embedding provider, same cosine similarity — no AWS, no network.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Minimum env stubs (same as main conftest)
os.environ.setdefault("CONFIG_BUCKET", "test-config-bucket")
os.environ.setdefault("ARTICLES_TABLE", "test-articles")
os.environ.setdefault("REPORTS_TABLE", "test-reports")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")

from embeddings import hashed_embedding, pack_base64  # noqa: E402

MODEL = "local-hashed-256"


def make_corpus() -> list[dict]:
    """Build a deterministic stub corpus of ~40 articles spanning all categories.

    Each article has title, url, category, source, summary, and a
    local-hashed-256 embedding (same provider the search core uses).
    """
    articles = [
        # AI Agents
        ("Building Autonomous AI Agents with LangChain", "https://example.com/ai-agents-langchain", "major", "OpenAI News", "A deep dive into building autonomous AI agents using LangChain's framework."),
        ("Multi-Agent Systems for Enterprise Workflow", "https://example.com/multi-agent-enterprise", "major", "VentureBeat AI", "How multi-agent systems are transforming enterprise workflows."),
        ("The Rise of AI Agents in 2026", "https://example.com/ai-agents-rise", "niche", "Ahead of AI (Raschka)", "An analysis of the growing field of AI agents and their capabilities."),

        # LLMs
        ("GPT-5: A Leap in Language Model Capabilities", "https://example.com/gpt5-leap", "major", "OpenAI News", "OpenAI's latest model shows significant improvements in reasoning."),
        ("Transformer Architecture Explained", "https://example.com/transformer-arch", "niche", "The Gradient", "A comprehensive guide to transformer architecture."),
        ("Open-Source LLMs Catching Up to GPT", "https://example.com/opensource-llm", "major", "Hugging Face Blog", "Open-source language models are narrowing the gap with proprietary ones."),

        # Quantum
        ("Quantum Computing Breakthrough at IBM", "https://example.com/ibm-quantum", "major", "MIT Technology Review AI", "IBM announces new qubit fidelity milestone."),

        # Cloud Security
        ("Major Cloud Security Breach at TechCorp", "https://example.com/cloud-breach", "major", "The Verge AI", "A significant cloud security breach exposes millions of records."),
        ("Zero-Trust Architecture Best Practices", "https://example.com/zero-trust", "niche", "NVIDIA Technical Blog", "Implementing zero-trust security in cloud environments."),
        ("API Vulnerability in Popular Framework", "https://example.com/api-vuln", "major", "InfoQ AI/ML/Data", "Researchers discover critical vulnerability in widely-used API framework."),

        # Semiconductors
        ("Chip Export Controls Impact AI Industry", "https://example.com/chip-export", "major", "The Verge AI", "New export controls on semiconductors reshape the AI landscape."),
        ("TSMC Supply Chain Update", "https://example.com/tsmc-supply", "major", "VentureBeat AI", "TSMC reports on supply chain resilience and future capacity."),

        # Open Source
        ("HuggingFace Releases New Model Hub", "https://example.com/hf-model-hub", "niche", "Hugging Face Blog", "A redesigned model hub with better search and model cards."),
        ("Meta Open-Sources Llama 4", "https://example.com/llama4-opensource", "major", "OpenAI News", "Meta's latest open-source model release with commercial license."),

        # EU Regulation
        ("EU AI Act Enters Final Negotiation Phase", "https://example.com/eu-ai-act", "european", "HEISE AI (Germany)", "The EU AI Act reaches a critical milestone in the legislative process."),
        ("Germany's AI Governance Framework", "https://example.com/germany-ai-gov", "european", "The Decoder (Germany)", "Germany introduces national AI governance guidelines."),

        # ML Research
        ("New Research on Efficient Fine-Tuning", "https://example.com/efficient-finetuning", "niche", "arXiv cs.AI", "Researchers propose a novel parameter-efficient fine-tuning method."),
        ("Neural Scaling Laws Revisited", "https://example.com/scaling-laws", "niche", "The Gradient", "A fresh look at neural scaling laws and their implications."),
        ("Breakthrough in Neural Network Interpretability", "https://example.com/nn-interpretability", "niche", "arXiv cs.AI", "A new method for interpreting neural network decisions."),

        # Computer Vision
        ("Real-Time Object Detection on Edge Devices", "https://example.com/edge-detection", "niche", "Roboflow Blog", "Achieving real-time object detection on resource-constrained devices."),

        # NLP
        ("BERT Variants: A 2026 Survey", "https://example.com/bert-survey", "niche", "Ahead of AI (Raschka)", "A comprehensive survey of BERT variants and their performance."),

        # Robotics
        ("Autonomous Robot Navigation in Warehouses", "https://example.com/robot-warehouse", "niche", "KDnuggets", "How autonomous robots are revolutionizing warehouse operations."),

        # Privacy
        ("GDPR Compliance for AI Systems", "https://example.com/gdpr-ai", "european", "France 24 AI", "New guidelines for making AI systems GDPR compliant."),
        ("Data Protection in Cross-Border AI", "https://example.com/crossborder-data", "european", "Silicon UK AI", "Challenges of data protection in international AI deployments."),

        # Cybersecurity
        ("Ransomware Attacks on the Rise", "https://example.com/ransomware-rise", "major", "The Verge AI", "A new wave of ransomware attacks targets critical infrastructure."),
        ("AI-Powered Threat Detection", "https://example.com/ai-threat-detection", "niche", "InfoQ AI/ML/Data", "Using AI for real-time cybersecurity threat detection."),

        # Generative AI
        ("Stable Diffusion 4 Released", "https://example.com/sd4-release", "major", "Hugging Face Blog", "The latest version of Stable Diffusion brings improved quality."),
        ("Generative AI for Code: Copilot and Beyond", "https://example.com/genai-code", "major", "VentureBeat AI", "How generative AI is transforming software development."),

        # Ethics
        ("AI Bias in Hiring Systems", "https://example.com/ai-bias-hiring", "major", "MIT Technology Review AI", "Study reveals significant bias in AI-powered hiring tools."),

        # Neural Networks
        ("Deep Learning for Time Series Forecasting", "https://example.com/dl-timeseries", "niche", "arXiv cs.AI", "Applying deep neural networks to time series prediction."),

        # Reinforcement Learning
        ("RLHF: Reinforcement Learning from Human Feedback", "https://example.com/rlhf", "niche", "Ahead of AI (Raschka)", "How RLHF is shaping the next generation of language models."),

        # Federated Learning
        ("Federated Learning for Privacy-Preserving AI", "https://example.com/federated-privacy", "niche", "arXiv cs.AI", "Federated learning enables training without sharing raw data."),

        # AI Chips
        ("NVIDIA Announces Next-Gen GPU for AI", "https://example.com/nvidia-nextgen", "major", "The Verge AI", "NVIDIA's new accelerator promises 2x performance for AI workloads."),
        ("Google TPU v6 Specifications", "https://example.com/tpu-v6", "major", "OpenAI News", "Google reveals details of its latest tensor processing unit."),

        # Autonomous Vehicles
        ("Waymo Expands Self-Driving Service", "https://example.com/waymo-expand", "major", "VentureBeat AI", "Waymo expands its autonomous vehicle service to new cities."),

        # Climate AI
        ("AI for Climate Modeling", "https://example.com/ai-climate", "niche", "NVIDIA Technical Blog", "Using AI to improve climate change predictions."),

        # Healthcare AI
        ("AI Diagnosis Outperforms Radiologists", "https://example.com/ai-diagnosis", "major", "MIT Technology Review AI", "New study shows AI matching radiologists in cancer detection."),
        ("Drug Discovery with Machine Learning", "https://example.com/ml-drug-discovery", "niche", "arXiv cs.AI", "How ML is accelerating pharmaceutical drug discovery."),

        # Deepfake
        ("Deepfake Detection Challenge 2026", "https://example.com/deepfake-challenge", "major", "The Verge AI", "A new competition aims to improve deepfake detection."),

        # Industry
        ("Tech Layoffs Continue into Q4", "https://example.com/tech-layoffs", "major", "The Verge AI", "Major tech companies announce additional workforce reductions."),
        ("AI Startup Raises $200M Series B", "https://example.com/ai-startup-funding", "major", "VentureBeat AI", "An AI infrastructure startup closes a major funding round."),

        # DevOps
        ("Docker + Kubernetes: Best Practices 2026", "https://example.com/docker-k8s", "niche", "InfoQ AI/ML/Data", "Modern container orchestration patterns for ML workloads."),

        # Edge/IoT
        ("Edge AI: Running Models on IoT Devices", "https://example.com/edge-ai-iot", "niche", "KDnuggets", "Deploying AI models on edge devices with limited resources."),
    ]

    corpus = []
    for title, url, category, source, summary in articles:
        text = f"{title} {summary}"
        vec = hashed_embedding(text, 256)
        corpus.append({
            "title": title,
            "url": url,
            "category": category,
            "first_seen": "2026-10-01",
            "source": source,
            "summary": summary,
            "embedding": pack_base64(vec),
            "embedding_model": MODEL,
        })
    return corpus


@pytest.fixture
def corpus() -> list[dict]:
    """Deterministic stub corpus for eval tests."""
    return make_corpus()


@pytest.fixture
def eval_queries() -> list[dict]:
    """Load the eval test queries from JSON."""
    queries_file = Path(__file__).parent / "test_queries.json"
    with open(queries_file) as f:
        data = json.load(f)
    return data["queries"]


def url_matches(url: str, expected_patterns: list[str]) -> bool:
    """Check if a result URL contains any expected pattern (substring match)."""
    url_lower = url.lower()
    return any(pat.lower() in url_lower for pat in expected_patterns)