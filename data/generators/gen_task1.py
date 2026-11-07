"""Synthetic data, Task 1: buyer-side seed data for the corrugated-box RFQ demo.
All names, GSTINs and numbers are fictional. Weights use the agreed industry formula."""
import csv, json, random, os, yaml

random.seed(42)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "seed")
os.makedirs(OUT, exist_ok=True)

# ---------------- config ----------------
CONFIG = {
    "fx": {"USD_INR": 95.17, "date": "2026-10-26", "source": "Market close (Pound Sterling Live); not SBI TT selling"},
    "cost_of_funds_pct": 10.9,
    "payment_loading_max_extra_days": 30,
    "msme_max_credit_days": 45,
    "weight_formula": {
        "flute_factors": {"A": 1.55, "B": 1.35, "C": 1.45, "E": 1.30},
        "sheet_margin_mm": {"normal": 60, "large": 120},
        "large_cutoff_L_plus_W_mm": 890,
        "sheet_width_allowance_mm": 30,
        "wastage_factor": 1.03,
    },
    "sanity_band_pct": 25,
    "weight_mismatch_pct": 5,
    "confidence_base": {"excel": 0.92, "word": 0.85, "pdf": 0.85, "email_text": 0.80, "photo": 0.65},
    "thresholds": {
        "core": {"auto": 0.95, "spot": 0.80, "fields": ["price", "unit", "currency", "freight", "tax_basis"]},
        "terms": {"auto": 0.85, "spot": 0.70, "fields": ["lead_time", "moq", "payment_terms"]},
        "knockout_answer_min": 0.95,
    },
}
FF = CONFIG["weight_formula"]

def weight(L, W, H, layers):
    large = (L + W) > FF["large_cutoff_L_plus_W_mm"]
    margin = FF["sheet_margin_mm"]["large" if large else "normal"]
    sl, sw = (L + W) * 2 + margin, W + H + FF["sheet_width_allowance_mm"]
    area = sl * sw / 1e6
    gsm = sum(l["gsm"] if l["role"] == "liner" else l["gsm"] * FF["flute_factors"][l["flute"]] for l in layers)
    wkg = area * gsm / 1000 * FF["wastage_factor"]
    trace = (f"Sheet = ({L}+{W})x2+{margin} = {sl} mm by {W}+{H}+30 = {sw} mm; area {area:.4f} m2; "
             f"total GSM {gsm:.1f}; weight = {area:.4f} x {gsm:.1f} / 1000 x 1.03 = {wkg:.3f} kg")
    return ("large" if large else "normal"), round(wkg, 3), trace

def layers_for(ply, g):
    """g = (outer liner, flute, inner liner) GSM base; BF fixed by role."""
    o, f, i = g
    if ply == 3:
        return [{"pos": 1, "role": "liner", "gsm": o, "bf": 20},
                {"pos": 2, "role": "flute", "flute": "B", "gsm": f, "bf": 16},
                {"pos": 3, "role": "liner", "gsm": i, "bf": 20}]
    if ply == 5:
        return [{"pos": 1, "role": "liner", "gsm": o, "bf": 22},
                {"pos": 2, "role": "flute", "flute": "B", "gsm": f, "bf": 16},
                {"pos": 3, "role": "liner", "gsm": i, "bf": 18},
                {"pos": 4, "role": "flute", "flute": "C", "gsm": f, "bf": 16},
                {"pos": 5, "role": "liner", "gsm": o, "bf": 22}]
    return [{"pos": 1, "role": "liner", "gsm": o, "bf": 24},
            {"pos": 2, "role": "flute", "flute": "B", "gsm": f, "bf": 18},
            {"pos": 3, "role": "liner", "gsm": i, "bf": 20},
            {"pos": 4, "role": "flute", "flute": "C", "gsm": f, "bf": 18},
            {"pos": 5, "role": "liner", "gsm": i, "bf": 20},
            {"pos": 6, "role": "flute", "flute": "B", "gsm": f, "bf": 18},
            {"pos": 7, "role": "liner", "gsm": o, "bf": 24}]

FLUTE = {3: "B", 5: "BC", 7: "BCB"}

