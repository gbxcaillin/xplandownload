"""The "suggested updates" PDF staff find in the client's SharePoint folder."""

from __future__ import annotations

import datetime as dt
import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

BRAND = colors.HexColor("#0f6e6a")
LINE = colors.HexColor("#dde2e5")
SOFT = colors.HexColor("#eef5f4")
MUTED = colors.HexColor("#5f6b72")
AMBER = colors.HexColor("#9c5700")

AREAS = {
    "personal": "Personal details", "contact": "Contact details", "address": "Address",
    "employment_income": "Employment and income", "super_accounts": "Super and investments",
    "insurance": "Insurance", "assets": "Assets", "liabilities": "Liabilities",
    "estate": "Estate planning", "goals": "Goals", "other": "Other",
}


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontName="Helvetica-Bold",
                                fontSize=18, leading=22, alignment=TA_LEFT, textColor=BRAND,
                                spaceAfter=2),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontSize=9.5, leading=13,
                              textColor=MUTED),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold",
                             fontSize=12.5, leading=16, textColor=colors.black, spaceBefore=10,
                             spaceAfter=4),
        "body": ParagraphStyle("b", parent=base["Normal"], fontSize=9.5, leading=13),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontSize=8.5, leading=11),
        "cellb": ParagraphStyle("cb", parent=base["Normal"], fontName="Helvetica-Bold",
                                fontSize=8.5, leading=11),
        "head": ParagraphStyle("hd", parent=base["Normal"], fontName="Helvetica-Bold",
                               fontSize=8, leading=10, textColor=MUTED),
        "note": ParagraphStyle("n", parent=base["Normal"], fontSize=8, leading=11,
                               textColor=MUTED),
    }


def _p(text, style) -> Paragraph:
    return Paragraph(escape(str(text if text not in (None, "") else "—")).replace("\n", "<br/>"),
                     style)


def render(review: dict, *, brand: str, client_name: str, client_ref: str | None,
           files: list[dict], generated: dt.datetime, record_found: bool) -> bytes:
    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm,
                            title=f"Suggested updates - {client_name}", author=f"{brand} vault")
    width = A4[0] - 32 * mm
    story = [
        _p(f"{brand}", st["sub"]),
        _p(f"Suggested client data updates: {client_name}", st["title"]),
        _p(f"{('Xplan / Brightly ref ' + client_ref + ' · ') if client_ref else ''}"
           f"{len(files)} document(s) received through the vault · prepared "
           f"{generated.astimezone().strftime('%d %B %Y, %I:%M %p')}", st["sub"]),
        Spacer(1, 6),
    ]
    banner = Table([[Paragraph(
        "<b>Suggestions only.</b> Prepared automatically by an AI reviewer. Check each item "
        "against the source document before changing the client's record. Tax file numbers and "
        "health details are never reproduced here; long account and ID numbers show only their "
        "last 4 digits." + ("" if record_found else
                            " <b>The client's current record wasn't available</b>, so "
                            "'currently on file' is blank: compare manually."), st["cell"])]],
        colWidths=[width])
    banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT),
                                ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                                ("TOPPADDING", (0, 0), (-1, -1), 6),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [banner, Spacer(1, 4)]

    if review.get("summary"):
        story += [_p("Summary", st["h2"]), _p(review["summary"], st["body"])]

    flags = review.get("flags") or []
    if flags:
        story.append(_p("Needs attention", st["h2"]))
        rows = [[_p("", st["head"]), _p("What", st["head"]), _p("Source", st["head"])]]
        for f in sorted(flags, key=lambda f: f.get("severity") != "action"):
            mark = "Action" if f.get("severity") == "action" else "Note"
            rows.append([_p(mark, st["cellb"]), _p(f.get("text"), st["cell"]),
                         _p(f.get("source_file"), st["cell"])])
        t = Table(rows, colWidths=[16 * mm, width - 66 * mm, 50 * mm], repeatRows=1)
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TEXTCOLOR", (0, 1), (0, -1), AMBER)]))
        story.append(t)

    updates = review.get("suggested_updates") or []
    story.append(_p("Suggested updates", st["h2"]))
    if not updates:
        story.append(_p("No changes to the client's details were found in these documents.",
                        st["body"]))
    by_area: dict[str, list[dict]] = {}
    for u in updates:
        by_area.setdefault(u.get("area") or "other", []).append(u)
    cols = [34 * mm, 36 * mm, 36 * mm, width - 136 * mm, 30 * mm]
    for area in list(AREAS) + [a for a in by_area if a not in AREAS]:
        items = by_area.get(area)
        if not items:
            continue
        rows = [[_p(h, st["head"]) for h in
                 ("Field", "Currently on file", "Suggested", "Why", "Source · confidence")]]
        for u in items:
            src = u.get("source_file") or ""
            if u.get("source_page"):
                src += f", p{u['source_page']}"
            rows.append([_p(u.get("field"), st["cellb"]), _p(u.get("current_value"), st["cell"]),
                         _p(u.get("suggested_value"), st["cellb"]), _p(u.get("reason"), st["cell"]),
                         _p(f"{src} · {u.get('confidence', '')}", st["cell"])])
        t = Table(rows, colWidths=cols, repeatRows=1)
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("BACKGROUND", (0, 0), (-1, 0), SOFT)]))
        story.append(KeepTogether([_p(AREAS.get(area, area.title()), st["cellb"]),
                                   Spacer(1, 2), t, Spacer(1, 6)]))

    docs = review.get("documents") or []
    if docs:
        story.append(_p("Documents reviewed", st["h2"]))
        rows = [[_p(h, st["head"]) for h in ("Document", "Type and date", "About", "In short")]]
        for d in docs:
            rows.append([_p(d.get("file"), st["cellb"]),
                         _p(f"{d.get('type') or ''}\n{d.get('date') or ''}", st["cell"]),
                         _p(d.get("about_whom"), st["cell"]), _p(d.get("summary"), st["cell"])])
        t = Table(rows, colWidths=[44 * mm, 32 * mm, 30 * mm, width - 106 * mm], repeatRows=1)
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(t)
    unreadable = review.get("unreadable") or []
    if unreadable:
        story += [_p("Couldn't be read", st["h2"]),
                  _p("; ".join(unreadable) + ". Please open these yourself.", st["body"])]

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(16 * mm, 10 * mm, f"{brand} · AI-prepared suggestions, verify before "
                                             f"updating · {client_name}")
        canvas.drawRightString(A4[0] - 16 * mm, 10 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
