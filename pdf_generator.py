"""
pdf_generator.py — reportlab-based PDF report generation for the news pipeline.

Self-contained module: takes categorized articles and pipeline stats, returns
PDF bytes. No AWS dependencies — easy to unit-test in isolation.

Layout (port of the Telegram digest format):
  - Header: title, date, stats line
  - Top Picks (highest importance, full summaries)
  - Category sections (Major / Niche / European / Asian)
  - Footer: generated timestamp, failed sources
"""

from io import BytesIO
import re
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

# ─── Source authority (mirrors lambda_handler / dedup.py) ───────────────────

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

CATEGORY_LABELS = {
    "major": "Major News",
    "niche": "Niche Finds",
    "european": "European Perspective",
    "asian": "Asian Perspective",
}
CATEGORY_ORDER = ["major", "niche", "european", "asian"]

# ─── Styles ──────────────────────────────────────────────────────────────────

_ACCENT = colors.HexColor("#1a3a6b")
_GREY = colors.HexColor("#666666")
_LIGHT = colors.HexColor("#999999")
_LINK = colors.HexColor("#1155cc")

STYLE_TITLE = ParagraphStyle("Title", fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=_ACCENT)
STYLE_SUBTITLE = ParagraphStyle("Subtitle", fontName="Helvetica", fontSize=10, leading=14, textColor=_GREY)
STYLE_SECTION = ParagraphStyle(
    "Section", fontName="Helvetica-Bold", fontSize=13, leading=17,
    textColor=_ACCENT, spaceBefore=14, spaceAfter=4,
)
STYLE_ARTICLE_TITLE = ParagraphStyle("ArticleTitle", fontName="Helvetica-Bold", fontSize=10.5, leading=13.5, textColor=colors.black)
STYLE_META = ParagraphStyle("Meta", fontName="Helvetica", fontSize=8.5, leading=11, textColor=_LIGHT)
STYLE_SUMMARY = ParagraphStyle("Summary", fontName="Helvetica", fontSize=9.5, leading=13, textColor=colors.HexColor("#333333"))
STYLE_LINK = ParagraphStyle("Link", fontName="Helvetica", fontSize=8.5, leading=11, textColor=_LINK)
STYLE_FOOTER = ParagraphStyle("Footer", fontName="Helvetica", fontSize=8, leading=11, textColor=_LIGHT)
STYLE_EMPTY = ParagraphStyle("Empty", fontName="Helvetica-Oblique", fontSize=11, leading=15, textColor=_GREY)


def importance_score(article: dict) -> float:
    """Importance score for an article (0-1): authority + description substance.

    Args:
        article: Article dict with ``source`` and ``description`` keys.

    Returns:
        Score between 0.0 and 1.0.
    """
    authority = SOURCE_AUTHORITY.get(article.get("source", ""), DEFAULT_AUTHORITY)
    desc_len = len(article.get("description", ""))
    substance = min(desc_len / 500, 1.0) * 0.2
    return authority * 0.8 + substance


def ranking_score(article: dict) -> float:
    """Ranking key: interest relevance when available, else importance.

    Args:
        article: Article dict, optionally with ``relevance_score``.

    Returns:
        Score for ordering (relevance wins when the pipeline scored it).
    """
    relevance = article.get("relevance_score")
    if relevance is not None:
        return float(relevance)
    return importance_score(article)


def _clean_for_pdf(text: str) -> str:
    """Strip HTML tags and collapse whitespace for clean PDF rendering.

    Source descriptions and AI summaries may intentionally carry inline HTML
    (the Telegram digest renders it); for the PDF we want plain text.

    Args:
        text: Raw text possibly containing HTML tags.

    Returns:
        Plain text with tags removed and whitespace collapsed.
    """
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _article_block(article: dict, show_summary: bool = True) -> list:
    """Build the flowable block for one article.

    Args:
        article: Article dict.
        show_summary: Include the summary paragraph.

    Returns:
        List of flowables for this article.
    """
    meta = (
        xml_escape(_clean_for_pdf(article.get("source", "unknown")))
        + " \u00b7 " + xml_escape(CATEGORY_LABELS.get(article.get("category", ""), article.get("category", "")))
    )
    relevance = article.get("relevance_score")
    if relevance is not None:
        meta += f" \u00b7 relevance {float(relevance):.2f}"
    block = [
        Paragraph(xml_escape(_clean_for_pdf(article["title"])), STYLE_ARTICLE_TITLE),
        Paragraph(meta, STYLE_META),
    ]
    if show_summary and article.get("summary"):
        block.append(Spacer(1, 1))
        block.append(Paragraph(xml_escape(_clean_for_pdf(article["summary"])), STYLE_SUMMARY))
    block.append(
        Paragraph(f'<a href="{xml_escape(article["url"], {chr(34): "&quot;"})}" color="#1155cc">'
                  f'{xml_escape(article["url"][:100])}</a>', STYLE_LINK)
    )
    block.append(Spacer(1, 6))
    return block


