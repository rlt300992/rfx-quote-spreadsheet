"""Source viewer for Review & Reconcile: the original file on a white page with the cited place highlighted.
PDF: page image with the cited row boxed (word coordinates from the PDF text). Excel: the relevant columns around the cited row.
Word: the cited paragraph or table row. Email: the body with the phrase marked. Photo: the image and the row reference."""
from __future__ import annotations

import html
import re
from pathlib import Path

import streamlit as st

from core.responses_view import file_kind, resolve_path

MIME = {".pdf": "application/pdf", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".eml": "message/rfc822",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


# ------------------------------------------------------------------ PDF
@st.cache_data(show_spinner=False, max_entries=64)
def pdf_page_png(path: str, page_no: int, needle: str, resolution: int = 110) -> tuple[bytes | None, int, str]:
    """(PNG bytes with the cited row highlighted, number of pages, what was highlighted)."""
    import io

    import pdfplumber

    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        page = pdf.pages[min(max(page_no, 1), n) - 1]
        box = None
        what = ""
        cells = [c.strip() for c in (needle or "").split("|") if c.strip()]
        for probe in sorted(cells, key=len, reverse=True)[:3] + ([needle] if needle else []):
            try:
                hits = page.search(re.escape(probe), regex=True, case=False)
            except Exception:
                hits = []
            if hits:
                h = hits[0]
                box = (page.bbox[0] + 8, h["top"] - 2, page.bbox[2] - 8, h["bottom"] + 2)
                what = f"“{probe[:60]}”"
                break
        im = page.to_image(resolution=resolution)
        if box:
            im.draw_rect(box, fill=(255, 220, 0, 70), stroke=(255, 140, 0, 255), stroke_width=2)
        buf = io.BytesIO()
        im.original.save(buf, format="PNG")
        return buf.getvalue(), n, what


# ------------------------------------------------------------------ Excel (an HTML table on white, like the original sheet)
def _excel_html(path: Path, ref: str) -> tuple[str, str, str]:
    """(html table, sheet name, 'row 23 (cells A23:G23)')."""
    import openpyxl
    from openpyxl.utils import column_index_from_string, get_column_letter

    m = re.match(r"(?:'?(?P<sheet>[^'!]+)'?!)?(?P<c1>[A-Z]+)(?P<r1>\d+)(?::(?P<c2>[A-Z]+)(?P<r2>\d+))?", ref or "")
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[m.group("sheet")] if m and m.group("sheet") in wb.sheetnames else wb.worksheets[0]
    row = int(m.group("r1")) if m else 1
    lo, hi = max(1, row - 4), min(ws.max_row, row + 3)
    data = [[("" if v is None else str(v)) for v in r] for r in ws.iter_rows(min_row=lo, max_row=hi, max_col=ws.max_column, values_only=True)]
    keep = [i for i in range(ws.max_column) if any(r[i] for r in data)]          # only columns with something in the window
    cited = set(range(column_index_from_string(m.group("c1")) - 1, column_index_from_string(m.group("c2") or m.group("c1")))) if m else set()
    head = "<tr class='h'><td class='n'></td>" + "".join(f"<td>{get_column_letter(i + 1)}</td>" for i in keep) + "</tr>"
    body = ""
    for k, r in enumerate(data):
        n = lo + k
        body += f"<tr class='{'hl' if n == row else ''}'><td class='n'>{n}</td>" + "".join(f"<td title='{html.escape(r[i])}'>{html.escape(r[i])}</td>" for i in keep) + "</tr>"
    cells = f"{get_column_letter(min(cited) + 1)}{row}:{get_column_letter(max(cited) + 1)}{row}" if cited else f"row {row}"
    return f"<table class='rr-xl'>{head}{body}</table>", ws.title, f"row {row} (cells {cells})"


# ------------------------------------------------------------------ Word
def _word_html(path: Path, snippet: str) -> tuple[str, str]:
    import docx

    d = docx.Document(str(path))
    paras = [p.text for p in d.paragraphs if p.text.strip()]
    want = _norm(snippet.strip("| "))
    for i, p in enumerate(paras):
        if want and (want in _norm(p) or (_norm(p) in want and len(_norm(p)) > 25)):
            lo = max(0, i - 2)
            return "".join(f"<p>{'<mark>' + html.escape(t) + '</mark>' if j == i else html.escape(t)}</p>" for j, t in enumerate(paras[lo:i + 3], lo)), f"paragraph {i + 1}"
    cells = [c.strip() for c in snippet.split("|") if c.strip()]
    for t in d.tables:
        for ri, r in enumerate(t.rows):
            texts = [c.text.strip() for c in r.cells]
            if cells and all(c in texts for c in cells[:2]):
                lo = max(0, ri - 3)
                rows = ""
                for k, rr in enumerate(t.rows[lo:ri + 4], lo):
                    hl = " class='hl'" if k == ri else ""
                    rows += f"<tr{hl}>" + "".join(f"<td>{html.escape(c.text)}</td>" for c in rr.cells) + "</tr>"
                return f"<table class='rr-xl'>{rows}</table>", f"table row {ri + 1}"
    return "".join(f"<p>{html.escape(t)}</p>" for t in paras[:6]), "(cited text not found; showing the start of the file)"


def render_source(row: dict | None, ref: str | None, snippet: str | None) -> None:
    """Draw the viewer card for one cited value: header, white body, footer."""
    if row is None or not row.get("file_name"):
        st.info("Select an item to see its source here.")
        return
    path = resolve_path(row.get("file_path"), row.get("file_name"))
    ref, snippet = ref or "", snippet or ""
    with st.container(key="rr_src"):
        top = st.columns([5, 1.3], vertical_alignment="center")
        if path is None:
            top[0].markdown(f"**{row['file_name']}**")
            st.error("The original file is not available on this server.")
            return
        kind = file_kind(path)
        meta = ""
        body_html = ""
        what = ""
        try:
            if kind == "pdf":
                m = re.search(r"page (\d+)", ref)
                png, n, found = pdf_page_png(str(path), int(m.group(1)) if m else 1, snippet)
                meta = f"page {int(m.group(1)) if m else 1} of {n}"
                what = f"Highlighted: {ref}" + (f" · found {found} in the PDF text" if found else " · not located in the PDF text")
            elif kind == "excel":
                body_html, sheet, found = _excel_html(path, ref)
                meta = f"sheet “{sheet}”"
                what = f"Highlighted: {found}"
            elif kind == "word":
                body_html, found = _word_html(path, snippet)
                what = f"Highlighted: {found} ({ref})"
            elif kind == "email":
                from ui.preview import email_parts
                e = email_parts(path)
                text = e["body"]
                i = text.lower().find(_norm(snippet)[:40]) if snippet else -1
                safe = html.escape(text)
                if i >= 0:
                    j = i + len(snippet)
                    safe = html.escape(text[:i]) + "<mark>" + html.escape(text[i:j]) + "</mark>" + html.escape(text[j:])
                body_html = f"<div class='rr-doc'><b>From:</b> {html.escape(e['From'])}<br><b>Subject:</b> {html.escape(e['Subject'])}<hr>{safe.replace(chr(10), '<br>')}</div>"
                meta = "email"
                what = f"Highlighted: “{snippet[:70]}” ({ref})"
            elif kind == "photo":
                meta = "photo"
                what = f"{ref or 'Row reference'}: {snippet}. Photos can't be highlighted precisely; zoom to check."
        except Exception as ex:  # a corrupt file or an unexpected reference must not break the page
            st.warning(f"Could not draw the highlight ({type(ex).__name__}). The original is available to download.")
        top[0].html(f"<div class='rr-src-hd'><b>{html.escape(row['file_name'])}</b> <span style='color:var(--rr-mut)'>· {html.escape(meta)}</span></div>")
        top[1].download_button("⬇ Original", data=path.read_bytes(), file_name=path.name, mime=MIME.get(path.suffix.lower(), "application/octet-stream"),
                               key=f"rv_dl_{path.name}", type="tertiary")
        if kind == "pdf" and "png" in locals() and png:
            st.image(png, width="stretch")
        elif kind == "photo":
            st.image(str(path), width="stretch")
        elif body_html:
            st.html(f"<div class='rr-src-bd'>{body_html}</div>")
        if what:
            st.html(f"<div class='rr-src-ft'>{html.escape(what)}</div>")