# ---------------- line items (30) ----------------
# (product, code, ply, L, W, H, gsm(o,f,i), BS, colours, annual, order)
SPEC = [
    ("Shampoo 180ml bottle shipper (24s)",   "SH", 3, 300, 200, 150, (150,120,150),  7, 0, 120000, 10000),
    ("Shampoo sachet master (960s)",         "SH", 3, 380, 250, 200, (150,120,150),  8, 1,  90000,  7500),
    ("Bathing soap 75g x 144 carton",        "SP", 3, 360, 240, 180, (180,120,150),  9, 1, 150000, 12500),
    ("Bathing soap 125g x 72 carton",        "SP", 3, 340, 260, 160, (180,120,150),  9, 1,  96000,  8000),
    ("Toothpaste 150g x 72 shipper",         "TP", 3, 420, 280, 190, (150,120,150),  8, 2,  84000,  7000),
    ("Toothbrush retail display (inner)",    "TB", 3, 250, 180, 120, (120,100,120),  5, 4,  60000,  5000),
    ("Hair oil 100ml x 48 shipper",          "HO", 3, 320, 240, 170, (150,120,150),  8, 1,  72000,  6000),
    ("Face cream 50g x 96 shipper",          "FC", 3, 300, 220, 140, (150,120,150),  7, 2,  48000,  4000),
    ("Biscuit 75g x 60 carton",              "BS", 3, 400, 300, 220, (150,120,150),  8, 1, 180000, 15000),
    ("Noodles 70g x 96 carton",              "ND", 3, 420, 320, 250, (150,120,150),  8, 1, 144000, 12000),
    ("Tea 250g x 40 carton",                 "TE", 3, 360, 280, 230, (180,120,150),  9, 2,  66000,  5500),
    ("Spices 100g x 120 carton",             "SC", 3, 330, 250, 200, (150,120,150),  8, 1,  54000,  4500),
    ("Detergent powder 1kg x 20 master",     "DT", 5, 450, 300, 300, (180,120,150), 14, 2,  60000,  5000),
    ("Detergent powder 500g x 40 master",    "DT", 5, 430, 320, 280, (180,120,150), 13, 2,  72000,  6000),
    ("Liquid detergent 1L x 12 shipper",     "LD", 5, 400, 300, 270, (180,120,150), 14, 1,  48000,  4000),
    ("Dishwash bar 200g x 100 carton",       "DW", 5, 420, 300, 250, (180,120,150), 13, 1,  84000,  7000),
    ("Floor cleaner 1L x 12 shipper",        "FL", 5, 410, 310, 290, (180,120,150), 14, 1,  42000,  3500),
    ("Toilet cleaner 500ml x 24 shipper",    "TC", 5, 420, 280, 260, (180,120,150), 13, 1,  54000,  4500),
    ("Edible oil 1L pouch x 16 carton",      "EO", 5, 380, 280, 260, (200,120,180), 15, 1, 120000, 10000),
    ("Edible oil 1L bottle x 12 carton",     "EO", 5, 360, 270, 300, (200,120,180), 16, 1,  96000,  8000),
    ("Ghee 500ml jar x 24 carton",           "GH", 5, 400, 270, 220, (180,120,150), 14, 2,  36000,  3000),
    ("Atta 5kg x 4 carton",                  "AT", 5, 460, 330, 300, (200,120,180), 16, 1,  60000,  5000),
    ("Sugar 1kg x 20 carton",                "SG", 5, 440, 300, 280, (180,120,150), 14, 0,  48000,  4000),
    ("Hand wash 250ml x 24 shipper",         "HW", 5, 380, 260, 240, (180,120,150), 13, 2,  42000,  3500),
    ("Mixed SKU e-commerce shipper",         "EC", 5, 400, 300, 300, (180,120,150), 14, 0,  30000,  2500),
    ("Promo gift-pack outer",                "PG", 5, 450, 350, 250, (180,120,150), 13, 4,  12000,  1000),
    ("Export detergent 25kg bulk carton",    "XD", 7, 600, 400, 400, (200,150,150), 22, 1,  12000,  1000),
    ("Export edible oil 15L tin x 2 carton", "XO", 7, 620, 330, 400, (200,150,150), 21, 1,   9600,   800),
    ("Pallet-load spice drum outer",         "PD", 7, 580, 400, 450, (200,150,150), 22, 0,   6000,   500),
    ("Heavy-duty atta 25kg export carton",   "XA", 7, 560, 400, 420, (200,150,150), 20, 1,   7200,   600),
]

