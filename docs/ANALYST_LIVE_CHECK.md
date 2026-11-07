# Analyst live check

Run against `rfx.db` with the real analyst on the configured Gemini model. Ground truth is computed in `tests/live_analyst_check.py` directly from the database, independent of the model.

## 1. What if we split it, cheapest per line, but only among vendors who cleared the quality questionnaire?

*35s, model `gemini-3.8-flash`, 5 analysis tool calls, 0 tables, 0 charts.*

**Tool calls:** `award_scenario`; `query_view`; `award_scenario`; `query_view`; `query_view`

**Answer**

If the RFQ is split line-by-line among only vendors who cleared the knock-out questionnaire, the order splits evenly between **V1** and **V3**:

### 1. Vendor Qualification Context
Only two of the five vendors have cleared the quality knock-out questionnaire (`qual_status = 'qualified'`):
* **V1 (Deccan Corrugators Pvt Ltd)**: Qualified (passed all 3 knock-outs).
* **V3 (Western India Cartons Ltd)**: Qualified (passed all 3 knock-outs).
* *Excluded*: **V2** and **V4** are `pending` (V2 has unresolved ISO 9001 parent-entity matching and conditional rejection responses; V4 provided no answers). **V5** is `disqualified` (ISO 9001 certificate expired).

---

### 2. Evaluated Basis (Standard Ranking)
Under the standard evaluated basis (`evaluated_inr_pc`, which includes landed price, payment-terms loading, and any one-time charges):

* **Total Evaluated Value:** **INR 4,90,84,617.36** (~₹4.91 Cr)
* **Total Landed Quoted Cost:** **INR 4,88,46,801.24** (~₹4.88 Cr)
* **Last Year Baseline Value:** **INR 4,89,18,168.00**
* **Net Evaluated Savings vs LY:** **INR 71,366.76** (+0.15%)

#### Allocation Split
| Vendor | Lines Won | Evaluated Total (INR) | Quoted Landed Total (INR) | Lines Awarded |
| :--- | :---: | :---: | :---: | :--- |
| **V1** | 15 | 2,22,99,912.00 | 2,22,99,912.00 | 1, 2, 4, 6, 7, 8, 10, 12, 14, 15, 17, 18, 20, 24, 28 |
| **V3** | 15 | 2,67,84,705.36 | 2,65,46,889.24 | 3, 5, 9, 11, 13, 16, 19, 21, 22, 23, 25, 26, 27, 29, 30 |

*(Note: On a pure quoted landed basis [`landed_inr_pc`], V3 would win 18 lines and V1 would win 12 lines, shifting lines 2, 17, and 18 to V3 for a total landed cost of INR 4,88,24,234.04).*

---

### 3. Critical Caveats & Operational Risks
1. **Price Firmness (Kraft Paper Linkage):** All 15 lines won by **V3** (worth INR 2,67,84,705.36 evaluated) have `price_not_firm = 1`. V3's prices are tied to kraft paper index fluctuations, meaning Sahyadri carries cost-variation risk on half the lines.
2. **Review Queue Uncertainty:** All 15 lines won by **V3** currently carry an open `spot_check` extraction review state.
3. **Lines Without a Valid Backup:**
   * **Line 10:** Won by V1. V3 quoted but is ineligible due to quoting a partial quantity (`partial_qty`).
   * **Line 16:** Won by V3. V1 is blocked pending review of `open_must_confirm:spec_match`.
   * **Line 25:** Won by V3. V1 quoted an evaluated price of INR 22.84/pc vs V3's INR 35.61/pc, but V1 is currently blocked by `open_must_confirm:price`. If buyer confirmation verifies V1's price, Line 25 would flip to V1.
4. **Freight Imputation:** 0 lines rely on imputed freight (both vendors provided freight terms).

**Checks against the database**

- PASS: answer contains scenario total = ₹49,084,617
- PASS: answer contains V1 value = ₹22,299,912
- PASS: answer contains V3 value = ₹26,784,705
- PASS: mentions “V1”
- PASS: mentions “V3”

---

## 2. Which values are still unconfirmed, and could any of them change the winner?

