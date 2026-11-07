Independent check. Below is a vendor's reply to a corrugated-box RFQ. Another reader has already extracted it; you are the second pair of eyes. Read the reply afresh and report ONLY the commercial essentials, as they appear in the document.

For every priced item, report:
- line_key: the vendor's own row number or item code if the document has one, otherwise the box size exactly as printed (for example 600x400x400);
- price: the number printed (null if the vendor says "same as last year" or gives no number);
- unit: how the document states the price - per_piece, per_box, per_100, per_1000, per_kg, or as_last_year;
- currency: INR or USD.

Include prices that appear in running text as well as in tables. If the vendor prices many items with one statement (for example one rate for all 5-ply boxes, or "rest same as last year"), do not list every item: report the statement once under blanket_rules (scope_ply = 3, 5 or 7, or scope_rest = true for "the rest").

Also report the freight terms under freight_rules: one entry per distinct statement (scope_ply = the ply it applies to, or null for all boxes); basis = included (price already includes delivery to the buyer's plant), extra_quoted (charged extra with an amount, put the amount in INR per piece in value_inr_per_piece) or extra_unquoted (extra / ex-works / not mentioned, no amount).

Finally, answer the buyer's questionnaire items below from the reply: for each one the reply addresses, give q_code and a short normalised answer_value (yes / no / conditional for yes-no questions, a bare number for numeric ones, virgin / recycled / mixed for paper source). Omit any the reply does not address.

Do not convert, calculate or correct anything. If a digit is unclear, give your best reading.

{{QUESTIONS}}