line_items = []
counter = {}
for n, (desc, code, ply, L, W, H, g, bs, col, aq, oq) in enumerate(SPEC, start=1):
    counter[code] = counter.get(code, 0) + 1
    sku = f"CB-{ply}P-{code}-{counter[code]:02d}"
    layers = layers_for(ply, g)
    size_class, wkg, trace = weight(L, W, H, layers)
    line_items.append({
        "line_id": n, "rfq_id": 1, "line_no": n, "catalog_sku": sku, "description": desc,
        "length_mm": L, "width_mm": W, "height_mm": H, "ply": ply, "flute_profile": FLUTE[ply],
        "layers_json": json.dumps(layers), "bursting_strength_kgcm2": bs,
        "print_colours": col, "print_notes": ("Plain" if col == 0 else f"{col}-colour flexo, brand artwork per spec"),
        "annual_qty": aq, "typical_order_qty": oq, "size_class": size_class,
        "theoretical_weight_kg": wkg, "weight_calc_trace": trace, "buyer_confirmed": 1,
    })

# ---------------- rfq ----------------
rfqs = [{
    "rfq_id": 1, "title": "Corrugated boxes - FY2026-27 annual rate contract", "category": "Corrugated packaging",
    "delivery_location": "Sahyadri Consumer Products Ltd, Plot B-14, Chakan MIDC Phase II, Pune 410501 (FOR destination)",
    "std_payment_terms_days": 60, "expected_gst_pct": 18, "baseline_period": "FY2025-26",
    "response_due_date": "2026-11-16", "award_reference_date": "2026-11-16",
    "rfq_body": "", "status": "issued", "issued_at": "2026-11-07T11:00:00+05:30",
}]

# ---------------- vendors (5) ----------------
vendors = [
    {"vendor_id": 1, "vendor_code": "V1", "name": "Deccan Corrugators Pvt Ltd", "gstin": "27AADCD4417K1Z9",
     "city": "Chakan, Pune", "state": "Maharashtra", "contact_email": "sales@deccancorrugators.example",
     "msme_category": "medium", "udyam_no": "UDYAM-MH-26-0114782",
     "known_aliases": json.dumps(["Deccan Corrugators", "DCPL", "Deccan Corr."])},
    {"vendor_id": 2, "vendor_code": "V2", "name": "Shree Ganesh Packaging Industries", "gstin": "27ABRFS2281M1ZQ",
     "city": "Bhosari MIDC, Pune", "state": "Maharashtra", "contact_email": "quotes@shreeganeshpack.example",
     "msme_category": "small", "udyam_no": "UDYAM-MH-26-0458831",
     "known_aliases": json.dumps(["Shree Ganesh Packaging", "SGPI", "Ganesh Packaging"])},
    {"vendor_id": 3, "vendor_code": "V3", "name": "Western India Cartons Ltd", "gstin": "24AAACW7731P1Z2",
     "city": "Vapi", "state": "Gujarat", "contact_email": "tenders@wicartons.example",
     "msme_category": "none", "udyam_no": "",
     "known_aliases": json.dumps(["WIC", "Western India Cartons", "WICL"])},
    {"vendor_id": 4, "vendor_code": "V4", "name": "Om Sai Box Works", "gstin": "27BQXPK6624H1ZT",
     "city": "Ranjangaon MIDC, Pune", "state": "Maharashtra", "contact_email": "omsaiboxworks@gmail.example",
     "msme_category": "micro", "udyam_no": "UDYAM-MH-26-0902216",
     "known_aliases": json.dumps(["Om Sai Box", "OSBW", "Sai Box Works"])},
    {"vendor_id": 5, "vendor_code": "V5", "name": "Krishna Kraft Packaging LLP", "gstin": "27AAOFK9045D1ZK",
     "city": "Shirwal, Satara", "state": "Maharashtra", "contact_email": "rajesh@krishnakraft.example",
     "msme_category": "small", "udyam_no": "UDYAM-MH-31-0067395",
     "known_aliases": json.dumps(["Krishna Kraft", "KKP", "Krishna Packaging"])},
]