*50s, model `gemini-3.8-flash`, 7 analysis tool calls, 0 tables, 0 charts.*

**Tool calls:** `query_view`; `query_view`; `query_view`; `query_view`; `query_view`; `query_view`; `query_view` (error)

**Answer**

Yes, unconfirmed values **can directly change the winner on 2 lines** between currently qualified vendors (**Line 16** and **Line 25**), while unconfirmed fields on pending vendors could affect winners only if their knock-out qualification status changes.

---

### 1. Values that directly change the winner today (Qualified Vendors)

Both lines currently have quotes from **V1** blocked solely by open `must_confirm` review items. Confirming them would displace **V3** as the winner:

* **Line 16 (Vendor V1)**:
  * **Unconfirmed field**: `spec_match` (`must_confirm`, confidence 0.79).
  * **Current awardable winner**: **V3** at evaluated price of **INR 34.4258/pc** (landed: INR 34.1202/pc).
  * **V1 quote**: Evaluated price of **INR 33.47/pc** (landed: INR 33.47/pc), which is **2.78% lower** than V3.
  * **Impact**: V1 is currently excluded solely due to `open_must_confirm:spec_match`. If confirmed, **V1 wins Line 16**.

* **Line 25 (Vendor V1)**:
  * **Unconfirmed fields**: `price`, `unit`, and `spec_match` (`must_confirm`, confidence 0.79; quote snippet notes: *“DCPL-C117... 2284 | FOR Chakan | We suggest 3 ply heavy duty 200/150/200 - cost effective alternative”*).
  * **Current awardable winner**: **V3** at evaluated price of **INR 35.6069/pc** (landed: INR 35.2908/pc).
  * **V1 quote**: Evaluated price of **INR 22.84/pc** (landed: INR 22.84/pc), which is **35.86% lower** than V3.
  * **Impact**: V1 is currently excluded by `open_must_confirm:price`. If confirmed, **V1 wins Line 25**.

---

### 2. Current winning quotes with unconfirmed spot-checks (Qualified Vendor V3)

* **Vendor V3** has `price` and `gst_rate` flagged as `spot_check` across **16 lines** (Lines 3, 5, 9, 10, 11, 13, 16, 19, 21, 22, 23, 25, 26, 27, 29, and 30).
* Because V3 is the current awardable winner on these lines, validating these spot checks confirms the active winning evaluated benchmark.

---

### 3. Unconfirmed values on Pending & Disqualified Vendors

* **Vendor V2 (Pending qualification)**:
  * 19 lines have unconfirmed `payment_terms` (`spot_check`). While their evaluated prices are lower on these lines, **V2 cannot win any line** until its knock-out questionnaire requirements are cleared.
* **Vendor V4 (Pending qualification)**:
  * 22 lines have unconfirmed `must_confirm` fields (`currency`, `freight`, `gst_rate`, `lead_time`, `payment_terms`, `tax_basis`, `spec_match`, and `price`). V4 is cheaper on several lines, but **cannot win** because its qualification status is `pending`.
* **Vendor V5 (Disqualified)**:
  * Has 30 lines with `must_confirm` fields (`price`, `tax_basis`), but `could_change_winner = 0` on all lines because V5 is disqualified.

**Checks against the database**

- PASS: answer contains must-confirm values that could change the winner = ₹159
- PASS: mentions “V4”

---

## 3. Why didn't V5 win line 1? It's the cheapest.

*17s, model `gemini-3.8-flash`, 3 analysis tool calls, 0 tables, 0 charts.*

**Tool calls:** `query_view`; `query_view`; `query_view`

**Answer**

Even though **V5 (Krishna Kraft Packaging LLP)** quoted the lowest price on Line 1, it cannot win because **the vendor is disqualified (`qual_status: disqualified`)**.

### Details for Line 1:
- **V5 Price:** Evaluated price of **₹8.496/pc** (`evaluated_inr_pc`) and landed price of **₹8.496/pc** (`landed_inr_pc`), ranking 1st on price (`rank_evaluated: 1`, `rank_landed: 1`).
- **Award Winner:** **V1 (Deccan Corrugators Pvt Ltd)** won the line at an evaluated price of **₹8.87/pc** (`evaluated_inr_pc`) / landed price of **₹8.87/pc** (`landed_inr_pc`), as it is the cheapest eligible and qualified vendor (`rank_awardable: 1`).

