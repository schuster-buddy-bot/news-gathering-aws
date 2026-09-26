"""source_authority.py — shared source-authority weights for the news pipeline.

Single source of truth for the per-source authority scores used for ranking
(summarize_top in lambda_handler.py, importance_score in pdf_generator.py).
Higher values rank trusted sources first; unknown sources fall back to
``DEFAULT_AUTHORITY``.
"""

# Authority weight per source name (0.0 .. 1.0)
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

# Weight for sources not listed in SOURCE_AUTHORITY
DEFAULT_AUTHORITY = 0.5