def generate_report_pdf(
    articles_by_category: dict,
    stats: dict,
    date_str: str,
    failed_sources: list | None = None,
    source_warnings: list | None = None,
    top_n: int = 5,
) -> bytes:
    """Generate the daily news report PDF.

    Args:
        articles_by_category: Mapping of category name to article dicts.
        stats: Pipeline statistics (sources_fetched, total_articles, curated).
        date_str: Report date (YYYY-MM-DD).
        failed_sources: Source names that failed to fetch.
        source_warnings: Structured per-source warning dicts.
        top_n: Number of top picks to feature.

    Returns:
        PDF file bytes.
    """
    failed_sources = failed_sources or []
    source_warnings = source_warnings or []

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"AI & Data Science Daily — {date_str}",
        author="News Pipeline (AWS Lambda)",
    )

    # Collect + score all articles for top picks (relevance-ranked)
    all_articles: list[dict] = []
    for cat_articles in articles_by_category.values():
        all_articles.extend(cat_articles)
    scored = sorted(all_articles, key=ranking_score, reverse=True)
    top_pick_urls = {a["url"] for a in scored[:top_n]}

    story: list = []

    # Header
    story.append(Paragraph(f"AI &amp; Data Science Daily — {xml_escape(date_str)}", STYLE_TITLE))
    story.append(Spacer(1, 3))
    story.append(Paragraph(
        f"{stats.get('sources_fetched', 0)} sources · "
        f"{stats.get('total_articles', 0)} articles fetched · "
        f"{stats.get('curated', 0)} curated",
        STYLE_SUBTITLE,
    ))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1, color=_ACCENT))
    story.append(Spacer(1, 10))

    if not all_articles:
        story.append(Paragraph("No articles matched today's filters.", STYLE_EMPTY))
    else:
        # Top Picks (relevance-ranked when relevance scores exist)
        story.append(Paragraph("Top Picks", STYLE_SECTION))
        for article in scored[:top_n]:
            story.extend(_article_block(article, show_summary=True))

        # Category sections (top picks excluded)
        for cat in CATEGORY_ORDER:
            remaining = [
                a for a in articles_by_category.get(cat, [])
                if a["url"] not in top_pick_urls
            ]
            if not remaining:
                continue
            remaining.sort(key=ranking_score, reverse=True)
            story.append(Paragraph(xml_escape(CATEGORY_LABELS[cat]), STYLE_SECTION))
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
            story.append(Spacer(1, 6))
            for article in remaining:
                story.extend(_article_block(article, show_summary=False))

    # Footer
    story.append(Spacer(1, 14))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 6))
    footer_parts = ["Generated by News Pipeline (AWS Lambda)"]
    if failed_sources:
        shown = ", ".join(failed_sources[:5])
        more = f" +{len(failed_sources) - 5} more" if len(failed_sources) > 5 else ""
        footer_parts.append(f"Source issues: {xml_escape(shown)}{more}")
    if source_warnings:
        shown_w = ", ".join(
            f"{w.get('source', '?')} ({w.get('error_type', '?')})"
            for w in source_warnings[:5]
        )
        more_w = f" +{len(source_warnings) - 5} more" if len(source_warnings) > 5 else ""
        footer_parts.append(f"Warnings: {xml_escape(shown_w)}{more_w}")
    for part in footer_parts:
        story.append(Paragraph(part, STYLE_FOOTER))

    doc.build(story)
    return buffer.getvalue()