### Why V5 is Disqualified:
According to the vendor qualification record (`v_vendor_qualification`):
1. **Q01_ISO9001 (Fail):** ISO 9001 certificate `QMS/IN/17260` expired on `2026-03-31`, prior to the award reference date `2026-11-16`.
2. **Q02_BS_TEST (Unknown):** No answer was received for the bursting strength test questionnaire item.
3. **Q03_REJECTION (Unknown):** Extraction confidence (90%) fell below the 95% knock-out threshold.

### Additional Line 1 Caveats for V5:
Even if qualification were resolved, V5's quote on Line 1 currently has unconfirmed items (`review_state: must_confirm`):
- Open confirmations required: `open_must_confirm:price` and `open_must_confirm:tax_basis`.
- Flags: The price is derived from per-kg rates (`DERIVED_PER_KG`), freight is imputed (`FREIGHT_IMPUTED` at ₹1.20/pc), and the tax basis is unspecified (`TAX_BASIS_UNSPECIFIED`).

**Checks against the database**

- PASS: answer contains V5 line-1 landed price = ₹8
- PASS: mentions “disqualified”
- PASS: mentions “ISO”

---

## 4. Show savings vs last year by vendor as a chart.

*59s, model `gemini-3.8-flash`, 7 analysis tool calls, 0 tables, 1 charts.*

**Tool calls:** `query_view`; `award_scenario`; `query_view`; `query_view`; `query_view`; `query_view`; `render_chart`; `query_view` (error)

**Answer**

Under the baseline award scenario (evaluated basis, qualified vendors only), 30 lines are split equally between **V1** (15 lines) and **V3** (15 lines). The landed savings vs last year (`saving_vs_ly_inr_pc * annual_qty`, ex-GST) by vendor are:

### Summary of Awarded Landed Savings vs Last Year

* **V3 (Western India Cartons Ltd)**: **+₹3,13,106.76** (+INR 3,13,106.76) savings
  * **Lines Awarded (15)**: 3, 5, 9, 11, 13, 16, 19, 21, 22, 23, 25, 26, 27, 29, 30
  * **Last Year Baseline Value**: INR 2,68,59,996.00
  * **Awarded Landed Spend (`landed_inr_pc`)**: INR 2,65,46,889.24
  * **Awarded Evaluated Spend (`evaluated_inr_pc`)**: INR 2,67,84,705.36 (Evaluated savings vs LY: +INR 75,290.64)

* **V1 (Deccan Corrugators Pvt Ltd)**: **-₹2,41,740.00** (-INR 2,41,740.00) cost increase
  * **Lines Awarded (15)**: 1, 2, 4, 6, 7, 8, 10, 12, 14, 15, 17, 18, 20, 24, 28
  * **Last Year Baseline Value**: INR 2,20,58,172.00
  * **Awarded Landed Spend (`landed_inr_pc`)**: INR 2,22,99,912.00
  * **Awarded Evaluated Spend (`evaluated_inr_pc`)**: INR 2,22,99,912.00 (Evaluated savings vs LY: -INR 2,41,740.00)

* **Net Portfolio Total**:
  * **Last Year Baseline Value**: INR 4,89,18,168.00
  * **Total Landed Spend**: INR 4,88,46,801.24
  * **Net Landed Savings**: **+INR 71,366.76** (+0.15% vs LY)
  * **Total Evaluated Spend**: INR 4,90,84,617.36

---

### Non-Awarded Vendors
The remaining vendors have ₹0 awarded savings because they are ineligible to win under current qualification rules:
* **V2 (Shree Ganesh Packaging Industries)**: `qual_status = pending` (0 lines awarded).
* **V4 (Om Sai Box Works)**: `qual_status = pending` (0 lines awarded).
* **V5 (Krishna Kraft Packaging LLP)**: `qual_status = disqualified` (0 lines awarded).

