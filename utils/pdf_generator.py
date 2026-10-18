import re
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

NAVY = colors.HexColor("#1E407C")
GRAY = colors.HexColor("#6B7280")
RULE = colors.HexColor("#D1D5DB")

_REPLACEMENTS = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
    "•": "-", "×": "x", "−": "-",
}


def clean(text):
    """Make text safe for reportlab's built-in Latin-1 fonts and its XML-ish paragraph markup."""
    text = str(text or "")
    for src, dst in _REPLACEMENTS.items():
        text = text.replace(src, dst)
    text = text.encode("latin-1", "ignore").decode("latin-1")
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return escape(text)


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("T", parent=base["Title"], textColor=NAVY, fontSize=22, leading=26, alignment=TA_LEFT, spaceAfter=2),
        "sub": ParagraphStyle("S", parent=base["Normal"], textColor=GRAY, fontSize=10, spaceAfter=10),
        "h": ParagraphStyle("H", parent=base["Heading2"], textColor=NAVY, fontSize=14, spaceBefore=12, spaceAfter=6),
        "body": ParagraphStyle("B", parent=base["Normal"], fontSize=10.5, leading=14),
        "cell": ParagraphStyle("C", parent=base["Normal"], fontSize=10, leading=13),
        "q": ParagraphStyle("Q", parent=base["Normal"], fontSize=11, leading=15, fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=4),
        "small": ParagraphStyle("Sm", parent=base["Normal"], fontSize=9, leading=12, textColor=GRAY),
        "who": ParagraphStyle("W", parent=base["Normal"], fontSize=9, leading=12, textColor=NAVY, fontName="Helvetica-Bold"),
    }


def _scratch_box(height=0.9 * inch):
    box = Table([[""]], colWidths=[6.5 * inch], rowHeights=[height])
    box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.75, RULE)]))
    return box


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(GRAY)
    canvas.drawString(inch, 0.55 * inch, "Socratica - practice only, not for submission")
    canvas.drawRightString(letter[0] - inch, 0.55 * inch, f"Page {doc.page}")
    canvas.restoreState()


def create_study_worksheet(package, course, include_questions=True, include_definitions=True, transcript=None):
    """Render a StudyPackage as a printable PDF worksheet and return it as a BytesIO."""
    s = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, leftMargin=inch, rightMargin=inch,
        topMargin=0.8 * inch, bottomMargin=0.9 * inch,
        title=f"{clean(course)} Study Package", author="Socratica",
    )

    story = [
        Paragraph(f"{clean(course)} Study Package", s["title"]),
        Paragraph("Review what you worked through, then try the practice questions on your own.", s["sub"]),
        Paragraph("Session Summary", s["h"]),
        Paragraph(clean(package.session_summary), s["body"]),
    ]

    if include_definitions and package.concepts:
        rows = [[Paragraph("<b>Term</b>", s["cell"]), Paragraph("<b>Definition</b>", s["cell"])]]
        for c in package.concepts:
            rows.append([Paragraph(f"<b>{clean(c.term)}</b>", s["cell"]), Paragraph(clean(c.definition), s["cell"])])
        table = Table(rows, colWidths=[1.7 * inch, 4.8 * inch], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF7")),
            ("GRID", (0, 0), (-1, -1), 0.5, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story += [Paragraph("Key Concepts", s["h"]), table]

    if package.pitfalls:
        story.append(Paragraph("Common Pitfalls", s["h"]))
        for p in package.pitfalls:
            story.append(Paragraph(f"- {clean(p)}", s["body"]))
            story.append(Spacer(1, 3))

    if include_questions and package.practice_questions:
        story.append(Paragraph("Practice Questions", s["h"]))
        story.append(Paragraph("Try each one without looking at the hints. Hints are on the last page of this section.", s["small"]))
        for i, q in enumerate(package.practice_questions, 1):
            story.append(KeepTogether([
                Paragraph(f"{i}. {clean(q.question)}", s["q"]),
                _scratch_box(),
            ]))

        story.append(Paragraph("Hints and Guided Steps", s["h"]))
        for i, q in enumerate(package.practice_questions, 1):
            block = [Paragraph(f"<b>Question {i} hint:</b> {clean(q.hint)}", s["body"])]
            for step in q.guidance_steps:
                block.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&bull; {clean(step)}", s["body"]))
            block.append(Spacer(1, 6))
            story.append(KeepTogether(block))

        story.append(Paragraph("Self-Check", s["h"]))
        for item in (
            "I tried every question before reading a hint.",
            "I can explain the main idea in my own words.",
            "I know which question to bring to office hours or tutoring.",
        ):
            story.append(Paragraph(f"[ &nbsp;] {clean(item)}", s["body"]))
            story.append(Spacer(1, 3))

    if transcript:
        story.append(Paragraph("Appendix: Session Transcript", s["h"]))
        for speaker, text in transcript:
            story.append(Paragraph(clean(speaker), s["who"]))
            story.append(Paragraph(clean(text).replace("\n", "<br/>"), s["body"]))
            story.append(Spacer(1, 6))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buffer.seek(0)
    return buffer
