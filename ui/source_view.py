"""Source viewer for Review & Reconcile: the original file with the cited place highlighted.
PDF: page image with the cited row marked (word coordinates from the PDF text). Excel: the sheet around the cited cell.
Word: the cited paragraph or table row. Email: the body with the cited phrase marked. Photo: the image and the row reference."""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
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


# ------------------------------------------------------------------ Excel
def _excel_view(path: Path, ref: str) -> tuple[pd.io.formats.style.Styler | None, str]:
    import openpyxl
    from openpyxl.utils import get_column_letter

    m = re.match(r"(?:'?(?P<sheet>[^'!]+)'?!)?(?P<c1>[A-Z]+)(?P<r1>\d+)(?::(?P<c2>[A-Z]+)(?P<r2>\d+))?", ref or "")
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[m.group("sheet")] if m and m.group("sheet") in wb.sheetnames else wb.worksheets[0]
    row = int(m.group("r1")) if m else 1
    lo, hi = max(1, row - 4), min(ws.max_row, row + 3)
    width = ws.max_column
    data = [["" if v is None else str(v) for v in r] for r in ws.iter_rows(min_row=lo, max_row=hi, max_col=width, values_only=True)]
    df = pd.DataFrame(data, index=range(lo, hi + 1), columns=[get_column_letter(i) for i in range(1, width + 1)])

    def hl(r):
        return ["background-color:#ffe28a;color:#000" if r.name == row else "" for _ in r]

    return df.style.apply(hl, axis=1), f"row {row} of sheet “{ws.title}”" + (f" ({ref.split('!')[-1]})" if ref else "")


# ------------------------------------------------------------------ Word / email
def _word_view(path: Path, snippet: str) -> tuple[list[str], str, pd.DataFrame | None, int | None]:
    import docx

    d = docx.Document(str(path))
    paras = [p.text for p in d.paragraphs if p.text.strip()]
    want = _norm(snippet.strip("| "))
    for i, p in enumerate(paras):
        if want and (want in _norm(p) or _norm(p) in want and len(_norm(p)) > 25):
            lo = max(0, i - 2)
            return [(f":orange-background[{t}]" if j == i else t) for j, t in enumerate(paras[lo:i + 3], lo)], f"paragraph {i + 1}", None, None
    cells = [c.strip() for c in snippet.split("|") if c.strip()]
    for t in d.tables:
        for ri, r in enumerate(t.rows):
            texts = [c.text.strip() for c in r.cells]
            if cells and all(c in texts for c in cells[:2]):
                df = pd.DataFrame([[c.text for c in rr.cells] for rr in t.rows])
                lo = max(0, ri - 3)
                return [], f"table row {ri + 1}", df.iloc[lo:ri + 4], ri - lo
    return paras[:6], "(cited text not found; showing the start of the file)", None, None


def render_source(row: dict | None, ref: str | None, snippet: str | None, header: str = "") -> None:
    """Draw the viewer card for one cited value."""
    if row is None or not row.get("file_name"):
        st.info("Select an item to see its source here.")
        return
    path = resolve_path(row.get("file_path"), row.get("file_name"))
    kind = file_kind(path) if path else "other"
    top = st.columns([5, 1.4], vertical_alignment="center")
    page_txt = ""
    what = ""
    ref = ref or ""
    snippet = snippet or ""
    if path is None:
        top[0].markdown(f"**{row['file_name']}**")
        st.error("The original file is not available on this server.")
        return
    try:
        if kind == "pdf":
            m = re.search(r"page (\d+)", ref)
            png, n, found = pdf_page_png(str(path), int(m.group(1)) if m else 1, snippet)
            page_txt = f" · page {int(m.group(1)) if m else 1} of {n}"
            top[0].markdown(f"**{row['file_name']}**{page_txt}")
            st.image(png, width="stretch")
            what = f"Highlighted: {ref}" + (f" · found {found} in the PDF text" if found else " · not located in the PDF text")
        elif kind == "excel":
            styler, found = _excel_view(path, ref)
            top[0].markdown(f"**{row['file_name']}**" + (f" · sheet “{ref.split('!')[0]}”" if "!" in ref else ""))
            st.dataframe(styler, width="stretch", height=300)
            what = f"Highlighted: {found}"
        elif kind == "word":
            before, found, table, hl = _word_view(path, snippet)
            top[0].markdown(f"**{row['file_name']}**")
            for t in before:
                st.markdown(t)
            if table is not None:
                st.dataframe(table.style.apply(lambda r: ["background-color:#ffe28a;color:#000" if list(table.index).index(r.name) == hl else "" for _ in r], axis=1),
                             hide_index=True, width="stretch")
            what = f"Highlighted: {found} ({ref})"
        elif kind == "email":
            from ui.preview import email_parts
            e = email_parts(path)
            body = e["body"]
            i = body.lower().find(_norm(snippet)[:40]) if snippet else -1
            top[0].markdown(f"**{row['file_name']}** · email")
            st.markdown(f"**From:** {e['From']}  \n**Subject:** {e['Subject']}")
            if i >= 0:
                j = i + len(snippet)
                st.markdown((body[:i] + f":orange-background[{body[i:j]}]" + body[j:]).replace("\n", "  \n"))
            else:
                st.text(body)
            what = f"Highlighted: “{snippet[:70]}” ({ref})"
        elif kind == "photo":
            top[0].markdown(f"**{row['file_name']}** · photo")
            st.image(str(path), width="stretch")
            what = f"{ref or 'Row reference'}: {snippet}. Photos can't be highlighted precisely; zoom to check."
        else:
            top[0].markdown(f"**{row['file_name']}**")
            st.info("No preview for this file type.")
    except Exception as ex:  # a corrupt file or an unexpected reference must not break the page
        st.warning(f"Could not draw the highlight ({type(ex).__name__}). The original is available to download.")
    top[1].download_button("⬇ Original", data=path.read_bytes(), file_name=path.name, mime=MIME.get(path.suffix.lower(), "application/octet-stream"),
                           key=f"rv_dl_{path.name}", type="tertiary")
    if what:
        st.caption(what)
