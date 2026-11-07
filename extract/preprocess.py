"""Convert-first preprocessing (CLAUDE.md step 1).

xlsx -> cell-addressed text ("Sheet!E12: 887", merged ranges noted); docx -> Markdown with numbered
paragraphs and tables; eml -> headers + body (+ attachment names). PDF and JPG are NOT converted: they go
to Gemini natively as bytes.
"""
from __future__ import annotations

import email
import email.policy
from dataclasses import dataclass
from pathlib import Path

MIME = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
SOURCE_TYPE = {".xlsx": "excel", ".docx": "word", ".eml": "email_text", ".pdf": "pdf", ".jpg": "photo",
               ".jpeg": "photo", ".png": "photo"}
PATH_LABEL = {".xlsx": "xlsx→celltext", ".docx": "docx→markdown", ".eml": "eml→text", ".pdf": "pdf→native",
              ".jpg": "jpg→native", ".jpeg": "jpg→native", ".png": "png→native"}


@dataclass
class Prepared:
    path: Path
    source_type: str            # excel / word / email_text / pdf / photo
    path_label: str             # e.g. 'xlsx→celltext'
    text: str | None            # converted text, or None for native files
    mime_type: str | None       # set for native files
    data: bytes | None          # raw bytes for native files


def xlsx_to_celltext(path: str | Path) -> str:
    """One line per non-empty cell, `Sheet!A1: value`, plus a header line per sheet listing merged ranges.

    Merged cells appear once at their top-left address (that is where the value lives); the merge range
    is listed so the model knows a title or section row spans several columns.
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    out: list[str] = []
    for ws in wb.worksheets:
        out.append(f"## Sheet: {ws.title} (dimensions {ws.dimensions})")
        merged = sorted(str(r) for r in ws.merged_cells.ranges)
        if merged:
            out.append(f"Merged ranges (value is at the top-left cell): {', '.join(merged)}")
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None and str(c.value).strip() != "":
                    out.append(f"{ws.title}!{c.coordinate}: {c.value}")
    return "\n".join(out)


def docx_to_markdown(path: str | Path) -> str:
    """Body in document order: numbered paragraphs (P1, P2, ...) and tables as Markdown with row numbers."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    out: list[str] = []
    pn = tn = 0
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, d).text.strip()
            if text:
                pn += 1
                out.append(f"[P{pn}] {text}")
        elif tag == "tbl":
            tn += 1
            t = Table(child, d)
            rows = [[c.text.strip().replace("\n", " ") for c in r.cells] for r in t.rows]
            if not rows:
                continue
            out.append(f"[Table {tn}]")
            out.append("| Row | " + " | ".join(rows[0]) + " |")
            out.append("|" + "---|" * (len(rows[0]) + 1))
            for i, r in enumerate(rows[1:], start=1):
                out.append(f"| {i} | " + " | ".join(r) + " |")
    return "\n\n".join(out)


def eml_to_text(path: str | Path) -> str:
    msg = email.message_from_binary_file(open(path, "rb"), policy=email.policy.default)
    head = [f"{k}: {msg[k]}" for k in ("From", "To", "Date", "Subject", "Attachments") if msg[k]]
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:  # decode bytes ourselves: many mails omit the charset and the rupee sign would be mangled
        raw = body.get_payload(decode=True) or b""
        charset = body.get_content_charset() or "utf-8"
        try:
            text = raw.decode(charset)
        except (UnicodeDecodeError, LookupError):
            text = raw.decode("utf-8", errors="replace")
    names = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    if names:
        head.append("Attached files: " + ", ".join(names))
    return "\n".join(head) + "\n\n" + str(text).strip()


def prepare(path: str | Path) -> Prepared:
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in SOURCE_TYPE:
        raise ValueError(f"Unsupported file type: {p.name}")
    if ext == ".xlsx":
        return Prepared(p, "excel", PATH_LABEL[ext], xlsx_to_celltext(p), None, None)
    if ext == ".docx":
        return Prepared(p, "word", PATH_LABEL[ext], docx_to_markdown(p), None, None)
    if ext == ".eml":
        return Prepared(p, "email_text", PATH_LABEL[ext], eml_to_text(p), None, None)
    return Prepared(p, SOURCE_TYPE[ext], PATH_LABEL[ext], None, MIME[ext], p.read_bytes())
