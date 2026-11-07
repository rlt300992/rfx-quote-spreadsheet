"""Synthetic data, Task 2: five messy vendor replies, attachments, inbox manifest and answer key.
Everything is fictional. The answer key holds what each vendor *meant*, so extraction accuracy can be measured."""
import csv, json, os, random, math
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from docx import Document
from docx.shared import Pt
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import ParagraphStyle

random.seed(7)
BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SEED, INBOX, KEY = (os.path.join(BASE, d) for d in ("seed", "inbox", "answer_key"))
os.makedirs(INBOX, exist_ok=True); os.makedirs(KEY, exist_ok=True)

pdfmetrics.registerFont(TTFont("DV", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("DVB", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
FX = 95.17

def read(name):
    with open(os.path.join(SEED, name), encoding="utf-8") as f:
        return list(csv.DictReader(f))
LINES = read("line_items.csv")
for l in LINES:
    for k in ("line_no", "length_mm", "width_mm", "height_mm", "ply", "print_colours", "annual_qty", "typical_order_qty"):
        l[k] = int(l[k])
    l["theoretical_weight_kg"] = float(l["theoretical_weight_kg"])
    l["layers"] = json.loads(l["layers_json"])
LY = {(int(r["vendor_id"]), r["catalog_sku"]): r for r in read("last_year_prices.csv")}

def size(l): return f'{l["length_mm"]}x{l["width_mm"]}x{l["height_mm"]}'
def gsm_str(layers): return "/".join(str(x["gsm"]) for x in layers)
def jitter(): return random.uniform(0.97, 1.03)
def base_inr(l, rate_kg, adder): return l["theoretical_weight_kg"] * rate_kg * jitter() + l["print_colours"] * adder

KEYROWS = []  # answer key: one row per vendor x line
def key(v, l, **kw):
    row = {"vendor_code": v, "line_no": l["line_no"], "catalog_sku": l["catalog_sku"], "quote_status": "quoted",
           "raw_price_text": "", "raw_price": "", "raw_unit": "per_piece", "raw_currency": "INR",
           "true_base_inr_pc": "", "freight_basis": "", "true_freight_inr_pc": "", "tax_basis": "ex_gst",
           "gst_rate_pct": 18, "spec_match": "exact", "expected_flags": "", "notes": ""}
    row.update(kw); KEYROWS.append(row)

# =====================================================================================
# V1  Deccan Corrugators - beautiful Excel that ignores the template; prices per 100 pcs
# =====================================================================================
def build_v1():
    wb = Workbook(); ws = wb.active; ws.title = "Price Offer"
    thin = Side(style="thin", color="999999"); box = Border(left=thin, right=thin, top=thin, bottom=thin)
    ws.merge_cells("A1:G1"); ws["A1"] = "DECCAN CORRUGATORS PVT LTD"
    ws["A1"].font = Font(size=16, bold=True, color="1F4E79"); ws["A1"].alignment = Alignment(horizontal="center")
    ws.merge_cells("A2:G2"); ws["A2"] = "Gat No. 212, Chakan Talegaon Road, Chakan, Pune 410501 | GSTIN 27AADCD4417K1Z9"
    ws["A2"].alignment = Alignment(horizontal="center")
    ws.merge_cells("A3:G3"); ws["A3"] = "COMMERCIAL OFFER - Ref DCPL/SCPL/26-27/118 dt 10.11.2026 - Kind Attn: Purchase Dept, Sahyadri Consumer Products"
    ws["A3"].font = Font(italic=True)
    hdr = ["Our Code", "Box Description", "Inside Size (mm)", "Construction", "Rate / 100 Nos (Rs.)", "Delivery", "Remarks"]
    ws.append([]); ws.append(hdr)
    for c in ws[5]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="1F4E79"); c.border = box
        c.alignment = Alignment(wrap_text=True, horizontal="center")
    # their own order: 7-ply first, then 5, then 3; own codes; paraphrased descriptions
    order = sorted(LINES, key=lambda l: (-l["ply"], l["line_no"]))
    section = None; r = 6
    for i, l in enumerate(order, start=1):
        if l["ply"] != section:
            section = l["ply"]
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
            ws.cell(r, 1, f"{section} PLY CARTONS").font = Font(bold=True)
            ws.cell(r, 1).fill = PatternFill("solid", fgColor="DDEBF7"); r += 1
        layers, remark, spec, flags = l["layers"], "", "exact", []
        price = base_inr(l, 46.0, 0.35)                                 # FOR Chakan, freight included
        if l["line_no"] == 16:                                          # deviation: lighter outer liners
            layers = [dict(x, gsm=150) if x["role"] == "liner" and x["pos"] in (1, 5) else x for x in layers]
            remark, spec, flags = "Offered in 150 GSM outer liner - meets BS", "deviation", ["SPEC_DEVIATION"]
            price *= 0.95
        if l["line_no"] == 25:                                          # substitute: 3-ply heavy instead of 5-ply
            remark, spec, flags = "We suggest 3 ply heavy duty 200/150/200 - cost effective alternative", "substitute", ["SPEC_SUBSTITUTE"]
            price = l["theoretical_weight_kg"] * 0.62 * 46.0
        if l["line_no"] in (1, 3, 9):
            remark = (remark + "; " if remark else "") + "* Addl 4% off for release qty >= 20,000 nos"
            flags.append("TIER_NOT_MET_PER_ORDER")                      # D1 trap: annual qty qualifies, order qty does not
        if l["line_no"] == 19:
            remark = "3% volume discount above 50,000 nos"              # basis unclear (per order or annual?)
            flags.append("TIER_BASIS_UNCLEAR")
        per100 = round(price * 100, 0)
        constr = f'{l["ply"] if l["line_no"] != 25 else 3} Ply {gsm_str(layers) if l["line_no"] != 25 else "200/150/200"}'
        desc = l["description"].replace(" carton", " box").replace(" shipper", " shipper box").upper()
        ws.append([f"DCPL-C{100 + i}", desc, size(l).replace("x", " x "), constr, per100, "FOR Chakan", remark])
        for c in ws[r]: c.border = box
        r += 1
        key("V1", l, raw_price_text=f"Rs {per100:.0f} / 100 Nos", raw_price=per100, raw_unit="per_100",
            true_base_inr_pc=round(per100 / 100, 4), freight_basis="included", true_freight_inr_pc=0,
            spec_match=spec, expected_flags=";".join(flags),
            notes="Tier: 4% off only if order >= 20,000; our order qty is lower -> no tier" if l["line_no"] in (1, 3, 9) else remark)
    r += 1
    notes = ["TERMS & CONDITIONS:", "1. Above rates are FOR your Chakan plant, i.e. inclusive of freight.",
             "2. GST @ 18% extra as applicable.", "3. Payment: 60 days from date of invoice.",
             "4. Lead time: 10 days from PO + artwork approval. MOQ 2,000 nos per size.",
             "5. Offer valid for 30 days. Prices firm for 6 months from PO.",
             "6. ISO 9001 certificate and BS test report attached."]
    for n in notes:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7); ws.cell(r, 1, n); r += 1
    for col, w in zip("ABCDEFG", (11, 42, 18, 26, 14, 12, 50)): ws.column_dimensions[col].width = w
    q = wb.create_sheet("Vendor Questionnaire")
    q.append(["Sr", "Question (as received)", "Our Response"])
    for row in [(1, "ISO 9001 certified?", "Yes - Cert No. QMS/IN/22814, valid till Aug 2027 (copy attached)"),
                (2, "BS test report", "Yes, all grades meet BS. Test report dt 20-Sep-2026 attached"),
                (3, "Rejection / replacement", "Agreed"),
                (4, "Capacity (MT/month)", "260 MT per month available"),
                (5, "First supply lead time", "10 days"),
                (6, "Paper source", "Mixed (virgin kraft liner + recycled fluting)"),
                (7, "FSC CoC", "Not certified"),
                (8, "Complaints last 12 months", "1 (resolved)")]:
        q.append(row)
    q.column_dimensions["B"].width = 30; q.column_dimensions["C"].width = 70
    wb.save(os.path.join(INBOX, "V1_Deccan_Price_Offer_SCPL.xlsx"))

# =====================================================================================
# V2  Shree Ganesh - PDF on letterhead; discount in footnote; "Rate/box" but 3 lines are per 100
# =====================================================================================
AMBIG_PER100 = {6, 8, 12}
def build_v2():
    S = lambda **k: ParagraphStyle("s", fontName=k.pop("f", "DV"), fontSize=k.pop("sz", 9), leading=k.pop("ld", 12), **k)
    story = [Paragraph("SHREE GANESH PACKAGING INDUSTRIES", S(f="DVB", sz=16, ld=20, textColor=colors.HexColor("#8B0000"))),
             Paragraph("Manufacturers of Corrugated Boxes &amp; Sheets | J-Block, Bhosari MIDC, Pune 411026 | GSTIN 27ABRFS2281M1ZQ", S(sz=8)),
             Paragraph("Udyam Regd. (Small Enterprise): UDYAM-MH-26-0458831 | quotes@shreeganeshpack.example", S(sz=8)),
             Spacer(1, 8), Paragraph("Ref: SGPI/Q/1127 &nbsp;&nbsp;&nbsp; Date: 12/11/2026", S()),
             Paragraph("To, The Purchase Manager, Sahyadri Consumer Products Ltd, Chakan MIDC, Pune", S()),
             Paragraph("Sub: Quotation against your RFQ for Corrugated Boxes FY 2026-27", S(f="DVB")), Spacer(1, 6),
             Paragraph("Dear Sir, with reference to your enquiry, we are pleased to submit our best rates as under:", S()), Spacer(1, 6)]
    data = [["Sr", "Item", "Size (mm)", "Ply", "Rate/box (₹)*"]]
    for l in LINES:
        n = l["line_no"]
        price = base_inr(l, 43.5, 0.30)
        if n in AMBIG_PER100:
            shown, unit, flags, note = round(price * 100, 0), "per_100", ["UNIT_AMBIGUOUS", "SANITY_RANGE_FAIL"], "Column says Rate/box but value is per 100 pcs"
        else:
            shown, unit, flags, note = round(price, 2), "per_piece", [], ""
        data.append([str(n), l["description"][:38], size(l), f'{l["ply"]}', f"{shown:,.2f}"])
        key("V2", l, raw_price_text=f"₹{shown:,.2f} (Rate/box)", raw_price=shown, raw_unit=unit,
            true_base_inr_pc=round(shown / 100 if n in AMBIG_PER100 else shown, 4),
            freight_basis="extra_quoted", true_freight_inr_pc=0.60, expected_flags=";".join(flags + ["MSME_TERMS_CAPPED"]),
            notes=note)
    t = Table(data, colWidths=[10*mm, 72*mm, 32*mm, 12*mm, 28*mm], repeatRows=1)
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "DV", 8), ("FONT", (0, 0), (-1, 0), "DVB", 8),
                           ("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2DCDB")),
                           ("ALIGN", (4, 1), (4, -1), "RIGHT")]))
    story += [t, Spacer(1, 8), Paragraph("<b>Terms:</b>", S()),
              Paragraph("Freight: ₹0.60 per box extra up to your Chakan plant. GST @ 18% extra.", S()),
              Paragraph("Payment: 90 days credit from date of invoice.", S()),
              Paragraph("Printing plates: ₹2,500 per colour, one time, for printed items.", S()),
              Paragraph("Delivery: 12 days from PO. Validity: 45 days from date of quotation.", S()),
              Spacer(1, 24), Paragraph("For Shree Ganesh Packaging Industries<br/>Authorised Signatory", S()),
              PageBreak(),
              Paragraph("Annexure - Response to Supplier Questionnaire", S(f="DVB", sz=11, ld=14)), Spacer(1, 6)]
    for q, a in [("1. ISO 9001", "Yes. Certificate attached."),
                 ("2. Bursting strength test report", "Yes, test report dated 05/10/2026 attached."),
                 ("3. Rejection & replacement", "Yes, subject to joint inspection at your end."),
                 ("4. Capacity", "180 tonnes per month."), ("5. First supply lead time", "12 days."),
                 ("6. Kraft paper source", "Recycled kraft (Indian mills)."), ("7. FSC", "No."),
                 ("8. Complaints (12 months)", "2 nos.")]:
        story += [Paragraph(f"<b>{q}:</b> {a}", S()), Spacer(1, 3)]
    def footnote(c, doc):
        c.saveState(); c.setFont("DV", 6); c.setFillColor(colors.grey)
        c.drawString(15*mm, 9*mm, "* Rates subject to 2% cash discount if payment is released within 15 days of invoice.")
        c.restoreState()
    SimpleDocTemplate(os.path.join(INBOX, "V2_ShreeGanesh_Quotation_SGPI-Q-1127.pdf"), pagesize=A4,
                      leftMargin=15*mm, rightMargin=15*mm, topMargin=15*mm, bottomMargin=15*mm).build(story, onFirstPage=footnote)

