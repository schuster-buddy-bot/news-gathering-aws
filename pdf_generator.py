"""
pdf_generator.py — Polished PDF news report for the AWS Lambda pipeline.

Layout v5 (unified with local news-gatherer v4 + demo-UI design learnings):

  - Header: title, date, dark stat bar (indigo accent)
  - Table of contents with internal bookmark links
  - Top Picks: full summaries, colored category accent, clickable headlines
  - Category sections: colored header bar (CategoryBar flowable),
    zebra-striped list, clickable headlines, max 8 per category
  - Page numbers + date in footer
  - Clean encoding (NBSP → space, entity cleanup)

Lambda-compatible: pure in-memory (BytesIO), no filesystem image cache,
no external HTTP calls. All images replaced with colored accent design.

Design tokens borrowed from the demo UI:
  - Accent: indigo #4f46e5 / violet #7c3aed / cyan #06b6d4
  - Dark stat bar: #0f172a
  - Category colors: matched to demo UI category palette
  - Rounded corners, subtle borders, zebra striping
"""

from io import BytesIO
import re
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_LEFT, TA_JUSTIFY
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.flowables import Flowable
from reportlab.lib.colors import HexColor

from source_authority import DEFAULT_AUTHORITY, SOURCE_AUTHORITY

# ─── Categories (matched to local v4 + sources.json) ─────────────────────────

CATEGORIES = {
    "un": {"label": "United Nations", "color": HexColor("#009EDB"), "bg": HexColor("#E8F4FB")},
    "eu_parliament": {"label": "EU Parliament", "color": HexColor("#003399"), "bg": HexColor("#E8EDF5")},
    "bundestag": {"label": "Deutscher Bundestag", "color": HexColor("#333333"), "bg": HexColor("#F0F0F0")},
    "major": {"label": "Major AI News", "color": HexColor("#2E7D32"), "bg": HexColor("#E8F5E9")},
    "niche": {"label": "Niche & Research", "color": HexColor("#6A1B9A"), "bg": HexColor("#F3E5F5")},
    "european": {"label": "European Perspective", "color": HexColor("#1565C0"), "bg": HexColor("#E3F2FD")},
    "asian": {"label": "Asian Perspective", "color": HexColor("#BF360C"), "bg": HexColor("#FBE9E7")},
}

CATEGORY_ORDER = ["un", "eu_parliament", "bundestag", "major", "niche", "european", "asian"]
CATEGORY_LABELS = {k: v["label"] for k, v in CATEGORIES.items()}

TOP_PICKS_COUNT = 8
MAX_PER_CATEGORY = 8

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

# ─── Design tokens (from demo UI) ────────────────────────────────────────────

ACCENT = HexColor("#4f46e5")      # indigo
ACCENT_2 = HexColor("#7c3aed")    # violet
ACCENT_3 = HexColor("#06b6d4")    # cyan
DARK_BAR = HexColor("#0f172a")    # dark slate (stat bar)
INK = HexColor("#1a1a1a")
INK_SOFT = HexColor("#333333")
INK_MUTED = HexColor("#666666")
INK_FAINT = HexColor("#888888")
INK_LIGHTER = HexColor("#999999")
LINK = HexColor("#4A90D9")
ZEBRA_LIGHT = HexColor("#f5f5f5")
ZEBRA_WHITE = HexColor("#ffffff")
BORDER_LIGHT = HexColor("#e0e0e0")
BORDER_FAINT = HexColor("#eeeeee")


# ─── Scoring ─────────────────────────────────────────────────────────────────

def importance_score(article: dict) -> float:
    """Importance score: authority + description substance."""
    authority = SOURCE_AUTHORITY.get(article.get("source", ""), DEFAULT_AUTHORITY)
    desc_len = len(article.get("description", article.get("summary", "")))
    substance = min(desc_len / 500, 1.0) * 0.2
    return authority * 0.8 + substance


def ranking_score(article: dict) -> float:
    """Ranking key: relevance when available, else importance."""
    relevance = article.get("relevance_score")
    if relevance is not None:
        return float(relevance)
    return importance_score(article)


# ─── Text cleaning ───────────────────────────────────────────────────────────

def _clean_text(text: str) -> str:
    """Clean text for PDF: fix encoding, remove HTML, normalize spaces."""
    if not text:
        return ""
    from html import unescape
    text = unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ")
    text = text.replace("\u2009", " ").replace("\u200a", " ").replace("\u202f", " ")
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return text.strip()