---

### Key Caveats & Uncertainties
1. **Firmness of Pricing**: All 15 lines awarded to **V3** (INR 2,67,84,705.36 evaluated value) have `price_not_firm = 1` (linked to raw kraft paper index) and are in `spot_check` review state. V3's realized savings may fluctuate with index changes.
2. **Firm Pricing on V1**: All 15 lines awarded to **V1** have firm pricing (`price_not_firm = 0`) and `review_state = auto_ok`.
3. **Freight**: No lines rely on imputed freight (`freight_imputed = 0`).

**Checks against the database**

- PASS: mentions “V4”
- PASS: mentions “V5”
- PASS: produced a chart (1)

---

## 5. What if we ordered 20,000 per release instead? Does anything change?

*39s, model `gemini-3.8-flash`, 6 analysis tool calls, 0 tables, 0 charts.*

**Tool calls:** `award_scenario`; `award_scenario`; `query_view`; `query_view`; `query_view`; `query_view`

**Answer**

**Yes, the allocation and total spend change.** 

Increasing the release size to 20,000 pieces unlocks **Vendor V1’s 4% per-order volume discount** on **Lines 1, 3, and 9**, which had typical order quantities below 20,000 pieces (10,000, 12,500, and 15,000 respectively).

This flips **2 lines (Lines 3 and 9) from V3 to V1**, cuts evaluated annual spend by **₹2,29,563.00**, and increases savings versus last year from **0.15% to 0.52%**.

---

### 1. Specific Line Impacts

*All prices are evaluated (ranking) prices per piece (`evaluated_inr_pc`, which equals `landed_inr_pc` for V1 since freight is included and terms match 60 days standard):*

* **Line 3 (Annual Qty: 1,50,000 pcs; Typical release: 12,500 pcs):**
  * **Baseline:** Winner was **V3** at evaluated **₹13.8185/pc** (landed ₹13.6958/pc; V1 backup at ₹13.93/pc).
  * **At 20,000/release:** **V1** wins at evaluated **₹13.3728/pc** (4% discount applied to ₹13.93/pc). V3 becomes backup at ₹13.8185/pc.
  * **Line Savings:** **₹66,855.00** annual reduction.

* **Line 9 (Annual Qty: 1,80,000 pcs; Typical release: 15,000 pcs):**
  * **Baseline:** Winner was **V3** at evaluated **₹17.6786/pc** (landed ₹17.5217/pc; V1 backup at ₹17.72/pc).
  * **At 20,000/release:** **V1** wins at evaluated **₹17.0112/pc** (4% discount applied to ₹17.72/pc). V3 becomes backup at ₹17.6786/pc.
  * **Line Savings:** **₹1,20,132.00** annual reduction.

* **Line 1 (Annual Qty: 1,20,000 pcs; Typical release: 10,000 pcs):**
  * **Baseline:** Winner was already **V1** at evaluated **₹8.8700/pc** (V3 backup at ₹9.5263/pc).
  * **At 20,000/release:** Winner remains **V1**, but price drops to **₹8.5152/pc** (4% discount applied).
  * **Line Savings:** **₹42,576.00** annual reduction.

---

### 2. Overall Scenario Comparison

| Metric | Baseline (Typical Order Qty) | Override (20,000 / release) | Net Difference |
| :--- | :--- | :--- | :--- |
| **Total Evaluated Value** | ₹4,90,84,617.36 | ₹4,88,55,054.36 | **-₹2,29,563.00** |
| **Total Quoted Landed Value** | ₹4,88,46,801.24 | ₹4,86,63,885.24 | **-₹1,82,916.00** |
| **Savings vs Last Year (LY: ₹4.89 Cr)** | ₹71,366.76 (0.15%) | ₹2,54,282.76 (0.52%) | **+₹1,82,916.00** |
| **V1 Allocation** | 15 lines (₹2,22,99,912.00) | **17 lines** (₹2,73,25,272.00) | +2 lines (+₹50.25 lakh) |
| **V3 Allocation** | 15 lines (₹2,67,84,705.36) | **13 lines** (₹2,15,29,782.36) | -2 lines (-₹52.55 lakh) |
| **Spend on Non-Firm Prices** | ₹2,67,84,705.36 (15 lines) | ₹2,15,29,782.36 (13 lines) | -₹52,54,923.00 |

