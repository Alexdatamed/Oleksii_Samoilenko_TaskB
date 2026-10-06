"""Offline application tests. All generated invoice/image/model fixtures are synthetic."""
from dataclasses import replace
from io import BytesIO
from functools import partial
import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, Mock

from fastapi.testclient import TestClient
from PIL import Image
from app.api import create_app
from app.application_service import ApplicationService, Settings, ApplicationError, BATCH_LOCK
from app.llm_extraction import (ExtractionResult, ExtractionError, FIELD_NAMES,
                                GeminiExtractor, ReplayExtractor, RequestConfiguration)
from app.ocr import ExtractedDocument
from app.schemas import InvoiceFields
from app.storage import InvoiceStore
from app.ui import ReviewController, format_usd, money_text

TEXT = ('Invoice: TEST-1 Supplier: SUP-A Purchase order: PO-X SKU: ITEM-X '
        'Quantity: 2 Unit price (USD): 24.00 Total (USD}: 48.00')
FIELDS = dict(invoice_number='TEST-1', supplier_id='SUP-A', po_id='PO-X', sku='ITEM-X',
              quantity=2, unit_price_cents=2400, total_cents=4800)


def png(color='white', format='PNG'):
    stream = BytesIO()
    Image.new('RGB', (20, 20), color).save(stream, format=format)
    return stream.getvalue()


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.references = self.folder / 'references.json'
        self.references.write_text(json.dumps({
            'purchase_orders': [{'supplier_id': 'SUP-A', 'po_id': 'PO-X', 'sku': 'ITEM-X', 'quantity': 2, 'unit_cents': 1000}],
            'receipts': [{'supplier_id': 'SUP-A', 'po_id': 'PO-X', 'sku': 'ITEM-X', 'quantity': 2}]}))
        self.settings = Settings(db_path=self.folder / 'invoices.sqlite3', runtime_dir=self.folder / 'review',
                                 references_path=self.references, image_dir=self.folder / 'images',
                                 capture_dir=self.folder / 'captures')
        self.service = ApplicationService(self.settings)
        self.client = TestClient(create_app(self.settings, mount_ui=False))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def imported(self, name='synthetic-parser', uploads=None):
        with patch('app.application_service.extract_document', return_value=ExtractedDocument(TEXT, {'synthetic': True})):
            return self.service.batch(name, 'parser', uploads or [('b.png', png()), ('a.png', png())])

    def test_factory_health_startup_no_processing_or_database_mutation(self):
        with patch('app.application_service.extract_document') as ocr, patch('app.application_service.GeminiExtractor') as gemini:
            app = create_app(self.settings)
            self.assertFalse(self.settings.db_path.exists())
            with TestClient(app) as client:
                self.assertEqual(client.get('/health').json(), {'status': 'ok'})
                self.assertEqual(client.get('/docs').status_code, 200)
                self.assertEqual(client.get('/ui/').status_code, 200)
            self.assertFalse(self.settings.db_path.exists())
            ocr.assert_not_called()
            gemini.assert_not_called()

    def test_datasets_queue_filter_summary_detail_and_image(self):
        imported = self.imported()
        self.assertEqual([r['original_filename'] for r in imported['files']], ['a.png', 'b.png'])
        self.assertEqual(self.client.get('/api/datasets').json()[0]['name'], 'synthetic-parser')
        rows = self.client.get('/api/invoices', params={'dataset': 'synthetic-parser'}).json()
        self.assertEqual([r['reconciliation']['status'] for r in rows], ['discrepant', 'duplicate'])
        self.assertEqual(rows[0]['reconciliation']['expected_cents'], 2000)
        self.assertEqual(rows[0]['reconciliation']['difference_cents'], 2800)
        self.assertEqual(self.client.get('/api/summary', params={'dataset': 'synthetic-parser'}).json()['overcharge_cents'], 2800)
        filtered = self.client.get('/api/invoices', params={'dataset': 'synthetic-parser', 'status': 'duplicate'}).json()
        self.assertEqual(len(filtered), 1)
        image = self.client.get('/api/invoices/' + rows[0]['id'] + '/image')
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.content, png())
        detail = self.client.get('/api/invoices/' + rows[0]['id']).json()
        self.assertTrue(detail['image_available'])
        self.assertEqual(detail['supporting_references']['purchase_orders'][0]['po_id'], 'PO-X')
        self.assertEqual(detail['original_fields']['total_cents'], 4800)
        self.assertEqual(detail['correction_history'], [])

    def test_strict_partial_correction_null_and_changed_duplicate_totals_persist(self):
        imported = self.imported()
        invoice_id = imported['invoice_ids']['0000']
        url = '/api/invoices/' + invoice_id
        original = self.client.get(url).json()
        response = self.client.patch(url, json={'fields': {'invoice_number': 'TEST-NEW'}, 'reason': 'Separate synthetic identities'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['current_fields']['quantity'], 2)
        self.assertEqual(self.service.summary('synthetic-parser')['overcharge_cents'], 5600)
        self.assertEqual([r['reconciliation']['status'] for r in self.service.invoices('synthetic-parser')], ['discrepant', 'discrepant'])
        response = self.client.patch(url, json={'fields': {'po_id': None}, 'reason': 'Clear unverified PO'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['current_fields']['po_id'])
        self.assertEqual(response.json()['reconciliation']['status'], 'unresolved')
        self.assertEqual(self.service.summary('synthetic-parser')['overcharge_cents'], 2800)
        with TestClient(create_app(self.settings, mount_ui=False)) as client:
            current = client.get(url).json()
        self.assertEqual(len(current['correction_history']), 2)
        self.assertEqual(current['original_evidence'], original['original_evidence'])
        self.assertEqual(current['review_state'], 'corrected')

    def test_unknown_fields_invalid_types_blank_reason_and_no_credential_echo(self):
        invoice_id = self.imported()['invoice_ids']['0000']
        url = '/api/invoices/' + invoice_id
        bodies = [
            {'fields': {'quantity': 2.5}, 'reason': 'Test'},
            {'fields': {'quantity': '2'}, 'reason': 'Test'},
            {'fields': {'quantity': True}, 'reason': 'Test'},
            {'fields': {'file_id': 'change'}, 'reason': 'Test'},
            {'fields': {'unknown': 'synthetic-secret'}, 'reason': 'Test'},
            {'fields': {'sku': ''}, 'reason': 'Test'},
            {'fields': {}, 'reason': 'Test'},
            {'fields': {'po_id': None}, 'reason': '   '},
            {'fields': {'po_id': None}, 'reason': 'Test', 'extra': True},
        ]
        for body in bodies:
            with self.subTest(body=body):
                response = self.client.patch(url, json=body)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn('synthetic-secret', response.text)
        self.assertEqual(self.service.detail(invoice_id)['correction_history'], [])
        self.assertEqual(self.client.get('/api/invoices/unknown').status_code, 404)
        self.assertEqual(self.client.patch('/api/invoices/unknown', json={'fields': {'quantity': 2}, 'reason': 'Test'}).status_code, 404)
        self.assertEqual(self.client.get('/api/summary', params={'dataset': 'missing'}).status_code, 404)
        self.assertEqual(self.client.get('/api/invoices', params={'dataset': 'synthetic-parser', 'status': 'invalid'}).status_code, 422)

    def test_upload_route_success_individual_ocr_failure_and_error_persistence(self):
        with patch('app.application_service.extract_document', side_effect=[RuntimeError('synthetic secret exception'), ExtractedDocument(TEXT, {'synthetic': True})]):
            response = self.client.post('/api/batches', data={'dataset': 'synthetic-api', 'mode': 'parser'},
                                        files=[('files', ('a.png', png(), 'image/png')), ('files', ('b.png', png('blue'), 'image/png'))])
        self.assertEqual(response.status_code, 201)
        self.assertEqual([r['status'] for r in response.json()['files']], ['error', 'success'])
        self.assertNotIn('secret exception', response.text)
        rows = self.service.invoices('synthetic-api')
        self.assertIsNone(rows[0]['current_fields'])
        self.assertIsNone(rows[0]['reconciliation'])
        self.assertIn('OCR failed', rows[0]['original_evidence']['record']['error'])
        self.assertEqual(self.service.summary('synthetic-api')['processing_errors'], 1)
        with patch('app.application_service.extract_document') as ocr:
            self.assertEqual(self.client.post('/api/batches', data={'dataset': 'synthetic-api', 'mode': 'parser'}, files={'files': ('c.png', png())}).status_code, 409)
            ocr.assert_not_called()

    def test_model_failure_no_fallback_and_continuation(self):
        fake = Mock()
        fake.extract.side_effect = [ExtractionError('synthetic model failure'),
                                   ExtractionResult(InvoiceFields(file_id='0001', **FIELDS), [], {'origin': 'synthetic_test'})]
        with patch('app.application_service.GeminiExtractor', return_value=fake), patch('app.application_service.extract_document', return_value=ExtractedDocument(TEXT, {})), patch('app.application_service.extract_invoice_fields') as parser:
            result = self.service.batch('synthetic-llm', 'llm', [('a.png', png()), ('b.png', png())])
        self.assertEqual([r['status'] for r in result['files']], ['error', 'success'])
        self.assertEqual(self.service.summary('synthetic-llm')['processing_errors'], 1)
        parser.assert_not_called()
        fake.close.assert_called_once()
        self.assertEqual(fake.extract.call_count, 2)

    def test_replay_verified_renamed_images_no_ocr_or_gemini_and_unknown_image_failure(self):
        self.imported()
        fake = Mock()
        fake.extract.return_value = ExtractionResult(InvoiceFields(file_id='0000', **FIELDS), [], {'origin': 'synthetic_test'})
        with patch.dict('os.environ', {}, clear=True), patch('app.application_service.ReplayExtractor', return_value=fake), patch('app.application_service.extract_document') as ocr, patch('app.application_service.GeminiExtractor') as gemini, patch('google.genai.Client') as client:
            result = self.service.batch('synthetic-replay', 'replay', [('renamed.png', png()), ('unknown.png', png('red'))])
        self.assertEqual([r['status'] for r in result['files']], ['success', 'error'])
        self.assertIn('Replay OCR association', result['files'][1]['error'])
        fake.extract.assert_called_once_with(TEXT, '0000')
        ocr.assert_not_called()
        gemini.assert_not_called()
        client.assert_not_called()
        self.assertEqual(self.service.detail(result['invoice_ids']['0000'])['file_id'], '0000')

    def test_replay_missing_capture_reports_error_without_parser_fallback(self):
        self.imported()
        with patch('app.application_service.extract_document') as ocr, patch('app.application_service.GeminiExtractor') as gemini:
            result = self.service.batch('synthetic-no-capture', 'replay', [('same.png', png())])
        self.assertEqual(result['files'][0]['status'], 'error')
        self.assertIn('Extraction failed', result['files'][0]['error'])
        ocr.assert_not_called()
        gemini.assert_not_called()

    def test_traversal_filename_safe_names_identical_uploads_and_jpeg(self):
        result = self.imported(uploads=[('../../evil.png', png()), ('../../evil.png', png()), ('jpeg.jpg', png(format='JPEG'))])
        self.assertEqual(len(result['invoice_ids']), 3)
        rows = self.service.invoices('synthetic-parser')
        self.assertEqual([r['original_evidence']['record']['original_filename'] for r in rows], ['../../evil.png', '../../evil.png', 'jpeg.jpg'])
        for row in rows:
            reference = Path(row['original_evidence']['image']['reference'])
            self.assertTrue(reference.is_relative_to(self.settings.runtime_dir))
            self.assertNotIn('evil', reference.name)
            self.assertTrue(self.service.detail(row['id'])['image_available'])
        self.assertFalse((self.folder / 'evil.png').exists())
        self.assertEqual(len({r['id'] for r in rows}), 3)
        self.assertEqual(rows[1]['reconciliation']['status'], 'duplicate')
        self.assertEqual(self.service.image(rows[2]['id'])[1], 'image/jpeg')

    def test_changed_missing_and_outside_allowed_image_references(self):
        result = self.imported()
        invoice_id = result['invoice_ids']['0000']
        image_path = Path(self.service.detail(invoice_id)['original_evidence']['image']['reference'])
        image_path.write_bytes(png('red'))
        self.assertEqual(self.client.get('/api/invoices/' + invoice_id + '/image').status_code, 409)
        self.assertFalse(self.service.detail(invoice_id)['image_available'])
        image_path.unlink()
        self.assertEqual(self.client.get('/api/invoices/' + invoice_id + '/image').status_code, 404)
        different = ApplicationService(replace(self.settings, runtime_dir=self.folder / 'other'))
        with self.assertRaises(ApplicationError) as context:
            different.image(result['invoice_ids']['0001'])
        self.assertEqual(context.exception.status, 404)
        self.assertEqual(self.client.get('/api/invoices/..%2F..%2F.env/image').status_code, 404)

    def test_invalid_image_empty_ocr_and_batch_limits(self):
        with patch('app.application_service.extract_document', return_value=ExtractedDocument('', {})) as ocr:
            result = self.service.batch('synthetic-errors', 'parser', [('a.png', b'not image'), ('b.png', png())])
        self.assertEqual([r['status'] for r in result['files']], ['error', 'error'])
        self.assertEqual(ocr.call_count, 1)
        self.assertEqual(self.service.summary('synthetic-errors')['processing_errors'], 2)
        for uploads in ([], [('x', png())] * 11, [('x', b'x' * (10 * 1024 * 1024 + 1))]):
            with self.assertRaises(ApplicationError) as exc:
                self.service.batch('limits', 'parser', uploads)
            self.assertEqual(exc.exception.status, 413)
        response = self.client.post('/api/batches', data={'dataset': 'other', 'mode': 'parser', 'references_path': '../../.env'}, files={'files': ('x.png', png())})
        self.assertEqual(response.status_code, 422)

    def test_ui_controller_shared_service_exact_integers_nulls_and_refresh(self):
        result = self.imported()
        controller = ReviewController(self.service)
        invoice_id = result['invoice_ids']['0000']
        values = [money_text(FIELDS[name]) if name.endswith('_cents') else str(FIELDS[name]) for name in FIELD_NAMES]
        values[0] = 'UNIQUE'
        edited = controller.save(invoice_id, values, 'Synthetic UI correction')
        self.assertEqual(edited['review_state'], 'corrected')
        table, summary, choices = controller.queue('synthetic-parser')
        self.assertEqual(summary['overcharge_cents'], 5600)
        self.assertEqual(table[1][3], 'Discrepant')
        values[4] = '2.0'
        with self.assertRaises(ApplicationError):
            controller.save(invoice_id, values, 'Reject float notation')
        values[4], values[2] = '2', ''
        cleared = controller.save(invoice_id, values, 'Clear PO using blank')
        self.assertIsNone(cleared['current_fields']['po_id'])
        values[5], values[6] = '90071992547409.93', '180143985094819.86'
        exact = controller.save(invoice_id, values, 'Exact large integer cents')
        self.assertEqual(exact['current_fields']['unit_price_cents'], 9007199254740993)
        self.assertEqual(exact['current_fields']['total_cents'], 18014398509481986)

    def test_no_db_connection_during_ocr_and_thread_scoped_operations(self):
        # A separate exclusive transaction is possible while OCR is executing.
        def document(path):
            with InvoiceStore(self.settings.db_path) as store:
                store.connection.execute('BEGIN EXCLUSIVE')
                store.connection.execute('ROLLBACK')
            return ExtractedDocument(TEXT, {})
        with patch('app.application_service.extract_document', side_effect=document):
            result = self.client.post('/api/batches', data={'dataset': 'synthetic-scope', 'mode': 'parser'}, files={'files': ('image.png', png())})
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json()['files'][0]['status'], 'success')

    def test_busy_batch_is_conflict(self):
        BATCH_LOCK.acquire()
        try:
            with self.assertRaises(ApplicationError) as exc:
                self.service.batch('busy', 'parser', [('x.png', png())])
            self.assertEqual(exc.exception.status, 409)
        finally:
            BATCH_LOCK.release()

    def test_ui_display_keeps_cents_exact_above_javascript_integer_limit(self):
        self.assertEqual(format_usd(9007199254740993), '$90,071,992,547,409.93')

    def test_unexpected_batch_error_is_sanitized_and_lock_released(self):
        with patch('app.application_service.extract_document', return_value=ExtractedDocument(TEXT, {})), patch.object(self.service, '_operation', side_effect=[[], RuntimeError('synthetic internal secret')]):
            with self.assertRaises(ApplicationError) as exc:
                self.service.batch('unexpected', 'parser', [('a.png', png())])
        self.assertEqual(exc.exception.status, 500)
        self.assertNotIn('secret', str(exc.exception))
        self.assertFalse(BATCH_LOCK.locked())

    def test_empty_reason_in_ui_is_explicit_and_replay_error_is_explanatory(self):
        result = self.imported()
        controller = ReviewController(self.service)
        with self.assertRaises(ApplicationError) as exc:
            controller.save(result['invoice_ids']['0000'], [''] * 7, ' ')
        self.assertIn('reason', str(exc.exception))
        replay = self.service.batch('unknown-replay', 'replay', [('unknown.png', png('yellow'))])
        self.assertIn('verified saved image/OCR association', replay['files'][0]['error'])

    def test_credential_bearing_model_error_is_not_saved_or_returned(self):
        synthetic_key = 'AIza' + 'Z' * 35
        fake = Mock()
        fake.extract.side_effect = ExtractionError('synthetic credential echo ' + synthetic_key)
        with patch('app.application_service.GeminiExtractor', return_value=fake), patch('app.application_service.extract_document', return_value=ExtractedDocument(TEXT, {})):
            result = self.service.batch('synthetic-credential-test', 'llm', [('a.png', png())])
        self.assertNotIn(synthetic_key, json.dumps(result))
        for path in self.settings.runtime_dir.rglob('*.json'):
            self.assertNotIn(synthetic_key, path.read_text())
        self.assertNotIn(synthetic_key, json.dumps(self.service.invoices('synthetic-credential-test')))

    def test_llm_upload_then_real_replay_uses_shared_capture_and_new_caller_ids(self):
        from google.genai import types
        # Real SDK response serialization/capture writing; explicitly synthetic.
        response = types.GenerateContentResponse.model_validate({
            'candidates': [{'finish_reason': 'STOP', 'content': {'parts': [
                {'text': json.dumps({'fields': FIELDS, 'issues': []})}]}}],
            'model_version': 'gemini-synthetic-test',
            'usage_metadata': {'total_token_count': 25}})
        service = ApplicationService(
            self.settings,
            gemini_factory=partial(GeminiExtractor,
                                   configuration=RequestConfiguration('gemini-synthetic-test'),
                                   api_key='synthetic-test-credential', origin='synthetic_test'),
            replay_factory=partial(ReplayExtractor, allow_synthetic=True))
        with patch('google.genai.models.Models.generate_content', return_value=response) as network, \
                patch('app.application_service.extract_document', return_value=ExtractedDocument(TEXT, {'synthetic': True})) as ocr:
            live = service.batch('synthetic-captured-live', 'llm', [('live.png', png())])
            self.assertEqual(live['files'][0]['status'], 'success')
            self.assertEqual(live['files'][0]['fields'], dict(file_id='0000', **FIELDS))
            network.assert_called_once()
            ocr.assert_called_once()
            capture_ref = live['files'][0]['metadata']['capture']
            capture_path = self.settings.capture_dir / capture_ref
            original_capture = capture_path.read_bytes()
            capture = json.loads(original_capture)
            self.assertEqual(capture['origin'], 'synthetic_test')
            self.assertEqual(json.loads(capture['original_response_text']), {'fields': FIELDS, 'issues': []})
            self.assertNotIn('file_id', json.loads(capture['original_response_text'])['fields'])
            self.assertEqual(len(list(self.settings.capture_dir.rglob('*.capture.json'))), 1)
            self.assertEqual(list(self.settings.runtime_dir.glob('batches/*/captures')), [])
            original_evidence = service.detail(live['invoice_ids']['0000'])['original_evidence']
            self.assertTrue(original_evidence['capture']['available'])
            self.assertEqual(original_evidence['capture']['reference'], capture_ref)
            # Production replay still rejects synthetic captures; only this test opts in.
            with self.assertRaisesRegex(ExtractionError, 'real Gemini'):
                ReplayExtractor(self.settings.capture_dir).extract(TEXT, 'production')
            network.reset_mock()
            ocr.reset_mock()
            network.side_effect = AssertionError('Replay must not request Gemini')
            ocr.side_effect = AssertionError('Replay must not run OCR')
            with patch.dict(os.environ, {}, clear=True):
                replay = service.batch('synthetic-captured-replay', 'replay',
                                       [('renamed-a.png', png()), ('renamed-b.png', png())])
            network.assert_not_called()
            ocr.assert_not_called()
        self.assertEqual([r['status'] for r in replay['files']], ['success', 'success'])
        for ordinal, record in enumerate(replay['files']):
            self.assertEqual(record['fields'], dict(file_id=f'{ordinal:04d}', **FIELDS))
            self.assertEqual(record['issues'], live['files'][0]['issues'])
            self.assertEqual(record['metadata']['capture'], capture_ref)
            stored = service.detail(replay['invoice_ids'][record['file_id']])
            self.assertTrue(stored['original_evidence']['capture']['available'])
            self.assertEqual(stored['current_fields'], record['fields'])
        self.assertEqual(capture_path.read_bytes(), original_capture)
        self.assertEqual(len(list(self.settings.capture_dir.rglob('*.capture.json'))), 1)
        self.assertEqual(service.detail(live['invoice_ids']['0000'])['original_evidence'], original_evidence)

    def test_legacy_capture_import_preserves_originals_and_refuses_overwrite(self):
        # Synthetic byte fixtures test migration only, never stand in for Gemini output.
        legacy = self.settings.runtime_dir / 'batches' / 'old-run' / 'captures' / ('a' * 64)
        legacy.mkdir(parents=True)
        source = legacy / 'synthetic.capture.json'
        source.write_bytes(b'{"origin":"synthetic_test","purpose":"migration fixture only"}')
        failure = legacy / 'synthetic.failure.json'
        failure.write_bytes(b'{"origin":"synthetic_test","error":"synthetic failure fixture"}')
        previous = self.settings.capture_dir / 'previous' / 'preserved.capture.json'
        previous.parent.mkdir(parents=True)
        previous.write_bytes(b'{"origin":"synthetic_test","purpose":"preexisting fixture only"}')
        for _ in range(2):
            self.assertEqual(self.service._capture_storage(), self.settings.capture_dir)
            self.assertEqual((self.settings.capture_dir / source.parent.name / source.name).read_bytes(), source.read_bytes())
            self.assertEqual((self.settings.capture_dir / failure.parent.name / failure.name).read_bytes(), failure.read_bytes())
        self.assertEqual(previous.read_bytes(), b'{"origin":"synthetic_test","purpose":"preexisting fixture only"}')
        before = source.read_bytes()
        destination = self.settings.capture_dir / source.parent.name / source.name
        destination.write_bytes(b'{"origin":"synthetic_test","purpose":"conflicting fixture only"}')
        conflict = destination.read_bytes()
        with self.assertRaises(ApplicationError) as exc:
            self.service._capture_storage()
        self.assertEqual(exc.exception.status, 409)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(destination.read_bytes(), conflict)
