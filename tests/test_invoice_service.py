from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.invoice_service import InvoiceService
from app.storage import InvoiceStore
from scripts.run_invoices import main

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / 'runtime/extraction/parser/summary.json'
REFERENCES = ROOT / 'client-ai-starter-pack/tasks/invoices/seed.json'


class InvoiceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store = InvoiceStore(self.folder / 'invoices.sqlite3')
        self.addCleanup(self.store.close)
        self.service = InvoiceService(self.store)
        self.original_summary = json.loads(SUMMARY.read_text())
        self.imported = self.service.import_results(SUMMARY, REFERENCES, dataset='original', mode='parser')
        self.ids = self.imported['invoice_ids']

    def import_custom(self, data, name='synthetic'):
        path = self.folder / (name + '.json')
        path.write_text(json.dumps(data), encoding='utf-8')
        return self.service.import_results(path, REFERENCES, dataset=name, mode=data['mode'])

    def snapshot(self):
        return self.service.list_invoices('original'), self.service.summary('original')

    def test_four_actual_results_expected_amount_and_supporting_evidence(self):
        expected = json.loads((REFERENCES.parent / 'expected-seed-results.json').read_text())
        actual = {row['file_id']: row for row in self.service.list_invoices('original')}
        for case in expected:
            with self.subTest(file=case['file_id']):
                result = actual[case['file_id']]['reconciliation']
                for key, value in case.items():
                    self.assertEqual(result[key], value)
        self.assertEqual(self.service.summary('original')['overcharge_cents'], 2000)
        self.assertEqual(self.service.summary('original')['counts_by_status'],
                         {'reconciled': 1, 'discrepant': 1, 'duplicate': 1, 'unresolved': 1})
        self.assertEqual([r['file_id'] for r in self.service.list_invoices('original')],
                         ['clean', 'duplicate', 'missing-reference', 'wrong-price'])
        self.assertEqual(len(self.service.list_invoices('original', 'unresolved')), 1)
        clean = actual['clean']
        self.assertNotEqual(clean['id'], clean['file_id'])
        self.assertIsNotNone(clean['original_evidence']['image']['sha256'])
        self.assertIn('INVOICE', clean['original_evidence']['ocr']['text'])
        self.assertEqual(clean['supporting_references']['purchase_orders'][0]['po_id'], 'PO-1')
        self.assertEqual(len(clean['reference_tables']['purchase_orders']), 2)

    def test_synthetic_extraction_error_correction_independent_calculation(self):
        data = deepcopy(self.original_summary)
        data['provenance'] = 'synthetic_extraction_error; never a real Gemini mistake'
        record = next(r for r in data['files'] if r['file_id'] == 'wrong-price')
        record['fields'].update(unit_price_cents=2600, total_cents=13000)
        record['issues'] = [{'field': 'unit_price_cents', 'code': 'invalid', 'message': 'Synthetic injected error', 'raw_values': ['synthetic']}]
        imported = self.import_custom(data)
        invoice_id = imported['invoice_ids']['wrong-price']
        before = self.service.get_invoice(invoice_id)
        # Independent arithmetic: agreed 5*2000=10000, synthetic billed 13000 -> 3000.
        self.assertEqual(before['reconciliation']['difference_cents'], 3000)
        after = self.service.correct_invoice(invoice_id, {'unit_price_cents': 2400, 'total_cents': 12000},
                                             'Remove independently labeled synthetic error')
        # Invoice text bills 5*2400=12000; difference from agreement is 2000.
        self.assertEqual(after['reconciliation']['difference_cents'], 2000)
        self.assertEqual(after['current_issues'], [])
        self.assertEqual(after['original_fields']['total_cents'], 13000)
        self.assertEqual(after['original_evidence'], before['original_evidence'])
        self.assertEqual(self.service.summary('synthetic')['overcharge_cents'], 2000)
        self.assertEqual(after['review_state'], 'corrected')
        self.assertEqual(len(after['correction_history'][0]['dataset_outcomes']), 4)

    def test_missing_po_null_issues_and_unrelated_fields_remain_visible(self):
        invoice_id = self.ids['missing-reference']
        original = self.service.get_invoice(invoice_id)
        edited = self.service.correct_invoice(invoice_id, {'quantity': 5}, 'Confirm quantity only')
        self.assertEqual(edited['current_issues'], original['current_issues'])
        self.assertIsNone(edited['current_fields']['po_id'])
        self.assertEqual(edited['reconciliation']['status'], 'unresolved')
        known = self.service.correct_invoice(invoice_id, {'po_id': 'PO-2'}, 'Explicit reviewer reference')
        self.assertEqual(known['current_issues'], [])
        cleared = self.service.correct_invoice(invoice_id, {'po_id': None}, 'Withdraw unverified reference')
        self.assertIsNone(cleared['current_fields']['po_id'])
        self.assertEqual(cleared['current_fields']['quantity'], 5)
        self.assertEqual(cleared['current_issues'][0]['code'], 'missing')
        self.assertEqual(cleared['original_issues'], original['original_issues'])
        self.assertEqual(cleared['reconciliation']['status'], 'unresolved')

    def test_issue_clearing_only_touches_explicit_known_fields(self):
        data = deepcopy(self.original_summary)
        record = data['files'][0]
        record['issues'] = [{'field': name, 'code': 'invalid', 'message': 'Synthetic issue', 'raw_values': []}
                            for name in ('quantity', 'unit_price_cents')]
        invoice_id = self.import_custom(data)['invoice_ids']['clean']
        edited = self.service.correct_invoice(invoice_id, {'quantity': 5}, 'Confirmed value')
        self.assertEqual([i['field'] for i in edited['current_issues']], ['unit_price_cents'])
        self.assertEqual(len(edited['original_issues']), 2)

    def test_invoice_identity_edit_changes_other_duplicate_and_total(self):
        # Initially wrong-price is recoverable +2000; duplicate clean is excluded.
        edited = self.service.correct_invoice(self.ids['clean'], {'invoice_number': 'INV-2'}, 'Synthetic identity change')
        self.assertEqual(edited['reconciliation']['status'], 'reconciled')
        self.assertEqual(self.service.get_invoice(self.ids['duplicate'])['reconciliation']['status'], 'reconciled')
        self.assertEqual(self.service.get_invoice(self.ids['wrong-price'])['reconciliation']['status'], 'duplicate')
        self.assertEqual(self.service.summary('original')['overcharge_cents'], 0)
        self.service.correct_invoice(self.ids['clean'], {'invoice_number': 'INV-1'}, 'Restore identity')
        self.assertEqual(self.service.get_invoice(self.ids['duplicate'])['reconciliation']['status'], 'duplicate')
        self.assertEqual(self.service.summary('original')['overcharge_cents'], 2000)

    def test_supplier_identity_edit_changes_other_duplicate_and_total(self):
        edited = self.service.correct_invoice(self.ids['clean'], {'supplier_id': 'S2'}, 'Synthetic supplier change')
        self.assertEqual(edited['reconciliation']['status'], 'unresolved')
        self.assertEqual(self.service.get_invoice(self.ids['duplicate'])['reconciliation']['status'], 'reconciled')
        self.assertEqual(self.service.summary('original')['overcharge_cents'], 2000)
        self.assertEqual(self.service.get_invoice(self.ids['wrong-price'])['review_state'], 'unreviewed')

    def test_reimport_is_idempotent_and_keeps_corrections_and_internal_ids(self):
        self.service.correct_invoice(self.ids['missing-reference'], {'po_id': 'PO-1'}, 'Reviewer supplied PO')
        before = self.snapshot()
        again = self.service.import_results(SUMMARY, REFERENCES, dataset='original', mode='parser')
        self.assertTrue(again['reimported'])
        self.assertEqual(again['invoice_ids'], self.ids)
        self.assertEqual(self.snapshot(), before)
        changed = deepcopy(self.original_summary)
        changed['files'][0]['fields']['quantity'] = 6
        path = self.folder / 'different.json'
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'different input'):
            self.service.import_results(path, REFERENCES, dataset='original', mode='parser')
        self.assertEqual(self.snapshot(), before)

    def test_alternative_run_is_isolated_not_business_duplicates(self):
        alternate = self.service.import_results(SUMMARY, REFERENCES, dataset='alternative', mode='parser')
        self.assertNotEqual(alternate['dataset_id'], self.imported['dataset_id'])
        self.assertEqual(self.service.summary('alternative')['overcharge_cents'], 2000)
        self.assertEqual(self.service.summary('alternative')['counts_by_status']['duplicate'], 1)
        self.assertNotEqual(alternate['invoice_ids']['clean'], self.ids['clean'])
        self.assertEqual(self.service.get_invoice(alternate['invoice_ids']['clean'])['reconciliation']['status'], 'reconciled')

    def test_processing_errors_never_become_business_statuses_or_duplicates(self):
        data = deepcopy(self.original_summary)
        data['files'][0].update(status='error', fields=None, issues=[], error='Synthetic processing failure')
        imported = self.import_custom(data)
        failed = self.service.get_invoice(imported['invoice_ids']['clean'])
        self.assertIsNone(failed['reconciliation'])
        self.assertIsNone(failed['current_fields'])
        self.assertEqual(failed['processing_status'], 'error')
        self.assertEqual(failed['original_evidence']['record']['error'], 'Synthetic processing failure')
        self.assertEqual(self.service.get_invoice(imported['invoice_ids']['duplicate'])['reconciliation']['status'], 'reconciled')
        summary = self.service.summary('synthetic')
        self.assertEqual(summary['processing_errors'], 1)
        self.assertEqual(sum(summary['counts_by_status'].values()), 3)
        self.assertEqual(summary['overcharge_cents'], 2000)
        with self.assertRaises(ValueError):
            self.service.correct_invoice(failed['id'], {'quantity': 5}, 'Cannot invent extraction')

    def test_invalid_corrections_are_atomic(self):
        invalid = [({'file_id': 'new'}, 'reason'), ({'id': 'new'}, 'reason'),
                   ({'extraction_mode': 'llm'}, 'reason'), ({'original_evidence': {}}, 'reason'),
                   ({'quantity': '5'}, 'reason'), ({'quantity': 2.5}, 'reason'),
                   ({'quantity': True}, 'reason'), ({'total_cents': 20.0}, 'reason'),
                   ({'sku': ''}, 'reason'), ({}, 'reason'), ({'quantity': 5}, '  ')]
        before = self.snapshot()
        for update, reason in invalid:
            with self.subTest(update=update, reason=reason), self.assertRaises(ValueError):
                self.service.correct_invoice(self.ids['clean'], update, reason)
            self.assertEqual(self.snapshot(), before)

    def test_zero_negative_and_inconsistent_totals_are_not_repaired(self):
        invoice_id = self.ids['wrong-price']
        changed = self.service.correct_invoice(invoice_id, {'unit_price_cents': 2000}, 'Change only unit price')
        self.assertEqual(changed['current_fields']['total_cents'], 12000)
        self.assertEqual(changed['reconciliation']['status'], 'unresolved')
        self.assertIn('inconsistent_billed_total', changed['findings'])
        for value in (0, -5):
            changed = self.service.correct_invoice(invoice_id, {'quantity': value}, 'Keep explicitly supplied value')
            self.assertEqual(changed['current_fields']['quantity'], value)
        self.assertIn('negative_quantity_or_amount', changed['findings'])
        self.assertEqual(self.service.summary('original')['overcharge_cents'], 0)

    def test_failure_after_partial_recalculation_rolls_back_all_outcomes_and_history(self):
        before = self.snapshot()
        saved = self.service._save_outcome
        count = 0
        def fail_on_second(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError('Synthetic recalculation persistence failure')
            saved(*args)
        with patch.object(self.service, '_save_outcome', side_effect=fail_on_second):
            with self.assertRaises(RuntimeError):
                self.service.correct_invoice(self.ids['clean'], {'invoice_number': 'INV-2'}, 'Synthetic rollback test')
        self.assertEqual(count, 2)
        self.assertEqual(self.snapshot(), before)
        with patch('app.invoice_service.reconcile_invoices', side_effect=RuntimeError('Synthetic calculation failure')):
            with self.assertRaises(RuntimeError):
                self.service.correct_invoice(self.ids['clean'], {'invoice_number': 'INV-2'}, 'Synthetic calculation rollback')
        self.assertEqual(self.snapshot(), before)

    def test_unavailable_artifacts_are_documented_not_fabricated(self):
        data = deepcopy(self.original_summary)
        data['files'][0]['source'] = 'absent-source.md'
        path = self.folder / 'missing-artifacts.json'
        path.write_text(json.dumps(data))
        result = self.service.import_results(path, REFERENCES, dataset='missing-artifacts', mode='parser',
                                             ocr_dir=self.folder/'no-ocr', image_dir=self.folder/'no-images')
        evidence = self.service.get_invoice(result['invoice_ids']['clean'])['original_evidence']
        self.assertIsNone(evidence['ocr']['text'])
        self.assertIsNone(evidence['image']['sha256'])
        self.assertIn('OCR text is unavailable', evidence['availability_notes'])
        self.assertEqual(result['summary']['overcharge_cents'], 2000)

    def test_credential_bearing_input_is_rejected_without_import(self):
        data = deepcopy(self.original_summary)
        data['files'][0]['metadata'] = {'api_key': 'synthetic-credential-never-real'}
        with self.assertRaisesRegex(ValueError, 'Credential'):
            self.import_custom(data)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM datasets').fetchone()[0], 1)

    def test_import_order_is_independent_of_summary_order(self):
        data = deepcopy(self.original_summary)
        data['files'].reverse()
        imported = self.import_custom(data)
        self.assertEqual(imported['summary']['counts_by_status'], self.service.summary('original')['counts_by_status'])
        self.assertEqual(imported['summary']['overcharge_cents'], 2000)

    def test_invalid_saved_schema_is_visible_as_processing_error(self):
        data = deepcopy(self.original_summary)
        data['files'][0]['fields']['quantity'] = '5'
        imported = self.import_custom(data)
        invoice = self.service.get_invoice(imported['invoice_ids']['clean'])
        self.assertEqual(invoice['original_fields']['quantity'], '5')
        self.assertIsNone(invoice['current_fields'])
        self.assertIsNone(invoice['reconciliation'])
        self.assertEqual(invoice['processing_status'], 'error')
        self.assertIn('strict schema', invoice['original_evidence']['import_problems'][0])
        self.assertEqual(imported['summary']['processing_errors'], 1)

    def test_late_import_error_rolls_back_new_dataset_and_rows(self):
        data = deepcopy(self.original_summary)
        data['files'][1]['fields']['file_id'] = 'conflicting-source'
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            self.import_custom(data)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM datasets').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM invoices').fetchone()[0], 4)

    def test_saved_capture_metadata_and_ocr_fallback_without_network(self):
        from hashlib import sha256
        data = deepcopy(self.original_summary)
        data['mode'] = 'llm'
        data['provenance'] = 'synthetic_test; this is not a real Gemini response'
        for record in data['files']:
            record['mode'] = 'llm'
        source = data['files'][0]
        source['source'] = 'absent-source.md'
        text = 'Synthetic captured OCR text, not real model output'
        ocr_hash = sha256(text.encode()).hexdigest()
        source['metadata'] = {'origin': 'synthetic_test', 'capture': 'synthetic.capture.json',
                              'ocr_sha256': ocr_hash, 'requested_model': 'gemini-synthetic-test'}
        captures = self.folder / 'captures'
        captures.mkdir()
        (captures / 'synthetic.capture.json').write_text(json.dumps({'ocr_text': text, 'ocr_sha256': ocr_hash}), encoding='utf-8')
        path = self.folder / 'llm-shaped.json'
        path.write_text(json.dumps(data))
        with patch('socket.socket.connect', side_effect=AssertionError('No network allowed')):
            result = self.service.import_results(path, REFERENCES, dataset='synthetic-llm', mode='llm',
                                                 ocr_dir=self.folder/'missing-ocr', capture_dir=captures)
        invoice = self.service.get_invoice(result['invoice_ids']['clean'])
        self.assertEqual(invoice['original_evidence']['record']['metadata'], source['metadata'])
        self.assertEqual(invoice['original_evidence']['ocr']['text'], text)
        self.assertTrue(invoice['original_evidence']['capture']['available'])
        self.assertEqual(invoice['extraction_mode'], 'llm')
        self.assertEqual(result['summary']['overcharge_cents'], 2000)

    def test_ocr_hash_mismatch_never_substitutes_unverified_text(self):
        data = deepcopy(self.original_summary)
        data['files'][0]['metadata'] = {'ocr_sha256': '0' * 64, 'capture': '../.env'}
        imported = self.import_custom(data)
        evidence = self.service.get_invoice(imported['invoice_ids']['clean'])['original_evidence']
        self.assertIsNone(evidence['ocr']['text'])
        self.assertFalse(evidence['capture']['available'])
        self.assertIn('OCR source hash mismatch; source text was not accepted', evidence['availability_notes'])
        self.assertIn('Referenced model capture is unavailable', evidence['availability_notes'])

    def test_cli_init_import_show_correct_summary_and_restart(self):
        path = self.folder / 'cli.sqlite3'
        def cli(*args):
            with patch('sys.stdout', new_callable=StringIO) as output:
                code = main(['--db', str(path), *args])
            self.assertEqual(code, 0)
            return json.loads(output.getvalue())
        self.assertTrue(cli('init')['initialized'])
        imported = cli('import', '--dataset', 'cli', '--mode', 'parser', '--summary', str(SUMMARY), '--references', str(REFERENCES))
        invoice_id = imported['invoice_ids']['wrong-price']
        self.assertEqual(cli('show', '--id', invoice_id)['reconciliation']['difference_cents'], 2000)
        patch_file = self.folder / 'patch.json'
        patch_file.write_text('{"unit_price_cents":2600,"total_cents":13000}')
        corrected = cli('correct', '--id', invoice_id, '--patch', str(patch_file), '--reason', 'Synthetic CLI test')
        self.assertEqual(corrected['reconciliation']['difference_cents'], 3000)
        self.assertEqual(cli('show', '--id', invoice_id), corrected)
        self.assertEqual(cli('summary', '--dataset', 'cli')['overcharge_cents'], 3000)
        self.assertEqual(len(cli('list', '--dataset', 'cli', '--status', 'duplicate')), 1)


if __name__ == '__main__':
    unittest.main()
