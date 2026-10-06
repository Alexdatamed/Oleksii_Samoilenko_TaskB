import json
from copy import deepcopy
from pathlib import Path
import unittest

from app.reconciliation import reconcile_invoice, reconcile_invoices, overcharge_total


class ReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = Path(__file__).resolve().parents[1] / 'client-ai-starter-pack/tasks/invoices'
        cls.seed = json.loads((folder / 'seed.json').read_text(encoding='utf-8'))
        cls.expected = json.loads((folder / 'expected-seed-results.json').read_text(encoding='utf-8'))

    def reconcile(self, invoice, pos=None, receipts=None, seen=()):
        return reconcile_invoice(invoice, self.seed['purchase_orders'] if pos is None else pos,
                                 self.seed['receipts'] if receipts is None else receipts, seen)

    def test_supplied_cases(self):
        batch = reconcile_invoices(self.seed['invoices'], self.seed['purchase_orders'], self.seed['receipts'])
        self.assertEqual([r['file_id'] for r in batch['results']],
                         ['clean', 'wrong-price', 'duplicate', 'missing-reference'])
        for actual, expected in zip(batch['results'], self.expected):
            with self.subTest(case=expected['file_id']):
                for key, value in expected.items():
                    self.assertEqual(actual[key], value)
        self.assertEqual(batch['overcharge_cents'], 2000)
        self.assertEqual(batch['results'][0]['references']['purchase_orders'][0], self.seed['purchase_orders'][0])
        self.assertEqual(batch['results'][1]['findings'], ['unit_price_mismatch'])

    def test_quantity_mismatch(self):
        invoice = dict(self.seed['invoices'][0], quantity=6, total_cents=12000)
        result = self.reconcile(invoice)
        self.assertEqual(result['status'], 'discrepant')
        self.assertEqual(result['difference_cents'], 2000)
        self.assertEqual(result['findings'], ['ordered_quantity_mismatch', 'received_quantity_mismatch'])

    def test_received_quantity_mismatch(self):
        receipts = deepcopy(self.seed['receipts'])
        receipts[0]['quantity'] = 4
        result = self.reconcile(self.seed['invoices'][0], receipts=receipts)
        self.assertEqual(result['findings'], ['received_quantity_mismatch'])
        self.assertEqual(result['difference_cents'], 0)
        self.assertEqual(result['status'], 'discrepant')

    def test_supplier_reference_conflict(self):
        result = self.reconcile(dict(self.seed['invoices'][0], supplier_id='S2'))
        self.assertEqual(result['status'], 'unresolved')
        self.assertIn('supplier_reference_conflict', result['findings'])
        self.assertIsNone(result['difference_cents'])

    def test_duplicate_and_unresolved_excluded_from_total(self):
        invoice = self.seed['invoices'][1]
        duplicate = self.reconcile(invoice, seen={('S1', 'INV-2')})
        unresolved = self.reconcile(dict(invoice, po_id=None))
        self.assertEqual(duplicate['difference_cents'], 2000)
        self.assertEqual(duplicate['status'], 'duplicate')
        self.assertEqual(overcharge_total([duplicate, unresolved]), 0)
        self.assertEqual(overcharge_total([self.reconcile(invoice), duplicate, unresolved]), 2000)

    def test_ambiguous_po_and_receipt(self):
        invoice = self.seed['invoices'][0]
        for result in (self.reconcile(invoice, pos=self.seed['purchase_orders'] * 2),
                       self.reconcile(invoice, receipts=self.seed['receipts'] * 2)):
            self.assertEqual(result['status'], 'unresolved')
            self.assertIsNone(result['expected_cents'])

    def test_sku_mismatch(self):
        result = self.reconcile(dict(self.seed['invoices'][0], sku='OTHER'))
        self.assertEqual(result['status'], 'unresolved')
        self.assertIn('sku_mismatch', result['findings'])

    def test_sku_mismatch_overcharge_is_unresolved(self):
        for source, finding in (('invoice', 'sku_mismatch'),
                                ('receipt', 'receipt_sku_mismatch')):
            with self.subTest(source=source):
                invoice = dict(self.seed['invoices'][0], unit_cents=2400, total_cents=12000)
                receipts = deepcopy(self.seed['receipts'])
                if source == 'invoice':
                    invoice['sku'] = 'OTHER'
                else:
                    receipts[0]['sku'] = 'OTHER'
                result = self.reconcile(invoice, receipts=receipts)
                self.assertEqual(result['status'], 'unresolved')
                self.assertIn(finding, result['findings'])
                self.assertEqual(result['billed_cents'], 12000)
                self.assertIsNone(result['expected_cents'])
                self.assertIsNone(result['difference_cents'])
                self.assertEqual(result['references']['invoice'], invoice)
                self.assertEqual(result['references']['purchase_orders'], [self.seed['purchase_orders'][0]])
                self.assertEqual(result['references']['receipts'], [receipts[0]])
                self.assertEqual(overcharge_total([result]), 0)

    def test_negative_quantities_and_monetary_inputs(self):
        for source, field in (('invoice', 'quantity'), ('invoice', 'unit_cents'),
                              ('invoice', 'total_cents'), ('po', 'quantity'),
                              ('po', 'unit_cents'), ('receipt', 'quantity')):
            with self.subTest(source=source, field=field):
                invoice = deepcopy(self.seed['invoices'][0])
                pos = deepcopy(self.seed['purchase_orders'])
                receipts = deepcopy(self.seed['receipts'])
                record = {'invoice': invoice, 'po': pos[0], 'receipt': receipts[0]}[source]
                record[field] = -1
                result = self.reconcile(invoice, pos=pos, receipts=receipts)
                self.assertEqual(result['status'], 'unresolved')
                self.assertIn('negative_quantity_or_amount', result['findings'])
                self.assertIsNone(result['expected_cents'])
                self.assertIsNone(result['difference_cents'])
                self.assertEqual(overcharge_total([result]), 0)

    def test_zero_values_are_checked_normally(self):
        for field in ('quantity', 'unit_cents'):
            with self.subTest(field=field):
                invoice = dict(self.seed['invoices'][0], total_cents=0)
                pos = deepcopy(self.seed['purchase_orders'])
                receipts = deepcopy(self.seed['receipts'])
                invoice[field] = pos[0][field] = 0
                if field == 'quantity':
                    receipts[0]['quantity'] = 0
                result = self.reconcile(invoice, pos=pos, receipts=receipts)
                self.assertEqual(result['status'], 'reconciled')
                self.assertEqual(result['expected_cents'], 0)
                self.assertEqual(result['difference_cents'], 0)
                self.assertEqual(result['findings'], [])

    def test_integer_cents_and_inconsistent_total(self):
        for changes in ({'unit_cents': 2000.0}, {'total_cents': 9999}):
            result = self.reconcile(dict(self.seed['invoices'][0], **changes))
            self.assertEqual(result['status'], 'unresolved')
        result = self.reconcile(dict(self.seed['invoices'][0], unit_cents=1999, total_cents=9995))
        self.assertEqual(result['difference_cents'], -5)
        self.assertEqual(overcharge_total([result]), 0)

    def test_identity_uses_supplier_and_inputs_are_preserved(self):
        before = deepcopy(self.seed)
        seen = {('S2', 'INV-1')}
        self.assertEqual(self.reconcile(self.seed['invoices'][0], seen=seen)['status'], 'reconciled')
        self.assertEqual(seen, {('S2', 'INV-1')})
        self.assertEqual(self.seed, before)


if __name__ == '__main__':
    unittest.main()