# ---------------- questions (8) ----------------
Q = [
    ("Q01_ISO9001", "Do you hold a valid ISO 9001 certificate for the manufacturing site? Attach a copy.",
     "document", "doc_valid_on", {"doc_type": "ISO9001", "valid_on": "award_reference_date", "holder_must_match": True}, 1, "ISO9001"),
    ("Q02_BS_TEST", "Can you meet the specified bursting strength for every quoted line? Attach a recent (within 6 months) test report.",
     "bool", "equals", {"value": "yes"}, 1, "test_report"),
    ("Q03_REJECTION", "Do you accept rejection and free replacement of lots failing incoming inspection, at your cost?",
     "bool", "equals", {"value": "yes"}, 1, None),
    ("Q04_CAPACITY", "Monthly corrugation capacity available for this contract (tonnes/month)?",
     "number", "min", {"min": 150}, 0, None),
    ("Q05_FIRST_SUPPLY", "Lead time for first supply after PO and artwork approval (days)?",
     "number", "max", {"max": 15}, 0, None),
    ("Q06_PAPER_SOURCE", "Kraft paper source for liners: virgin, recycled, or mixed?",
     "text", "in_set", {"allowed": ["virgin", "mixed"]}, 0, None),
    ("Q07_FSC", "Do you hold FSC chain-of-custody certification? Attach if yes.",
     "document", "doc_valid_on", {"doc_type": "FSC", "valid_on": "award_reference_date"}, 0, "FSC"),
    ("Q08_COMPLAINTS", "Number of quality complaints/rejections from FMCG customers in the last 12 months?",
     "number", "max", {"max": 3}, 0, None),
]
rfx_questions = [{"question_id": i, "rfq_id": 1, "q_code": c, "question_text": t, "answer_type": at,
                  "rule_type": rt, "rule_params_json": json.dumps(rp), "is_knockout": ko,
                  "required_doc_type": rd or ""} for i, (c, t, at, rt, rp, ko, rd) in enumerate(Q, start=1)]

# ---------------- last year prices ----------------
# LY suppliers: V1 (lines 1-12 + 21-24), V3 (13-20, 27-30), V5 (all 30 - incumbent backup, needed for "same as last year").
def ly_price(li, rate_per_kg, print_adder):
    return round(li["theoretical_weight_kg"] * rate_per_kg + li["print_colours"] * print_adder, 2)

LY_SPEC_CHANGED_LINES = {28, 30}  # 7-ply lines covered by V5's "rest same as last year"; LY outer liner was lighter -> LY_SPEC_CHANGED
last_year = []
lid = 1
def add_ly(vendor_id, li, rate, adder, freight, terms, qty_share):
    global lid
    layers = json.loads(li["layers_json"])
    if li["line_no"] in LY_SPEC_CHANGED_LINES:
        layers = [dict(l, gsm=150) if (l["role"] == "liner" and l["pos"] in (1, len(layers))) else l for l in layers]
    snap = {"ply": li["ply"], "flute": li["flute_profile"], "layers": layers}
    w = weight(li["length_mm"], li["width_mm"], li["height_mm"], layers)[1]
    price = round(w * rate + li["print_colours"] * adder, 2)
    last_year.append({"lyp_id": lid, "vendor_id": vendor_id, "catalog_sku": li["catalog_sku"], "period": "FY2025-26",
                      "price_inr_pc": price, "freight_inr_pc": freight if freight is None else round(freight, 2),
                      "payment_terms_days": terms, "gst_rate_pct": 12,
                      "qty_supplied": int(li["annual_qty"] * qty_share), "spec_snapshot_json": json.dumps(snap)})
    lid += 1

for li in line_items:
    n = li["line_no"]
    if n <= 12 or 21 <= n <= 24:
        add_ly(1, li, random.uniform(44, 46), 0.35, None, 60, 0.7)               # V1: freight included
    if 13 <= n <= 20 or 27 <= n <= 30:
        add_ly(3, li, random.uniform(42, 44), 0.30, li["theoretical_weight_kg"] * 2.2, 60, 0.7)  # V3: Gujarat, freight extra
    add_ly(5, li, random.uniform(40, 43), 0.40, li["theoretical_weight_kg"] * 1.4, 45, 0.3)       # V5: all lines, smaller share

# ---------------- write ----------------
def write(name, rows):
    with open(os.path.join(OUT, name), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

write("rfqs.csv", rfqs)
write("vendors.csv", vendors)
write("line_items.csv", line_items)
write("rfx_questions.csv", rfx_questions)
write("last_year_prices.csv", last_year)
with open(os.path.join(OUT, "config.yaml"), "w") as f:
    yaml.safe_dump(CONFIG, f, sort_keys=False)

print(f"line_items={len(line_items)} questions={len(rfx_questions)} vendors={len(vendors)} last_year={len(last_year)}")
