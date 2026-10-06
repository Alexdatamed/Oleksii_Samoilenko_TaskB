"""Reconcile extracted single-line invoices under tasks/invoices/domain.md.

Ambiguities: the domain supplies no receipt aggregation policy, so multiple
receipts remain unresolved. Receipts without supplier IDs can only support a PO
ID unique across suppliers. Missing invoice identities cannot be deduplicated.
Missing/non-integer amounts or a total inconsistent with quantity * unit price
remain unresolved; no field is silently corrected. No payment policy is inferred
for discrepant invoices (count_as_payable is None). Duplicate status takes
precedence, though available references and amounts are still returned.
Negative quantities and monetary inputs remain unresolved. Zero values are
accepted and checked normally: the domain does not prohibit them, so no minimum
quantity or price is invented. Negative differences remain valid discrepancies.
Conservative SKU interpretation: an invoice or receipt SKU differing from the
PO cannot substantiate its expected amount. Keep findings and references, but
leave expected/difference unset and exclude the case from overcharge totals.
"""
from copy import deepcopy


def _integer(value):
    return type(value) is int


def reconcile_invoice(invoice, purchase_orders, receipts, processed_identities=()):
    """Return a result without mutating inputs or the supplied identity collection.

    processed_identities contains (supplier_id, invoice_number) pairs.
    Findings are stable codes. All monetary outputs are integer USD cents.
    """
    supplier = invoice.get('supplier_id')
    po_id = invoice.get('po_id')
    number = invoice.get('invoice_number')
    duplicate = bool(
        supplier
        and number
        and (supplier, number) in processed_identities
    )
    findings = ['duplicate_identity'] if duplicate else []
    result = dict(file_id=invoice.get('file_id'), status='unresolved',
                  billed_cents=None, expected_cents=None, difference_cents=None,
                  count_as_payable=False, findings=findings,
                  references={'invoice': deepcopy(invoice), 'purchase_orders': [], 'receipts': []})

    def finish():
        if duplicate:
            result['status'] = 'duplicate'
            result['count_as_payable'] = False
        return result

    quantity, unit, total = (invoice.get(k) for k in ('quantity', 'unit_cents', 'total_cents'))
    if _integer(total):
        result['billed_cents'] = total
    if not supplier or not number:
        findings.append('missing_invoice_identity')
    if not supplier or not po_id:
        findings.append('missing_po_reference')
        return finish()
    candidates = [p for p in purchase_orders if p.get('po_id') == po_id]
    matches = [p for p in candidates if p.get('supplier_id') == supplier]
    result['references']['purchase_orders'] = deepcopy(matches or candidates)
    if len(matches) != 1:
        findings.append('ambiguous_po_reference' if len(matches) > 1 else
                        'supplier_reference_conflict' if candidates else 'unknown_po_reference')
        return finish()
    po = matches[0]
    matching_receipts = [r for r in receipts if r.get('po_id') == po_id and
                        ('supplier_id' not in r or r['supplier_id'] == supplier)]
    result['references']['receipts'] = deepcopy(matching_receipts)
    if len(matching_receipts) != 1:
        findings.append('ambiguous_receipt_reference' if matching_receipts else 'missing_receipt_reference')
        return finish()
    receipt = matching_receipts[0]
    if 'supplier_id' not in receipt and len({p.get('supplier_id') for p in candidates}) > 1:
        findings.append('ambiguous_receipt_supplier')
        return finish()
    if invoice.get('sku') and po.get('sku') and invoice['sku'] != po['sku']:
        findings.append('sku_mismatch')
    if receipt.get('sku') and po.get('sku') and receipt['sku'] != po['sku']:
        findings.append('receipt_sku_mismatch')
    if not all(_integer(v) for v in (quantity, unit, total, po.get('quantity'),
                                     po.get('unit_cents'), receipt.get('quantity'))):
        findings.append('missing_or_non_integer_amount')
        return finish()
    if any(v < 0 for v in (quantity, unit, total, po['quantity'],
                           po['unit_cents'], receipt['quantity'])):
        findings.append('negative_quantity_or_amount')
        return finish()
    if 'sku_mismatch' in findings or 'receipt_sku_mismatch' in findings:
        return finish()
    if not all(record.get('sku') for record in (invoice, po, receipt)):
        findings.append('missing_sku')
        return finish()
    if total != quantity * unit:
        findings.append('inconsistent_billed_total')
        return finish()
    if 'missing_invoice_identity' in findings:
        return finish()
    result['expected_cents'] = po['quantity'] * po['unit_cents']
    result['difference_cents'] = total - result['expected_cents']
    if quantity != po['quantity']:
        findings.append('ordered_quantity_mismatch')
    if quantity != receipt['quantity']:
        findings.append('received_quantity_mismatch')
    if unit != po['unit_cents']:
        findings.append('unit_price_mismatch')
    result['status'] = 'discrepant' if any(f != 'duplicate_identity' for f in findings) else 'reconciled'
    result['count_as_payable'] = None if result['status'] == 'discrepant' else True
    return finish()


def reconcile_invoices(invoices, purchase_orders, receipts, processed_identities=()):
    """Process in input order; every seen identity becomes previously processed."""
    seen = set(processed_identities)
    results = []
    for invoice in invoices:
        results.append(reconcile_invoice(invoice, purchase_orders, receipts, seen))
        if invoice.get('supplier_id') and invoice.get('invoice_number'):
            seen.add((invoice['supplier_id'], invoice['invoice_number']))
    return {'results': results, 'overcharge_cents': overcharge_total(results)}


def overcharge_total(results):
    """Sum positive differences only for resolved, nonduplicate invoices."""
    return sum(max(0, r['difference_cents']) for r in results
               if r['status'] in ('reconciled', 'discrepant')
               and _integer(r.get('difference_cents')))