def _color_hex(color_obj) -> str:
    """Get #rrggbb string from reportlab HexColor."""
    return color_obj.hexval().replace("0x", "#")


# ─── Custom Flowable: CategoryBar ────────────────────────────────────────────

class CategoryBar(Flowable):
    """Colored category header bar with bookmark target for TOC links."""

    def __init__(self, label, count, color, bg, bookmark_key=None, width=CONTENT_W):
        Flowable.__init__(self)
        self.label = label
        self.count = count
        self.color = color
        self.bg = bg
        self.bookmark_key = bookmark_key
        self.width = width
        self.height = 9 * mm

    def draw(self):
        c = self.canv
        if self.bookmark_key:
            c.bookmarkPage(self.bookmark_key)
        # Rounded background
        c.setFillColor(self.bg)
        c.roundRect(0, 0, self.width, self.height, 1.5 * mm, fill=1, stroke=0)
        # Colored left accent
        c.setFillColor(self.color)
        c.rect(0, 0, 4 * mm, self.height, fill=1, stroke=0)
        # Label
        c.setFillColor(self.color)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(7 * mm, 2.5 * mm, self.label)
        # Count
        c.setFont("Helvetica", 8)
        c.setFillColor(INK_FAINT)
        c.drawRightString(self.width - 4 * mm, 2.5 * mm, f"{self.count} articles")


# ─── Page template (footer) ──────────────────────────────────────────────────

def _on_page(canvas_obj, doc):
    canvas_obj.saveState()
    canvas_obj.setFont("Helvetica", 7)
    canvas_obj.setFillColor(HexColor("#aaaaaa"))
    page_num = canvas_obj.getPageNumber()
    date_str = getattr(doc, "_date_str", "")
    canvas_obj.drawString(MARGIN, 10 * mm, f"News Intelligence Daily — {date_str}")
    canvas_obj.drawRightString(PAGE_W - MARGIN, 10 * mm, f"Page {page_num}")
    canvas_obj.setStrokeColor(BORDER_LIGHT)
    canvas_obj.setLineWidth(0.3)
    canvas_obj.line(MARGIN, 13 * mm, PAGE_W - MARGIN, 13 * mm)
    canvas_obj.restoreState()


# ─── PDF Generation ──────────────────────────────────────────────────────────

