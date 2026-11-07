-- The six views from docs/SCHEMA.md §4. The analyst agent queries ONLY these.
DROP VIEW IF EXISTS v_review_queue;
DROP VIEW IF EXISTS v_comparison_grid;
DROP VIEW IF EXISTS v_response_summary;
DROP VIEW IF EXISTS v_documents;
DROP VIEW IF EXISTS v_ly_baseline;
DROP VIEW IF EXISTS v_vendor_qualification;

-- Tri-state. A missing or unknown knock-out answer = pending, never qualified.
CREATE VIEW v_vendor_qualification AS
WITH ko AS (
  SELECT question_id, q_code FROM rfx_questions WHERE is_knockout = 1
), res AS (
  SELECT v.vendor_id, v.vendor_code, v.name AS vendor_name, k.q_code,
         COALESCE(a.rule_result, 'unknown') AS rule_result,
         a.rule_explanation
  FROM vendors v
  CROSS JOIN ko k
  LEFT JOIN questionnaire_answers a
         ON a.vendor_id = v.vendor_id AND a.question_id = k.question_id AND a.is_current = 1
)
SELECT vendor_id, vendor_code, vendor_name,
       COUNT(*)                                   AS ko_total,
       SUM(rule_result = 'pass')                  AS ko_pass,
       SUM(rule_result = 'fail')                  AS ko_fail,
       SUM(rule_result NOT IN ('pass','fail'))    AS ko_unknown,
       CASE WHEN SUM(rule_result = 'fail') > 0 THEN 'disqualified'
            WHEN SUM(rule_result = 'pass') = COUNT(*) THEN 'qualified'
            ELSE 'pending' END                    AS qual_status,
       group_concat(CASE WHEN rule_result <> 'pass'
                         THEN q_code || ' (' || rule_result || '): ' || COALESCE(rule_explanation, 'no answer received')
                    END, '; ')                    AS ko_issues
FROM res
GROUP BY vendor_id, vendor_code, vendor_name;

-- Last-year landed price per line from the main last-year supplier (highest qty_supplied).
CREATE VIEW v_ly_baseline AS
WITH ranked AS (
  SELECT li.line_id, li.line_no, li.catalog_sku, l.vendor_id AS ly_vendor_id, l.period,
         l.price_inr_pc AS ly_price_inr_pc, l.freight_inr_pc AS ly_freight_inr_pc,
         l.qty_supplied AS ly_qty,
         ROW_NUMBER() OVER (PARTITION BY li.line_id ORDER BY l.qty_supplied DESC, l.lyp_id) AS rn
  FROM line_items li
  JOIN last_year_prices l ON l.catalog_sku = li.catalog_sku
)
SELECT r.line_id, r.line_no, r.catalog_sku, r.ly_vendor_id, v.vendor_code AS ly_vendor_code, r.period,
       r.ly_price_inr_pc, r.ly_freight_inr_pc,
       r.ly_price_inr_pc + COALESCE(r.ly_freight_inr_pc, 0) AS ly_landed_inr_pc,
       r.ly_qty
FROM ranked r JOIN vendors v ON v.vendor_id = r.ly_vendor_id
WHERE r.rn = 1;

CREATE VIEW v_documents AS
SELECT d.doc_id, d.rfq_id, d.vendor_id, v.vendor_code, d.doc_type, d.issuer, d.doc_number,
       d.issue_date, d.expiry_date, d.holder_name, d.holder_matches_vendor, d.scope_text,
       d.file_name, d.review_state, r.award_reference_date,
       CASE WHEN d.expiry_date IS NULL THEN 'unknown'
            WHEN d.expiry_date < r.award_reference_date THEN 'expired'
            WHEN d.expiry_date <= date(r.award_reference_date, '+90 days') THEN 'expiring_90d'
            ELSE 'valid' END AS validity_status
FROM documents d
JOIN rfqs r ON r.rfq_id = d.rfq_id
LEFT JOIN vendors v ON v.vendor_id = d.vendor_id;

CREATE VIEW v_response_summary AS
SELECT vr.response_id, vr.vendor_id, v.vendor_code, v.name AS vendor_name,
       vr.file_name, vr.source_type, vr.content_role, vr.received_at, vr.received_after_due,
       vr.version, vr.extraction_status, vr.extraction_model, vr.clarification_status,
       (SELECT COUNT(*) FROM line_items li WHERE li.rfq_id = vr.rfq_id) AS lines_total,
       COALESCE(SUM(q.quote_status = 'quoted'), 0)     AS lines_quoted,
       COALESCE(SUM(q.quote_status = 'not_quoted'), 0) AS lines_not_quoted,
       COALESCE(SUM(q.quote_status = 'unmatched'), 0)  AS lines_unmatched
FROM vendor_responses vr
JOIN vendors v ON v.vendor_id = vr.vendor_id
LEFT JOIN quotes q ON q.response_id = vr.response_id AND q.is_current = 1
GROUP BY vr.response_id;

