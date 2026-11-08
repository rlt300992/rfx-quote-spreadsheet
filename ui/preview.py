"""File preview dialog: photo, PDF, Excel (sheet selector), Word and email, each with a Download original button."""
from __future__ import annotations

import email
import email.policy
from pathlib import Path

import pandas as pd
import streamlit as st

from core.responses_view import file_kind, resolve_path

MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".eml": "message/rfc822"}


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def excel_sheets(path: Path) -> dict[str, pd.DataFrame]:
    """Every sheet as a table of display strings (values, not formulas)."""
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    out = {}
    for ws in wb.worksheets:
        rows = [["" if v is None else str(v) for v in r] for r in ws.iter_rows(values_only=True)]
        width = max((len(r) for r in rows), default=0)
        df = pd.DataFrame([r + [""] * (width - len(r)) for r in rows])
        df.columns = [_col_letter(i) for i in range(width)]
        df.index = range(1, len(df) + 1)
        out[ws.title] = df
    return out


def word_content(path: Path) -> tuple[list[str], list[pd.DataFrame]]:
    import docx

    d = docx.Document(str(path))
    paras = [p.text for p in d.paragraphs if p.text.strip()]
    tables = [pd.DataFrame([[c.text for c in r.cells] for r in t.rows]) for t in d.tables]
    return paras, tables


def email_parts(path: Path) -> dict:
    m = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    body = m.get_body(preferencelist=("plain", "html"))
    return {"From": str(m["From"] or ""), "To": str(m["To"] or ""), "Date": str(m["Date"] or ""), "Subject": str(m["Subject"] or ""),
            "body": body.get_content() if body is not None else "", "attachments": [a.get_filename() for a in m.iter_attachments()]}


def render_file(path: Path, key: str) -> None:
    kind = file_kind(path)
    if kind == "photo":
        st.image(str(path), width="stretch")
    elif kind == "pdf":
        st.pdf(path.read_bytes(), height=600)
    elif kind == "excel":
        sheets = excel_sheets(path)
        for tab, (name, df) in zip(st.tabs(list(sheets)), sheets.items()):
            with tab:
                st.dataframe(df, hide_index=False, width="stretch", height=480)
    elif kind == "word":
        paras, tables = word_content(path)
        for i, p in enumerate(paras, 1):
            st.markdown(f"{i}. {p}")
        for t in tables:
            st.dataframe(t, hide_index=True, width="stretch")
    elif kind == "email":
        e = email_parts(path)
        st.markdown(f"**From:** {e['From']}  \n**To:** {e['To']}  \n**Date:** {e['Date']}  \n**Subject:** {e['Subject']}")
        st.divider()
        st.text(e["body"])
        if e["attachments"]:
            st.caption("Attachments: " + ", ".join(a for a in e["attachments"] if a))
    else:
        st.info("No preview for this file type. Use Download original.")


def _body(file_path: str | None, file_name: str | None, caption: str) -> None:
    path = resolve_path(file_path, file_name)
    if caption:
        st.caption(caption + (f" · {len(excel_sheets(path))} sheets" if path is not None and file_kind(path) == "excel" else ""))
    if path is None:
        st.error("The original file is not available on this server.")
        return
    try:
        render_file(path, key=f"pv_{path.name}")
    except Exception as e:  # a corrupt file must not break the page
        st.error(f"Could not render this file: {e}")
    c = st.columns([3, 1.3])
    c[1].download_button("⬇ Download original", data=path.read_bytes(), file_name=path.name,
                         mime=MIME.get(path.suffix.lower(), "application/octet-stream"), key=f"pv_dl_{path.name}", width="stretch")


def preview_dialog(file_path: str | None, file_name: str | None, caption: str = "") -> None:
    """Open the preview dialog; its title is the file name, the caption says whose file it is."""
    st.dialog(file_name or "File preview", width="large")(_body)(file_path, file_name, caption)