def generate_report_pdf(
    articles_by_category: dict,
    stats: dict,
    date_str: str,
    failed_sources: list | None = None,
    source_warnings: list | None = None,
    top_n: int = TOP_PICKS_COUNT,
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

    # Collect all articles for top picks
    all_articles: list[dict] = []
    for cat_articles in articles_by_category.values():
        all_articles.extend(cat_articles)

    # Top picks: best per category first, then fill by authority
    top_picks = []
    seen_sources = set()
    for cat_key in CATEGORY_ORDER:
        cat_articles = articles_by_category.get(cat_key, [])
        if not cat_articles:
            continue
        best = max(cat_articles, key=lambda a: importance_score(a))
        top_picks.append(best)
        seen_sources.add(best.get("source", ""))
    remaining = [a for a in all_articles if a not in top_picks and a.get("source", "") not in seen_sources]
    remaining.sort(key=importance_score, reverse=True)
    for a in remaining:
        if len(top_picks) >= top_n:
            break
        top_picks.append(a)
    top_picks = top_picks[:top_n]
    top_pick_urls = {a["url"] for a in top_picks}

    # ─── Build PDF ───
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=18 * mm,
        title=f"News Intelligence Daily — {date_str}",
        author="News Pipeline (AWS Lambda)",
    )
    doc._date_str = date_str

    s = getSampleStyleSheet()

    # ─── Styles ───
    st_title = ParagraphStyle("T", parent=s["Title"], fontSize=24, textColor=INK,
        spaceAfter=0, alignment=TA_LEFT, fontName="Helvetica-Bold", leading=28)
    st_date = ParagraphStyle("D", parent=s["Normal"], fontSize=10, textColor=INK_FAINT,
        spaceAfter=8, fontName="Helvetica")
    st_section = ParagraphStyle("S", parent=s["Heading2"], fontSize=14, spaceBefore=12, spaceAfter=4,
        textColor=INK, fontName="Helvetica-Bold")
    st_toc = ParagraphStyle("TOC", parent=s["Normal"], fontSize=10, spaceAfter=2, leading=15,
        fontName="Helvetica", textColor=INK_SOFT)
    st_pick_cat = ParagraphStyle("PC", parent=s["Normal"], fontSize=7.5, textColor=INK_FAINT,
        spaceAfter=3, fontName="Helvetica")
    st_pick_title = ParagraphStyle("PT", parent=s["Normal"], fontSize=12, fontName="Helvetica-Bold",
        spaceAfter=2, leading=15, textColor=INK)
    st_pick_src = ParagraphStyle("PS", parent=s["Normal"], fontSize=7.5, textColor=INK_FAINT,
        spaceAfter=3, fontName="Helvetica")
    st_pick_sum = ParagraphStyle("PU", parent=s["Normal"], fontSize=9, spaceAfter=3, leading=12,
        fontName="Helvetica", textColor=INK_SOFT, alignment=TA_JUSTIFY)
    st_pick_link = ParagraphStyle("PL", parent=s["Normal"], fontSize=7.5, textColor=LINK,
        fontName="Helvetica")
    st_art_title = ParagraphStyle("AT", parent=s["Normal"], fontSize=9.5, fontName="Helvetica-Bold",
        spaceAfter=1, leading=12, textColor=INK)
    st_art_sum = ParagraphStyle("AS", parent=s["Normal"], fontSize=8, spaceAfter=2, leading=10.5,
        fontName="Helvetica", textColor=INK_MUTED)
    st_art_src = ParagraphStyle("AR", parent=s["Normal"], fontSize=7, textColor=INK_LIGHTER,
        fontName="Helvetica", spaceAfter=2)
    st_more = ParagraphStyle("M", parent=s["Normal"], fontSize=8, textColor=INK_LIGHTER,
        fontName="Helvetica-Oblique")
    st_anchor = ParagraphStyle("A", parent=s["Normal"], fontSize=1, textColor=colors.white)

    story: list = []

    # ─── Header ───
    story.append(Paragraph("📰 News Intelligence Daily", st_title))
    story.append(Paragraph(date_str, st_date))

    stat_text = (
        f"  {stats.get('sources_fetched', 0)} sources  ·  "
        f"{stats.get('total_articles', 0)} fetched  ·  "
        f"{stats.get('curated', len(all_articles))} curated  ·  "
        f"{sum(1 for c in CATEGORY_ORDER if articles_by_category.get(c))} categories"
    )
    stat_table = Table(
        [[Paragraph(f'<font color="white" size="9">{xml_escape(stat_text)}</font>', st_pick_src)]],
        colWidths=[CONTENT_W], rowHeights=[7 * mm],
    )
    stat_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), DARK_BAR),
        ("ROUNDEDCORNERS", [1.5 * mm]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
    ]))
    story.append(stat_table)
    story.append(Spacer(1, 8))

    # ─── Table of Contents ───
    story.append(Paragraph("Contents", st_section))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_LIGHT, spaceBefore=2, spaceAfter=4))

    toc_rows = []
    toc_rows.append([
        Paragraph('<a href="#top_picks"><font color="#1a1a1a"><b>Top Picks</b></font></a>', st_toc),
        Paragraph(f'<font color="#888">{len(top_picks)} featured</font>', st_toc),
    ])
    for cat_key in CATEGORY_ORDER:
        cat_articles = articles_by_category.get(cat_key, [])
        if not cat_articles:
            continue
        cat_info = CATEGORIES[cat_key]
        bookmark_key = f"cat_{cat_key}"
        chex = _color_hex(cat_info["color"])
        toc_rows.append([
            Paragraph(
                f'<a href="#{bookmark_key}"><font color="{chex}">●</font> '
                f'<font color="#333">{cat_info["label"]}</font></a>',
                st_toc,
            ),
            Paragraph(f'<font color="#888">{len(cat_articles)} articles</font>', st_toc),
        ])

    toc_table = Table(toc_rows, colWidths=[CONTENT_W * 0.7, CONTENT_W * 0.3])
    toc_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, BORDER_FAINT),
    ]))
    story.append(toc_table)
    story.append(Spacer(1, 10))

    # ─── Top Picks ───
    story.append(Paragraph("Top Picks", st_section))
    story.append(Paragraph('<a name="top_picks"/>', st_anchor))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_LIGHT, spaceBefore=2, spaceAfter=6))

    for article in top_picks:
        cat_key = article.get("category", "niche")
        cat_info = CATEGORIES.get(cat_key, {"label": "", "color": HexColor("#999"), "bg": HexColor("#eee")})
        source = _clean_text(article.get("source", ""))
        summary = _clean_text(article.get("summary", article.get("description", "")))[:250]
        title = _clean_text(article.get("title", ""))
        url = article.get("url", "")
        chex = _color_hex(cat_info["color"])

        # Text column with colored left accent bar (replaces image)
        cat_dot = f'<font color="{chex}">●</font> <b>{cat_info["label"]}</b>'
        text_elements = [
            Paragraph(cat_dot, st_pick_cat),
            Spacer(1, 2),
            Paragraph(f'<a href="{xml_escape(url)}"><font color="#1a1a1a">{xml_escape(title)}</font></a>', st_pick_title),
            Paragraph(f'<font color="#888">{xml_escape(source)}</font>', st_pick_src),
            Spacer(1, 2),
            Paragraph(xml_escape(summary), st_pick_sum),
            Paragraph(f'<a href="{xml_escape(url)}"><font color="#4A90D9">Read more →</font></a>', st_pick_link),
        ]

        text_col = Table([[e] for e in text_elements], colWidths=[CONTENT_W - 8])
        text_col.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))

        # Card with colored left border (accent bar replaces image)
        card = Table([[text_col]], colWidths=[CONTENT_W])
        card.setStyle(TableStyle([
            ("LINEBEFORE", (0, 0), (0, -1), 3, cat_info["color"]),
            ("BACKGROUND", (0, 0), (-1, -1), HexColor("#fafafa")),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.3, BORDER_FAINT),
        ]))

        card_block = [card, Spacer(1, 3), HRFlowable(width="100%", thickness=0.3, color=BORDER_FAINT), Spacer(1, 5)]
        story.append(KeepTogether(card_block))

    # ─── Category Sections ───
    story.append(PageBreak())

    for cat_key in CATEGORY_ORDER:
        cat_articles_full = articles_by_category.get(cat_key, [])
        if not cat_articles_full:
            continue

        cat_info = CATEGORIES[cat_key]
        bookmark_key = f"cat_{cat_key}"

        # Sort by ranking score (relevance > importance)
        cat_sorted = sorted(cat_articles_full, key=ranking_score, reverse=True)

        # Exclude top picks from category sections
        cat_remaining = [a for a in cat_sorted if a["url"] not in top_pick_urls]

        # Category header bar with bookmark
        story.append(CategoryBar(
            cat_info["label"], len(cat_articles_full),
            cat_info["color"], cat_info["bg"],
            bookmark_key=bookmark_key,
        ))
        story.append(Spacer(1, 4))

        for idx, article in enumerate(cat_remaining[:MAX_PER_CATEGORY]):
            title = _clean_text(article.get("title", ""))
            summary = _clean_text(article.get("summary", article.get("description", "")))[:160]
            source = _clean_text(article.get("source", ""))
            url = article.get("url", "")

            bg_color = ZEBRA_LIGHT if idx % 2 == 0 else ZEBRA_WHITE

            title_html = f'<a href="{xml_escape(url)}"><font color="#1a1a1a"><b>{xml_escape(title)}</b></font></a>'
            art_content = [
                Paragraph(title_html, st_art_title),
                Paragraph(xml_escape(source), st_art_src),
            ]
            if summary:
                art_content.append(Paragraph(xml_escape(summary), st_art_sum))

            inner = Table([[e] for e in art_content], colWidths=[CONTENT_W - 14])
            inner.setStyle(TableStyle([
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))

            outer = Table([[inner]], colWidths=[CONTENT_W])
            outer.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), bg_color),
                ("LINEBEFORE", (0, 0), (0, -1), 3.5, cat_info["color"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))

            story.append(outer)
            story.append(Spacer(1, 1))

        remaining_count = len(cat_remaining) - MAX_PER_CATEGORY
        if remaining_count > 0:
            story.append(Spacer(1, 2))
            story.append(Paragraph(f"... and {remaining_count} more", st_more))

        story.append(Spacer(1, 8))

    # ─── Failed sources ───
    if failed_sources:
        story.append(Spacer(1, 6))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_LIGHT))
        story.append(Spacer(1, 3))
        shown = ", ".join(failed_sources[:5])
        more = f" +{len(failed_sources) - 5} more" if len(failed_sources) > 5 else ""
        story.append(Paragraph(
            f"⚠️ Failed sources ({len(failed_sources)}): {xml_escape(shown)}{more}",
            ParagraphStyle("F", parent=s["Normal"], fontSize=8, textColor=INK_LIGHTER),
        ))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return buffer.getvalue()