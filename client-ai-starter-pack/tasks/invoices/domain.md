# Invoice reconciliation: exercise rules

These fictional rules are the source of truth for this assignment. No industry research is required.

1. A purchase order records the agreed SKU, quantity, and unit price. A receipt records the quantity delivered. An invoice records what the supplier billed.
2. Match using supplier ID and purchase-order ID. A missing or conflicting reference remains unresolved. The seeds include two similar purchase orders, so matching from amount alone is insufficient.
3. Expected line amount equals ordered quantity times agreed unit price. Compare billed quantity with both ordered and received quantity. Use integer USD cents; exclude tax, partial billing, foreign exchange, and payment terms.
4. Identify duplicates by supplier ID and invoice number. Billed amount minus expected amount is the discrepancy; a positive result is an overcharge in these fictional examples. Do not total duplicates or unresolved matches as recoverable amounts.

## Worked example

PO-2 agrees five cables at 2,000 cents each; the receipt confirms five. INV-2 bills five at 2,400 cents: billed 12,000, expected 10,000, difference 2,000 cents.

## Extend the starter

Use the two supplied invoice layouts and reference tables. Add two to four invoices, keeping the same rules. Include a correction of an extracted field and record what recalculation should produce.

Record any unresolved ambiguity in your README. Do not silently add domain rules. Keep seed cases and their expected results so the reviewer can run the same checks.