# =====================================================================================
# V3  Western India Cartons - Word doc, commercials in paragraphs, USD, 12% GST, RM-linked
# =====================================================================================
def build_v3():
    d = Document(); st = d.styles["Normal"]; st.font.name = "Calibri"; st.font.size = Pt(10.5)
    d.add_paragraph("WESTERN INDIA CARTONS LTD").runs[0].bold = True
    d.add_paragraph("Plot 1104, GIDC Phase III, Vapi 396195, Gujarat | GSTIN 24AAACW7731P1Z2 | tenders@wicartons.example")
    d.add_paragraph("Ref: WICL/MKT/2611/044\t\t\t\t\t\tDate: 13th November 2026")
    d.add_paragraph("To\nThe Head - Procurement\nSahyadri Consumer Products Ltd\nChakan MIDC, Pune")
    d.add_paragraph("Dear Sir/Madam,")
    d.add_paragraph(
        "Thank you for your enquiry for corrugated cartons for FY 2026-27. As one of the largest export-oriented carton "
        "manufacturers in Western India, we are pleased to submit our offer. Please note that, in line with our standard "
        "practice for multinational and export customers, all our prices are quoted in US Dollars (USD) per piece, ex-works "
        "Vapi. Prices for the 3-ply and 5-ply items are tabulated below.")
    tbl = d.add_table(rows=1, cols=4); tbl.style = "Light Grid Accent 1"
    for c, h in zip(tbl.rows[0].cells, ("Item", "Size (mm)", "Ply", "Price USD/pc")): c.text = h
    usd = {}
    for l in LINES:
        inr = base_inr(l, 42.0, 0.30)
        usd[l["line_no"]] = round(inr / FX, 4)
        if l["ply"] < 7:
            row = tbl.add_row().cells
            row[0].text, row[1].text, row[2].text, row[3].text = l["description"], size(l), str(l["ply"]), f'{usd[l["line_no"]]:.4f}'
    seven = [l for l in LINES if l["ply"] == 7]
    d.add_paragraph(
        "For the heavy-duty 7-ply export cartons, our rates are as follows: the 25 kg export detergent carton "
        f"(600x400x400) at USD {usd[27]:.4f} per piece, the export edible oil 2 x 15 L carton (620x330x400) at "
        f"USD {usd[28]:.4f}, the pallet-load spice drum outer (580x400x450) at USD {usd[29]:.4f}, and the 25 kg atta "
        f"export carton (560x400x420) at USD {usd[30]:.4f} per piece.")
    d.add_paragraph(
        "Freight up to your Chakan plant will be charged extra at INR 1.20 per box for 3-ply, INR 2.40 per box for 5-ply "
        "and INR 5.50 per box for 7-ply cartons. Our prices are exclusive of GST, which shall be charged at 12%. Payment "
        "terms are 30 days from the date of invoice. Please note that our prices are linked to the kraft paper index; "
        "any movement beyond ±5% in kraft paper prices from the date of this offer will be passed on at actuals. "
        "This offer is valid for 15 days.")
    d.add_paragraph(
        "Kindly note that for the noodles carton (420x320x250) our available capacity is limited to 8,000 pieces per month. "
        "Lead time for first supply is 18 days from PO.")
    d.add_paragraph(
        "With regard to your quality questionnaire: we are ISO 9001 certified (certificate attached) and FSC chain-of-custody "
        "certified (certificate attached). We confirm that all quoted grades meet the specified bursting strength, and a test "
        "report dated 30 August 2026 is enclosed. We accept rejection and free replacement of lots failing your incoming "
        "inspection. Our corrugation capacity available for your contract is about 400 tonnes per month. We use virgin kraft "
        "liners on all export-grade boards. We have had no quality complaints from FMCG customers in the last 12 months.")
    d.add_paragraph("Assuring you of our best services,\n\nFor Western India Cartons Ltd\nV. R. Mehta, GM - Marketing")
    d.save(os.path.join(INBOX, "V3_WesternIndiaCartons_Offer_WICL-2611-044.docx"))
    fr = {3: 1.20, 5: 2.40, 7: 5.50}
    for l in LINES:
        n = l["line_no"]; flags = ["FX_CONVERTED", "GST_RATE_MISMATCH", "PRICE_NOT_FIRM"]
        if n == 10: flags.append("PARTIAL_QTY")
        key("V3", l, raw_price_text=f'USD {usd[n]:.4f}/pc', raw_price=usd[n], raw_currency="USD",
            true_base_inr_pc=round(usd[n] * FX, 4), freight_basis="extra_quoted", true_freight_inr_pc=fr[l["ply"]],
            gst_rate_pct=12, expected_flags=";".join(flags),
            notes=("7-ply prices are in prose, not the table. " if l["ply"] == 7 else "") +
                  ("Capacity 8,000/month vs need 12,000/month" if n == 10 else ""))