---

### 3. Key Observations & Caveats

1. **Reduced Exposure to Kraft Paper Fluctuations:** V3’s pricing carries the `PRICE_NOT_FIRM` flag (linked to kraft paper indices) and is marked `spot_check`. Moving Lines 3 and 9 to V1 reduces non-firm spend by ₹52.55 lakh and locks them in at firm, verified (`auto_ok`) rates.
2. **Vendor Qualification:** Only **V1** and **V3** were eligible to win in both scenarios. Vendor **V2** and **V4** are currently `pending` qualification, and **V5** is `disqualified`.
3. **Operational Consideration:** Ordering in 20,000-pc releases requires verifying plant warehouse floor space and working capital capacity to hold larger delivery batches on Lines 1, 3, and 9.

**Checks against the database**

- PASS: answer contains total at 20,000/release = ₹48,855,054
- PASS: answer contains base total = ₹49,084,617
- PASS: mentions “V1”
- PASS: mentions “4%”

---

## 6. Exclude V3. What's the total and who's the backup per line?

*41s, model `gemini-3.8-flash`, 6 analysis tool calls, 0 tables, 0 charts.*

**Tool calls:** `award_scenario`; `query_view`; `query_view`; `query_view`; `award_scenario`; `query_view`

**Answer**

When excluding **V3 (Western India Cartons Ltd)** under standard qualification rules:

### 1. Award Summary & Total
* **Total Evaluated Value:** **₹4,56,26,244** (₹4.56 Cr)
* **Total Landed Value:** **₹4,56,26,244** (V1 quoted 60-day standard payment terms, so evaluated price equals quoted landed price).
* **Lines Awarded:** **28 lines**, all awarded to **V1 (Deccan Corrugators Pvt Ltd)**.
* **Savings vs Last Year:** **-₹6,91,296** (-1.54% vs LY baseline of ₹4,49,34,948 for those 28 lines).
* **Unawarded Lines (2 lines):** 
  * **Line 16:** No winner (`open_must_confirm:spec_match` on V1).
  * **Line 25:** No winner (`open_must_confirm:price` on V1).

---

### 2. Backup (L2) per Line
* **There are no eligible backup vendors on any line.**
* **Reason:** V1 and V3 are the only vendors with `qual_status = qualified`. With V3 excluded, V1 is the sole qualified vendor. The remaining vendors cannot serve as backup under standard award rules:
  * **V2 (Shree Ganesh Packaging Industries):** `pending` (knock-out issues on ISO 9001 certificate holder mismatch and conditional rejection terms).
  * **V4 (Om Sai Box Works):** `pending` (no responses received for knock-out questions Q01–Q03).
  * **V5 (Krishna Kraft Packaging LLP):** `disqualified` (ISO 9001 certificate expired on 2026-03-31 before award reference date).

---

### 3. What-if: If Pending Vendors (V2 & V4) Were Admitted
If the qualification requirement is relaxed to include pending vendors (`include_pending = true`):
* **Total Evaluated Value:** **₹4,86,06,276** (30 lines awarded: V2 wins 21 lines, V1 wins 9 lines).
* **Backup (L2) per line:**
  * **Lines where V1 is L2 (V2 wins):** Lines 2, 3, 4, 5, 9, 10, 13, 14, 15, 17, 18, 20, 22, 23, 26, 27, 28, 29, 30.
  * **Lines where V2 is L2 (V1 wins):** Lines 1, 7, 11, 19, 21, 24.
  * **Lines with no backup:** Lines 6, 8, 12 (V2's quoted rates are per-thousand units and rank significantly higher), Line 16 (V2 wins; V1 unconfirmed), and Line 25 (V2 wins; V1 unconfirmed).

**Checks against the database**

- PASS: answer contains total excluding V3 = ₹45,626,244
- PASS: mentions “backup”
- PASS: mentions “V1”

---
