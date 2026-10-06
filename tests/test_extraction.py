import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from app.extraction import extract_invoice_fields
from app.reconciliation import reconcile_invoice, reconcile_invoices
from app.schemas import InvoiceFields
from scripts.run_extraction import main, run_batch

ROOT = Path(__file__).resolve().parents[1]
LAYOUT_A = '''## FICTIONAL INVOICE - BILL-A7
Supplier: VENDOR-27
Purchase order: ORDER-89
Item / SKU: WIDGET-X
Quantity: 3
Unit price (USD): 12.34
Total (USD): 37.02
'''
LAYOUT_B = '''## FICTIONAL INVOICE BILL-A7
Supplier: VENDOR-27 Purchase order: ORDER-89
Item / SKU: WIDGET-X Quantity: 3
Unit price (USD): 12.34 Total (USD}: 37.02
'''
EXPECTED = dict(file_id='source', invoice_number='BILL-A7', supplier_id='VENDOR-27',
                po_id='ORDER-89', sku='WIDGET-X', quantity=3,
                unit_price_cents=1234, total_cents=3702)


class ExtractionTests(unittest.TestCase):
    def test_both_layouts_and_all_fields_on_one_line(self):
        for text in (LAYOUT_A, LAYOUT_B, LAYOUT_B.replace('\n', ' ')):
            with self.subTest(text=text):
                fields, issues = extract_invoice_fields(text, 'source')
                self.assertEqual(fields.model_dump(), EXPECTED)
                self.assertEqual(issues, [])

    def test_whitespace_line_breaks_and_heading_labels(self):
        text = '''# Invoice number:
        BILL-A7
        ## Supplier ID:   VENDOR-27
        Purchase   order:
        ORDER-89
        Item / SKU: WIDGET-X
        Quantity:\t3
        Unit price ( USD ): 12.34
        Total (USD}:
        37.02'''
        fields, issues = extract_invoice_fields(text, 'source')
        self.assertEqual(fields.model_dump(), EXPECTED)
        self.assertEqual(issues, [])

    def test_file_id_is_supplied_not_extracted(self):
        fields, _ = extract_invoice_fields(LAYOUT_A, 'caller-file')
        self.assertEqual(fields.file_id, 'caller-file')
        self.assertEqual(fields.invoice_number, 'BILL-A7')

    def test_missing_purchase_order_and_absent_label(self):
        for text in (LAYOUT_A.replace('ORDER-89', '(missing)'),
                     LAYOUT_A.replace('Purchase order: ORDER-89\n', '')):
            fields, issues = extract_invoice_fields(text, 'source')
            self.assertIsNone(fields.po_id)
            self.assertEqual([(i.field, i.code) for i in issues], [('po_id', 'missing')])
            self.assertEqual(fields.total_cents, 3702)

    def test_conflicting_repeated_values(self):
        for label, field, value in (('Supplier', 'supplier_id', 'OTHER'),
                                     ('Quantity', 'quantity', '4'),
                                     ('Total (USD)', 'total_cents', '99.00'),
                                     ('Invoice number', 'invoice_number', 'OTHER-INV')):
            fields, issues = extract_invoice_fields(LAYOUT_A + f'{label}: {value}', 'source')
            self.assertIsNone(getattr(fields, field))
            self.assertEqual([(i.field, i.code) for i in issues], [(field, 'conflicting')])

    def test_equal_repeated_values_and_invalid_occurrence(self):
        fields, issues = extract_invoice_fields(LAYOUT_A + 'Total (USD): 37.020', 'source')
        self.assertIsNone(fields.total_cents)
        self.assertEqual(issues[0].code, 'invalid')
        fields, issues = extract_invoice_fields(LAYOUT_A + 'Total (USD): 37.02 Quantity: +3', 'source')
        self.assertEqual(fields.model_dump(), EXPECTED)
        self.assertEqual(issues, [])

    def test_money_conversion_is_exact(self):
        for raw, expected in (('0.01', 1), ('20.10', 2010), ('2', 200),
                              ('2.5', 250), ('-0.01', -1),
                              ('123456789012345678901234567890.12', 12345678901234567890123456789012)):
            with self.subTest(raw=raw):
                text = LAYOUT_A.replace('12.34', raw)
                fields, issues = extract_invoice_fields(text, 'source')
                self.assertEqual(fields.unit_price_cents, expected)
                self.assertEqual(issues, [])

    def test_fractional_quantities_and_excess_money_precision(self):
        for old, new, field in (('Quantity: 3', 'Quantity: 3.5', 'quantity'),
                                ('12.34', '12.345', 'unit_price_cents'),
                                ('37.02', '37.020', 'total_cents')):
            fields, issues = extract_invoice_fields(LAYOUT_A.replace(old, new), 'source')
            self.assertIsNone(getattr(fields, field))
            self.assertEqual([(i.field, i.code) for i in issues], [(field, 'invalid')])

    def test_zero_and_negative_values_are_preserved(self):
        for quantity, unit, total, expected in (('0', '0.00', '0', (0, 0, 0)),
                                                ('-3', '-12.34', '-37.02', (-3, -1234, -3702))):
            text = LAYOUT_A.replace('Quantity: 3', f'Quantity: {quantity}')
            text = text.replace('12.34', unit).replace('37.02', total)
            fields, issues = extract_invoice_fields(text, 'source')
            self.assertEqual((fields.quantity, fields.unit_price_cents, fields.total_cents), expected)
            self.assertEqual(issues, [])

    def test_missing_or_inconsistent_total_is_not_recalculated(self):
        fields, issues = extract_invoice_fields(LAYOUT_A.replace('Total (USD): 37.02\n', ''), 'source')
        self.assertIsNone(fields.total_cents)
        self.assertEqual([(i.field, i.code) for i in issues], [('total_cents', 'missing')])
        fields, issues = extract_invoice_fields(LAYOUT_A.replace('37.02', '100.00'), 'source')
        self.assertEqual(fields.total_cents, 10000)
        self.assertEqual(issues, [])

    def test_invalid_identifiers_and_numbers(self):
        for old, new, field in (('VENDOR-27', 'vendor with spaces', 'supplier_id'),
                                ('Quantity: 3', 'Quantity: many', 'quantity'),
                                ('12.34', 'NaN', 'unit_price_cents'),
                                ('37.02', '1,000.00', 'total_cents')):
            fields, issues = extract_invoice_fields(LAYOUT_A.replace(old, new), 'source')
            self.assertIsNone(getattr(fields, field))
            self.assertIn((field, 'invalid'), [(i.field, i.code) for i in issues])

    def test_schema_strict_types_nulls_and_adapter(self):
        fields = InvoiceFields(file_id='source')
        self.assertIsNone(fields.unit_price_cents)
        self.assertIsNone(fields.to_reconciliation_dict()['unit_cents'])
        for kwargs in ({'quantity': 3.5}, {'quantity': True}, {'total_cents': '100'},
                       {'unit_price_cents': 1.0}, {'po_id': ''}, {'supplier_id': ' '}, {'status': 'reconciled'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                InvoiceFields(file_id='source', **kwargs)
        self.assertEqual(reconcile_invoice(fields.to_reconciliation_dict(), [], [])['status'], 'unresolved')

    def test_parsed_values_pass_to_reconciliation(self):
        po = dict(po_id='ORDER-89', supplier_id='VENDOR-27', sku='WIDGET-X', quantity=3, unit_cents=1234)
        receipt = dict(receipt_id='RECEIPT', po_id='ORDER-89', sku='WIDGET-X', quantity=3)
        fields, issues = extract_invoice_fields(LAYOUT_A, 'source')
        self.assertEqual(issues, [])
        result = reconcile_invoice(fields.to_reconciliation_dict(), [po], [receipt])
        self.assertEqual((result['status'], result['expected_cents'], result['difference_cents']),
                         ('reconciled', 3702, 0))
        fields, _ = extract_invoice_fields(LAYOUT_A.replace('12.34', '13.34').replace('37.02', '40.02'), 'source')
        result = reconcile_invoice(fields.to_reconciliation_dict(), [po], [receipt])
        self.assertEqual((result['status'], result['difference_cents']), ('discrepant', 300))

    def test_all_four_actual_ocr_files_independently_against_seed(self):
        # Reference fixtures are read only here, never by extraction code.
        folder = ROOT / 'client-ai-starter-pack/tasks/invoices'
        seed = json.loads((folder / 'seed.json').read_text(encoding='utf-8'))
        expected_results = json.loads((folder / 'expected-seed-results.json').read_text(encoding='utf-8'))
        self.assertEqual({p.stem for p in (ROOT / 'runtime/ocr').glob('*.md')},
                         {'clean', 'wrong-price', 'duplicate', 'missing-reference'})
        extracted = []
        for original in seed['invoices']:
            with self.subTest(file=original['file_id']):
                source = ROOT / 'runtime/ocr' / (original['file_id'] + '.md')
                self.assertTrue(source.is_file(), f'Actual OCR file unavailable: {source}')
                fields, issues = extract_invoice_fields(source.read_text(encoding='utf-8'), source.stem)
                expected_fields = {name: original[name] for name in
                                   ('file_id', 'invoice_number', 'supplier_id', 'po_id', 'sku', 'quantity', 'total_cents')}
                expected_fields['unit_price_cents'] = original['unit_cents']
                self.assertEqual(fields.model_dump(), expected_fields)
                self.assertEqual([(i.field, i.code) for i in issues],
                                 [('po_id', 'missing')] if source.stem == 'missing-reference' else [])
                extracted.append(fields.to_reconciliation_dict())
        batch = reconcile_invoices(extracted, seed['purchase_orders'], seed['receipts'])
        for actual, expected in zip(batch['results'], expected_results):
            for key, value in expected.items():
                self.assertEqual(actual[key], value)
        self.assertEqual(batch['overcharge_cents'], 2000)

    def test_cli_continues_and_returns_nonzero_on_issues_or_processing_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs, outputs = root / 'inputs', root / 'outputs'
            inputs.mkdir()
            (inputs / 'a.md').write_bytes(b'\xff')
            (inputs / 'b.md').write_text(LAYOUT_A.replace('ORDER-89', '(missing)'), encoding='utf-8')
            (inputs / 'c.md').write_text(LAYOUT_A, encoding='utf-8')
            before = {p.name: p.read_bytes() for p in inputs.iterdir()}
            self.assertEqual(main(['--input-dir', str(inputs), '--output-dir', str(outputs)]), 1)
            summary = json.loads((outputs / 'summary.json').read_text())
            self.assertEqual(summary['mode'], 'parser')
            self.assertEqual([r['status'] for r in summary['files']], ['error', 'issues', 'success'])
            self.assertIn('UnicodeDecodeError', summary['files'][0]['error'])
            self.assertEqual(summary['files'][2]['fields']['file_id'], 'c')
            self.assertTrue((outputs / 'c.md.json').is_file())
            self.assertEqual(before, {p.name: p.read_bytes() for p in inputs.iterdir()})

    def test_cli_success_empty_inputs_and_output_overlap(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / 'inputs'
            inputs.mkdir()
            with self.assertRaisesRegex(ValueError, 'No Markdown'):
                run_batch(inputs, root / 'outputs')
            (inputs / 'valid.md').write_text(LAYOUT_A, encoding='utf-8')
            self.assertEqual(main(['--input-dir', str(inputs), '--output-dir', str(root / 'outputs')]), 0)
            with self.assertRaisesRegex(ValueError, 'separate'):
                run_batch(inputs, inputs)
            with self.assertRaisesRegex(ValueError, 'separate'):
                run_batch(inputs, inputs / 'extraction')


if __name__ == '__main__':
    unittest.main()