# =====================================================================================
# V4  Om Sai Box Works - angled phone photo of a printed rate card; 27 of 30 lines; late
# =====================================================================================
V4_MISSING = {26, 28, 29}
SMUDGED = 14
def build_v4():
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), (247, 244, 236)); dr = ImageDraw.Draw(img)
    fb = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
    fm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 27)
    fs = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    dr.text((W // 2, 70), "OM SAI BOX WORKS", font=fb, fill=(20, 20, 90), anchor="mm")
    dr.text((W // 2, 120), "Ranjangaon MIDC, Pune  |  Mob 98XXXXXX21  |  Udyam: Micro", font=fs, fill=(40, 40, 40), anchor="mm")
    dr.text((W // 2, 165), "RATE LIST - CORRUGATED BOXES  (Nov 2026)", font=fs, fill=(0, 0, 0), anchor="mm")
    y = 220; dr.line((60, y - 10, W - 60, y - 10), fill=(0, 0, 0), width=2)
    dr.text((70, y), f'{"Sr":<4}{"Box Size (mm)":<20}{"Ply":<6}{"Rate Rs/pc":>12}', font=fm, fill=(0, 0, 0)); y += 44
    dr.line((60, y - 6, W - 60, y - 6), fill=(0, 0, 0), width=1)
    sr = 0; smudge_box = None
    for l in LINES:
        n = l["line_no"]
        if n in V4_MISSING:
            key("V4", l, quote_status="not_quoted", raw_unit="", raw_currency="", gst_rate_pct="",
                expected_flags="NOT_QUOTED", notes="Absent from rate card")
            continue
        sr += 1
        rate = round(base_inr(l, 40.0, 0.30) * 20) / 20       # printed rate cards round to 5 paise
        txt = f'{sr:<4}{size(l):<20}{l["ply"]:<6}{rate:>12.2f}'
        dr.text((70, y), txt, font=fm, fill=(15, 15, 15))
        if n == SMUDGED: smudge_box = (70 + 30 * 16.2, y - 4, 70 + 30 * 16.2 + 120, y + 34)
        flags = ["FREIGHT_IMPUTED"]
        if n == SMUDGED: flags.append("LOW_CONFIDENCE_OCR")
        key("V4", l, raw_price_text=f"{rate:.2f}", raw_price=rate, true_base_inr_pc=rate,
            freight_basis="extra_unquoted", expected_flags=";".join(flags),
            notes="Ex-works; no freight quoted. Matched by size + ply only (no descriptions)" +
                  ("; price digits smudged in photo" if n == SMUDGED else ""))
        y += 40
    y += 20; dr.line((60, y, W - 60, y), fill=(0, 0, 0), width=2); y += 20
    for t in ("Rates Ex-Works Ranjangaon.  GST 18% extra.", "Payment: 15 days.  Delivery: 7 days.",
              "Rates valid till 31 Dec 2026."):
        dr.text((70, y), t, font=fs, fill=(0, 0, 0)); y += 36
    # ink smudge over line 14's price
    if smudge_box:
        sm = Image.new("L", img.size, 0); sd = ImageDraw.Draw(sm)
        x0, y0, x1, y1 = smudge_box; sd.ellipse((x0 + 95, y0 - 2, x1 + 25, y1 + 4), fill=215)
        sm = sm.filter(ImageFilter.GaussianBlur(7))
        img = Image.composite(Image.new("RGB", img.size, (95, 85, 120)), img, sm)
    # photograph it: place on a desk, perspective warp, rotate, uneven light, blur, noise, JPEG
    desk = Image.new("RGB", (1600, 2100), (92, 70, 52))
    desk.paste(img, (180, 170))
    coeffs = _persp([(0, 0), (1600, 0), (1600, 2100), (0, 2100)],
                    [(70, 90), (1530, 40), (1560, 2050), (30, 2000)])
    ph = desk.transform((1600, 2100), Image.PERSPECTIVE, coeffs, Image.BICUBIC).rotate(-1.5, expand=False, fillcolor=(92, 70, 52))
    a = np.asarray(ph).astype(np.float32)
    gx = np.linspace(1.08, 0.72, a.shape[1])[None, :, None]; gy = np.linspace(1.0, 0.85, a.shape[0])[:, None, None]
    a = a * gx * gy + np.random.normal(0, 6, a.shape)
    ph = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.1)).resize((1200, 1575))
    ph.save(os.path.join(INBOX, "V4_OmSai_ratecard_photo.jpg"), quality=68)

def _persp(src, dst):
    m = []
    for (x, y), (u, v) in zip(dst, src):
        m.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); m.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    A = np.array(m, dtype=float); B = np.array(src, dtype=float).reshape(8)
    return np.linalg.solve(A, B).tolist()

# =====================================================================================
# V5  Krishna Kraft - terse email; per kg; "rest same as last year"; freight extra; expired ISO
# =====================================================================================
def build_v5():
    body = """From: Rajesh Patil <rajesh@krishnakraft.example>
To: purchase@sahyadriconsumer.example
Date: Mon, 09 Nov 2026 19:42:11 +0530
Subject: Re: RFQ - Corrugated boxes FY27 - Sahyadri
Attachments: KKP_ISO9001.pdf

Sir,

₹42/kg for the 5-ply, 38 for the 3-ply, rest same as last year, freight extra.
Payment terms as before.

ISO copy attached. Rejection ok. Capacity approx 180 T/month.

Regards
Rajesh Patil
Krishna Kraft Packaging LLP, Shirwal
(Udyam: Small)
Sent from my phone
"""
    with open(os.path.join(INBOX, "V5_KrishnaKraft_email.eml"), "w", encoding="utf-8") as f:
        f.write(body)
    for l in LINES:
        n, ply = l["line_no"], l["ply"]
        if ply in (3, 5):
            rk = 38 if ply == 3 else 42
            key("V5", l, raw_price_text=f"{rk}/kg ({ply}-ply)", raw_price=rk, raw_unit="per_kg",
                true_base_inr_pc=round(rk * l["theoretical_weight_kg"], 4), freight_basis="extra_unquoted",
                expected_flags="DERIVED_PER_KG;FREIGHT_IMPUTED",
                notes="₹/pc derived = rate/kg x theoretical weight; 3-ply price has no ₹ sign")
        else:
            ly = LY[(5, l["catalog_sku"])]
            flags = ["IMPLIED_BY_BLANKET", "FREIGHT_IMPUTED"]
            note = f"Resolved from V5 last-year price ₹{ly['price_inr_pc']}"
            if n in (28, 30):
                flags.append("LY_SPEC_CHANGED"); note += "; LY spec had lighter outer liners -> must confirm"
            key("V5", l, raw_price_text="same as last year", raw_price="", raw_unit="as_last_year",
                true_base_inr_pc=float(ly["price_inr_pc"]), freight_basis="extra_unquoted",
                expected_flags=";".join(flags), notes=note)

# =====================================================================================
# Attachments: certificates and test reports (PDF)
# =====================================================================================
def cert(fname, title, rows, note=""):
    S = lambda **k: ParagraphStyle("c", fontName=k.pop("f", "DV"), fontSize=k.pop("sz", 10), leading=k.pop("ld", 14), **k)
    story = [Paragraph(title, S(f="DVB", sz=18, ld=24, alignment=1)), Spacer(1, 16)]
    t = Table([[Paragraph(f"<b>{a}</b>", S()), Paragraph(b, S())] for a, b in rows], colWidths=[50*mm, 120*mm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [t, Spacer(1, 12)]
    if note: story.append(Paragraph(note, S(sz=8, textColor=colors.grey)))
    SimpleDocTemplate(os.path.join(INBOX, fname), pagesize=A4).build(story)

def build_docs():
    iso = lambda h, site, no, iss, exp, body: [("Certificate No.", no), ("Certified organisation", h), ("Site", site),
        ("Standard", "ISO 9001:2015"), ("Scope", "Design and manufacture of corrugated boxes and sheets"),
        ("Issue date", iss), ("Expiry date", exp), ("Certification body", body)]
    cert("V1_ISO9001_certificate.pdf", "CERTIFICATE OF REGISTRATION",
         iso("Deccan Corrugators Pvt Ltd", "Gat No. 212, Chakan, Pune", "QMS/IN/22814", "12 Aug 2024", "11 Aug 2027", "Bharat Quality Assurance Pvt Ltd (fictional)"))
    cert("V1_BS_test_report.pdf", "TEST REPORT - BURSTING STRENGTH",
         [("Report No.", "PPTL/26/09/4471"), ("Customer", "Deccan Corrugators Pvt Ltd"), ("Date of test", "20 Sep 2026"),
          ("Samples", "3-ply 150/120/150, 5-ply 180/120/150/120/180, 7-ply 200/150/150/150/150/150/200"),
          ("Result", "All samples meet or exceed specified BS (min 7 / 13 / 20 kg/cm²)"), ("Lab", "Pune Packaging Test Lab (fictional)")])
    cert("V2_ISO9001_certificate.pdf", "CERTIFICATE OF REGISTRATION",
         iso("Shree Ganesh Group Holdings Pvt Ltd", "Corporate Office, Nigdi, Pune", "QMS/IN/31907", "03 Mar 2025", "02 Mar 2028", "Bharat Quality Assurance Pvt Ltd (fictional)"),
         note="Holder is the group holding company, not the quoting entity (Shree Ganesh Packaging Industries, Bhosari).")
    cert("V2_BS_test_report.pdf", "TEST REPORT - BURSTING STRENGTH",
         [("Report No.", "PPTL/26/10/4610"), ("Customer", "Shree Ganesh Packaging Industries"), ("Date of test", "05 Oct 2026"),
          ("Samples", "3-ply and 5-ply standard grades"), ("Result", "Meets specified BS"), ("Lab", "Pune Packaging Test Lab (fictional)")])
    cert("V3_ISO9001_certificate.pdf", "CERTIFICATE OF REGISTRATION",
         iso("Western India Cartons Ltd", "Plot 1104, GIDC Phase III, Vapi", "QMS/IN/09115", "15 Jan 2025", "14 Jan 2028", "Global Cert Services India (fictional)"))
    cert("V3_FSC_CoC_certificate.pdf", "FSC CHAIN OF CUSTODY CERTIFICATE",
         [("Certificate code", "XX-COC-018832 (fictional)"), ("Holder", "Western India Cartons Ltd"), ("Site", "Vapi, Gujarat"),
          ("Product group", "Corrugated board and boxes"), ("Valid until", "30 Jun 2028")])
    cert("V3_BS_test_report.pdf", "TEST REPORT - BURSTING STRENGTH",
         [("Report No.", "GTL/26/08/2290"), ("Customer", "Western India Cartons Ltd"), ("Date of test", "30 Aug 2026"),
          ("Samples", "3, 5 and 7-ply export grades"), ("Result", "Meets specified BS"), ("Lab", "Gujarat Testing Labs (fictional)")])
    cert("V5_KKP_ISO9001.pdf", "CERTIFICATE OF REGISTRATION",
         iso("Krishna Kraft Packaging LLP", "MIDC Shirwal, Satara", "QMS/IN/17260", "01 Apr 2023", "31 Mar 2026", "Bharat Quality Assurance Pvt Ltd (fictional)"))

# =====================================================================================
build_v1(); build_v2(); build_v3(); build_v4(); build_v5(); build_docs()

manifest = [
    {"vendor_code": "V5", "received_at": "2026-11-09T19:42:11+05:30", "channel": "email", "from": "rajesh@krishnakraft.example",
     "files": ["V5_KrishnaKraft_email.eml", "V5_KKP_ISO9001.pdf"]},
    {"vendor_code": "V1", "received_at": "2026-11-10T16:05:00+05:30", "channel": "email", "from": "sales@deccancorrugators.example",
     "files": ["V1_Deccan_Price_Offer_SCPL.xlsx", "V1_ISO9001_certificate.pdf", "V1_BS_test_report.pdf"]},
    {"vendor_code": "V2", "received_at": "2026-11-12T11:30:00+05:30", "channel": "email", "from": "quotes@shreeganeshpack.example",
     "files": ["V2_ShreeGanesh_Quotation_SGPI-Q-1127.pdf", "V2_ISO9001_certificate.pdf", "V2_BS_test_report.pdf"]},
    {"vendor_code": "V3", "received_at": "2026-11-13T18:20:00+05:30", "channel": "email", "from": "tenders@wicartons.example",
     "files": ["V3_WesternIndiaCartons_Offer_WICL-2611-044.docx", "V3_ISO9001_certificate.pdf", "V3_FSC_CoC_certificate.pdf", "V3_BS_test_report.pdf"]},
    {"vendor_code": "V4", "received_at": "2026-11-17T09:10:00+05:30", "channel": "whatsapp_forward", "from": "omsaiboxworks@gmail.example",
     "files": ["V4_OmSai_ratecard_photo.jpg"], "note": "Received after due date (16 Nov). No questionnaire response."},
]
with open(os.path.join(INBOX, "manifest.json"), "w") as f: json.dump(manifest, f, indent=2)

with open(os.path.join(KEY, "quotes_answer_key.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(KEYROWS[0].keys())); w.writeheader(); w.writerows(KEYROWS)

QA = {  # expected rule results: pass / fail / unknown
    "V1": ["pass", "pass", "pass", "pass", "pass", "pass", "fail", "pass"],
    "V2": ["unknown (holder mismatch: group company)", "pass", "unknown (conditional: subject to joint inspection)", "pass", "pass", "fail", "fail", "pass"],
    "V3": ["pass", "pass", "pass", "pass", "fail (18 > 15 days)", "pass", "pass", "pass"],
    "V4": ["unknown (no response)"] * 8,
    "V5": ["fail (ISO expired 31 Mar 2026)", "unknown (no report)", "pass", "pass", "unknown", "unknown", "unknown", "unknown"],
}
QUAL = {"V1": "qualified", "V2": "pending (Q01 holder mismatch, Q03 conditional)", "V3": "qualified",
        "V4": "pending (no questionnaire response)", "V5": "disqualified (Q01 knock-out fail)"}
codes = ["Q01_ISO9001", "Q02_BS_TEST", "Q03_REJECTION", "Q04_CAPACITY", "Q05_FIRST_SUPPLY", "Q06_PAPER_SOURCE", "Q07_FSC", "Q08_COMPLAINTS"]
with open(os.path.join(KEY, "questionnaire_answer_key.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["vendor_code"] + codes + ["expected_qualification"])
    for v, res in QA.items(): w.writerow([v] + res + [QUAL[v]])
print(f"answer key rows: {len(KEYROWS)}")
