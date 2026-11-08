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


def excel_sheets(path: Path) -> dict[str, pd.DataFrame]:
    """Every sheet as a table of display strings (values, not formulas)."""
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    out = {}
    for ws in wb.worksheets:
        rows = [["" if v is None else str(v) for v in r] for r in ws.iter_rows(values_only=True)]
        width = max((len(r) for r in rows), default=0)
        out[ws.title] = pd.DataFrame([r + [""] * (width - len(r)) for r in rows])
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
        name = st.selectbox("Sheet", list(sheets), key=f"{key}_sheet") if len(sheets) > 1 else next(iter(sheets))
        st.dataframe(sheets[name], hide_index=True, width="stretch", height=480, column_config=None)
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


@st.dialog("File preview", width="large")
def preview_dialog(file_path: str | None, file_name: str | None) -> None:
    path = resolve_path(file_path, file_name)
    st.markdown(f"**{file_name or (path.name if path else 'file')}**")
    if path is None:
        st.error("The original file is not available on this server.")
        return
    try:
        render_file(path, key=f"pv_{path.name}")
    except Exception as e:  # a corrupt file must not break the page
        st.error(f"Could not render this file: {e}")
    st.download_button("Download original", data=path.read_bytes(), file_name=path.name, mime=MIME.get(path.suffix.lower(), "application/octet-stream"),
                       key=f"pv_dl_{path.name}")