-- One row per current (line, vendor). is_awardable = quote-level eligible AND vendor qualified.
CREATE VIEW v_comparison_grid AS
WITH base AS (
  SELECT li.line_id, li.line_no, li.catalog_sku, li.description, li.ply, li.annual_qty,
         li.typical_order_qty, li.awarded_quote_id,
         v.vendor_id, v.vendor_code, v.name AS vendor_name, v.msme_category,
         q.quote_id, q.quote_status, q.raw_price_text, q.raw_unit, q.raw_currency,
         q.spec_match, q.spec_accepted, q.spec_deviation_note,
         q.base_inr_pc, q.freight_inr_pc, q.freight_imputed,
         q.landed_inr_pc, q.onetime_inr_pc, q.payment_loading_inr_pc, q.evaluated_inr_pc,
         q.payment_terms_days, q.effective_vendor_days, q.discount_applied_pct,
         q.min_confidence, q.review_state, q.award_eligible, q.ineligible_reasons_json,
         q.normalization_trace_json, q.field_meta_json, q.validation_json,
         qa.qual_status,
         CASE WHEN q.award_eligible = 1 AND qa.qual_status = 'qualified' THEN 1 ELSE 0 END AS is_awardable
  FROM line_items li
  JOIN quotes q ON q.line_id = li.line_id AND q.is_current = 1
  JOIN vendors v ON v.vendor_id = q.vendor_id
  JOIN v_vendor_qualification qa ON qa.vendor_id = v.vendor_id
), ranked AS (
  SELECT b.*,
    CASE WHEN landed_inr_pc IS NULL THEN NULL
         ELSE RANK() OVER (PARTITION BY line_id, (landed_inr_pc IS NULL) ORDER BY landed_inr_pc) END AS rank_landed,
    CASE WHEN evaluated_inr_pc IS NULL THEN NULL
         ELSE RANK() OVER (PARTITION BY line_id, (evaluated_inr_pc IS NULL) ORDER BY evaluated_inr_pc) END AS rank_evaluated,
    CASE WHEN is_awardable = 1
         THEN RANK() OVER (PARTITION BY line_id, is_awardable ORDER BY evaluated_inr_pc) END AS rank_awardable
  FROM base b
)
SELECT r.*,
       r.evaluated_inr_pc * r.annual_qty AS annual_value_inr,
       lb.ly_landed_inr_pc,
       lb.ly_landed_inr_pc - r.landed_inr_pc AS saving_vs_ly_inr_pc,
       CASE WHEN lb.ly_landed_inr_pc > 0 AND r.landed_inr_pc IS NOT NULL
            THEN (lb.ly_landed_inr_pc - r.landed_inr_pc) / lb.ly_landed_inr_pc * 100 END AS saving_vs_ly_pct,
       CASE WHEN r.awarded_quote_id = r.quote_id THEN 1 ELSE 0 END AS is_awarded,
       CASE WHEN r.awarded_quote_id = r.quote_id AND r.is_awardable = 0 THEN 1 ELSE 0 END AS award_stale,
       CASE WHEN r.quote_status <> 'quoted'      THEN COALESCE(r.quote_status, 'not_quoted')
            WHEN r.qual_status = 'disqualified'  THEN 'vendor_disqualified'
            WHEN r.qual_status = 'pending'       THEN 'vendor_pending_qualification'
            WHEN r.award_eligible = 0            THEN COALESCE(json_extract(r.ineligible_reasons_json, '$[0]'), 'not_eligible')
       END AS exclusion_reason
FROM ranked r
LEFT JOIN v_ly_baseline lb ON lb.line_id = r.line_id;

-- field_meta_json flattened; shows which open items could change the winner.
CREATE VIEW v_review_queue AS
WITH best AS (
  SELECT line_id, MIN(evaluated_inr_pc) AS best_awardable_inr_pc
  FROM v_comparison_grid WHERE is_awardable = 1 GROUP BY line_id
)
SELECT g.quote_id, g.line_id, g.line_no, g.vendor_id, g.vendor_code, g.qual_status,
       j.key AS field_name,
       json_extract(j.value, '$.confidence')      AS confidence,
       json_extract(j.value, '$.status')          AS status,
       json_extract(j.value, '$.flags')           AS flags_json,
       json_extract(j.value, '$.source.ref')      AS source_ref,
       json_extract(j.value, '$.source.snippet')  AS source_snippet,
       g.evaluated_inr_pc, b.best_awardable_inr_pc,
       CASE WHEN b.best_awardable_inr_pc > 0 AND g.evaluated_inr_pc IS NOT NULL
            THEN (g.evaluated_inr_pc / b.best_awardable_inr_pc - 1) * 100 END AS pct_vs_best,
       CASE WHEN g.quote_status = 'quoted' AND g.qual_status <> 'disqualified'
                 AND g.evaluated_inr_pc IS NOT NULL
                 AND (b.best_awardable_inr_pc IS NULL OR g.evaluated_inr_pc <= b.best_awardable_inr_pc)
            THEN 1 ELSE 0 END AS could_change_winner
FROM v_comparison_grid g
CROSS JOIN json_each(g.field_meta_json, '$.fields') j
LEFT JOIN best b ON b.line_id = g.line_id
WHERE json_extract(j.value, '$.status') IN ('must_confirm', 'spot_check');
