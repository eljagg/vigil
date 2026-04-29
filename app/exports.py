"""CSV and PDF export of scan results."""
from __future__ import annotations

import csv
import datetime
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from sqlalchemy import desc

from .extensions import db
from .models import FileSnapshot, PathEntry, Scan, User
from .scanner import format_bytes, format_delta


def _scan_context(scan_id: int):
    scan = db.session.get(Scan, scan_id)
    if not scan:
        raise LookupError(f"Scan {scan_id} not found")
    pe = db.session.get(PathEntry, scan.path_entry_id)
    op = db.session.get(User, scan.operator_user_id)
    files = db.session.scalars(
        FileSnapshot.__table__.select()
        .where(FileSnapshot.scan_id == scan_id)
        .order_by(desc(FileSnapshot.size_bytes))
    ).all()
    # The above returned Row objects; switch to model objects:
    files = db.session.execute(
        db.select(FileSnapshot)
        .where(FileSnapshot.scan_id == scan_id)
        .order_by(desc(FileSnapshot.size_bytes))
    ).scalars().all()
    return scan, files, pe, op


def export_csv(scan_id: int) -> bytes:
    scan, files, pe, op = _scan_context(scan_id)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Vigil — backup integrity check"])
    w.writerow(["Path", pe.path])
    w.writerow(["Operator", f"{op.full_name} ({op.username})"])
    w.writerow(["Started", scan.started_at.isoformat() if scan.started_at else ""])
    w.writerow(["Completed", scan.completed_at.isoformat() if scan.completed_at else ""])
    w.writerow(["Status", scan.status])
    w.writerow(["File count", scan.file_count or 0])
    w.writerow(["Total bytes", scan.total_bytes or 0])
    w.writerow(["Encrypted-named", scan.encrypted_count or 0])
    w.writerow(["Plain-named", scan.plain_count or 0])
    w.writerow(["New files", scan.new_file_count or 0])
    w.writerow(["Grew", scan.grew_count or 0])
    w.writerow(["Shrunk", scan.shrunk_count or 0])
    w.writerow([])
    w.writerow([
        "Filename", "Relative path", "Size (bytes)", "Size (human)",
        "Previous size (bytes)", "Delta (bytes)", "Delta (human)",
        "Modified", "Modified (day)", "New?", "Encrypted-named?",
    ])
    for f in files:
        day = f.mtime.strftime("%A") if f.mtime else ""
        w.writerow([
            f.filename, f.relative_path, f.size_bytes, format_bytes(f.size_bytes),
            f.prev_size_bytes if f.prev_size_bytes is not None else "",
            f.size_delta_bytes if f.size_delta_bytes is not None else "",
            format_delta(f.size_delta_bytes),
            f.mtime.isoformat() if f.mtime else "",
            day,
            "yes" if f.is_new else "no",
            "yes" if f.is_encrypted_named else "no",
        ])
    return buf.getvalue().encode("utf-8")


def export_pdf(scan_id: int, company_name: str = "",
               footer_credit: str = "",
               tagline: str = "Backup integrity check.") -> bytes:
    scan, files, pe, op = _scan_context(scan_id)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(letter),
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        title=f"Vigil — Scan {scan_id}",
        author=op.full_name if op else "Vigil",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11, spaceAfter=2)
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=7,
                           textColor=colors.grey)

    story = []
    if company_name:
        story.append(Paragraph(f"<b>{company_name}</b>", h2))
    story.append(Paragraph(f"<b>Vigil</b> — {tagline}", h1))
    story.append(Paragraph(
        f"Generated {datetime.datetime.now().strftime('%A, %B %d, %Y %H:%M')}",
        small,
    ))
    story.append(Spacer(1, 0.15 * inch))

    meta = [
        ["Path", pe.path],
        ["Operator", f"{op.full_name} ({op.username})" if op else "—"],
        ["Started", scan.started_at.strftime("%a, %b %d %Y · %H:%M") if scan.started_at else "—"],
        ["Completed", scan.completed_at.strftime("%a, %b %d %Y · %H:%M") if scan.completed_at else "—"],
        ["Status", scan.status],
        ["Files", str(scan.file_count or 0)],
        ["Total size", format_bytes(scan.total_bytes or 0)],
        ["Encrypted-named", str(scan.encrypted_count or 0)],
        ["Plain-named", str(scan.plain_count or 0)],
        ["New files", str(scan.new_file_count or 0)],
        ["Grew / shrunk", f"{scan.grew_count or 0} / {scan.shrunk_count or 0}"],
    ]
    mt = Table(meta, colWidths=[1.6 * inch, 5 * inch])
    mt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f5f5f0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(mt)
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("File detail (sorted by size, largest first)", h2))
    headers = ["Filename", "Size", "Previous", "Delta", "Modified", "Day", "New", "Enc."]
    rows = [headers]
    for f in files:
        rows.append([
            f.filename[:48],
            format_bytes(f.size_bytes),
            format_bytes(f.prev_size_bytes) if f.prev_size_bytes is not None else "—",
            format_delta(f.size_delta_bytes),
            f.mtime.strftime("%Y-%m-%d %H:%M") if f.mtime else "",
            f.mtime.strftime("%a") if f.mtime else "",
            "✓" if f.is_new else "",
            "✓" if f.is_encrypted_named else "",
        ])
    tbl = Table(rows, colWidths=[2.6 * inch, 0.9 * inch, 0.9 * inch, 0.9 * inch,
                                  1.4 * inch, 0.6 * inch, 0.5 * inch, 0.5 * inch],
                repeatRows=1)
    style = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3C3489")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd")),
        ("ALIGN", (1, 1), (3, -1), "RIGHT"),
        ("ALIGN", (5, 1), (7, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    for i, f in enumerate(files, start=1):
        if f.is_new:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#FAEEDA")))
        elif f.size_delta_bytes is not None and f.size_delta_bytes < 0:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#FCEBEB")))
        elif f.size_delta_bytes is not None and f.size_delta_bytes > 0:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#EAF3DE")))
    tbl.setStyle(TableStyle(style))
    story.append(tbl)

    if footer_credit:
        story.append(Spacer(1, 0.2 * inch))
        story.append(Paragraph(footer_credit, small))

    doc.build(story)
    return buf.getvalue()
