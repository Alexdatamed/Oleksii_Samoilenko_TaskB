"""Offline notes tests: all provider responses are explicitly synthetic.

Only SDK network requests are mocked in generation/replay tests; capture writing,
local validation, storage, reconciliation, and ReplayNotes are real.
"""
from contextlib import closing
from copy import deepcopy
from functools import partial
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from google.genai import types, errors
from app.api import create_app
from app.application_service import ApplicationService, Settings
from app.discrepancy_notes import (GeminiNotes, ReplayNotes, NoteConfiguration,
    NoteUnavailable, build_context, process_note, usd)
from app.invoice_service import InvoiceService
from app.llm_extraction import ExtractionError, json_hash
from app.note_service import NoteService
from app.storage import InvoiceStore
from app.ui import note_html, build_ui

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / 'runtime/extraction/parser/summary.json'
REFERENCES = ROOT / 'client-ai-starter-pack/tasks/invoices/seed.json'
MODEL = 'gemini-synthetic-test'
# Independently specified, never derived from implementation or fixture expectations.
WRONG_PRICE = {
    'note': 'Draft for human review: Invoice INV-2 bills a unit price above PO PO-2. Receipt RC-2 supports the delivered quantity; the verified difference is $20.00.',
    'source_references': ['Invoice INV-2', 'PO PO-2', 'Receipt RC-2'],
    'finding_references': ['unit_price_mismatch'],
    'amount_references': [{'key': 'result.difference_cents', 'cents': 2000}]}
DUPLICATE = {
    'note': 'Draft for human review: Invoice INV-1 repeats the supplier and invoice number of Earlier invoice INV-1. It is excluded from supported overcharge.',
    'source_references': ['Invoice INV-1', 'Earlier invoice INV-1'],
    'finding_references': ['duplicate_identity'], 'amount_references': []}
MISSING = {
    'note': 'Draft for human review: Invoice INV-3 has no purchase-order reference. A match cannot be confirmed and this invoice is excluded from supported overcharge.',
    'source_references': ['Invoice INV-3'],
    'finding_references': ['missing_po_reference'], 'amount_references': []}


def sdk(payload=WRONG_PRICE, *, raw=None, finish='STOP', blocked=False):
    data = {'candidates': [{'finish_reason': finish, 'content': {'parts': [
        {'text': json.dumps(payload) if raw is None else raw}]}}],
        'model_version': MODEL, 'usage_metadata': {'total_token_count': 30}}
    if blocked:
        data['prompt_feedback'] = {'block_reason': 'SAFETY'}
    return types.GenerateContentResponse.model_validate(data)


class NoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.settings = Settings(db_path=self.folder/'invoices.sqlite3', capture_dir=self.folder/'captures', runtime_dir=self.folder/'review')
        with InvoiceStore(self.settings.db_path) as store:
            self.ids = InvoiceService(store).import_results(SUMMARY, REFERENCES, dataset='notes', mode='parser')['invoice_ids']
        self.service = ApplicationService(self.settings,
            note_factory=partial(GeminiNotes, configuration=NoteConfiguration(MODEL), api_key='synthetic-test-credential', origin='synthetic_test'),
            note_replay_factory=partial(ReplayNotes, allow_synthetic=True))

    def context(self, name='wrong-price'):
        return self.service._operation(lambda service: NoteService(service).snapshot(self.ids[name]))

    def generate(self, name='wrong-price', payload=WRONG_PRICE):
        with patch('google.genai.models.Models.generate_content', return_value=sdk(payload)) as request:
            result = self.service.draft_note(self.ids[name])
        request.assert_called_once()
        return result

    def capture(self):
        return next(self.service.note_capture_dir.rglob('*.capture.json'))

    def test_wrong_price_exact_verified_context_and_actual_identifiers(self):
        context = self.context()
        self.assertEqual(context['status'], 'discrepant')
        self.assertEqual(context['amounts']['result.difference_cents'], {'cents': 2000, 'usd': '$20.00'})
        self.assertEqual(context['amounts']['result.expected_cents'], {'cents': 10000, 'usd': '$100.00'})
        self.assertEqual([s['label'] for s in context['sources']], ['Invoice INV-2', 'PO PO-2', 'Receipt RC-2'])
        self.assertEqual(context['sources'][2]['identifiers']['receipt_id'], 'RC-2')
        self.assertEqual(process_note(json.dumps(WRONG_PRICE), sdk().model_dump(mode='json'), context), WRONG_PRICE)
        self.assertEqual(usd(0), '$0.00')
        self.assertEqual(usd(-1), '-$0.01')
        self.assertEqual(usd(9007199254740993), '$90,071,992,547,409.93')

    def test_missing_duplicate_and_ambiguous_evidence_without_invention(self):
        missing = self.context('missing-reference')
        self.assertEqual([s['type'] for s in missing['sources']], ['invoice'])
        self.assertIsNone(missing['amounts']['result.expected_cents']['cents'])
        self.assertTrue(missing['overcharge_excluded'])
        self.assertEqual(self.generate('missing-reference', MISSING)['state'], 'current')
        duplicate = self.context('duplicate')
        self.assertEqual(duplicate['earlier_invoice_identity'], {'invoice_number': 'INV-1', 'supplier_id': 'S1', 'source_file_id': 'clean', 'dataset_order': 0})
        self.assertEqual(self.generate('duplicate', DUPLICATE)['state'], 'current')
        row = self.service.detail(self.ids['duplicate'])
        self.assertIsNone(build_context(row)['earlier_invoice_identity'])
        row = self.service.detail(self.ids['wrong-price'])
        row['supporting_references']['purchase_orders'] *= 2
        row['findings'] = ['ambiguous_po_reference']
        row['reconciliation']['findings'] = ['ambiguous_po_reference']
        context = build_context(row)
        self.assertEqual([s['type'] for s in context['sources']], ['invoice'])
        self.assertEqual(len(context['unconfirmed_candidates']['purchase_orders']), 2)
        self.assertEqual(len(context['unconfirmed_candidates']['receipts']), 1)

    def test_reconciled_and_processing_error_never_construct_provider(self):
        with patch('app.application_service.GeminiNotes') as live, patch('app.application_service.ReplayNotes') as replay:
            service = ApplicationService(self.settings)
            self.assertEqual(service.draft_note(self.ids['clean'])['state'], 'not_needed')
            self.assertEqual(service.draft_note(self.ids['clean'], 'replay')['message'], 'No discrepancy note needed')
            data = json.loads(SUMMARY.read_text(encoding='utf-8'))
            data['files'][0].update(status='error', fields=None, error='Synthetic processing error')
            path = self.folder/'error-summary.json'
            path.write_text(json.dumps(data),encoding='utf-8')
            with InvoiceStore(self.settings.db_path) as store:
                ids = InvoiceService(store).import_results(path, REFERENCES, dataset='synthetic-errors', mode='parser')['invoice_ids']
            self.assertEqual(service.draft_note(ids[data['files'][0]['file_id']])['state'], 'requires_reconciliation')
            live.assert_not_called()
            replay.assert_not_called()

    def test_capture_real_writer_and_keyless_replay_immutable_evidence(self):
        before = self.service.detail(self.ids['wrong-price'])
        with patch('google.genai.models.Models.generate_content', return_value=sdk()) as network:
            live = self.service.draft_note(self.ids['wrong-price'])
        self.assertEqual(live['state'], 'current')
        self.assertEqual(live['draft'], WRONG_PRICE)
        request = network.call_args.kwargs
        self.assertEqual(json.loads(request['contents']), self.context())
        self.assertEqual(request['model'], MODEL)
        self.assertEqual(request['config'].response_mime_type, 'application/json')
        raw_bytes = self.capture().read_bytes()
        record = json.loads(raw_bytes)
        self.assertEqual(record['origin'], 'synthetic_test')
        self.assertEqual(record['purpose'], 'discrepancy-note')
        self.assertEqual(json.loads(record['original_response_text']), WRONG_PRICE)
        self.assertEqual(record['context_sha256'], json_hash(self.context()))
        self.assertEqual(record['sdk_version'], '2.28.0')
        self.assertEqual(record['configuration']['transport']['sdk_attempts'], 1)
        self.assertNotIn('synthetic-test-credential', raw_bytes.decode())
        with patch.dict(os.environ, {}, clear=True), patch('dotenv.load_dotenv') as dotenv, patch('google.genai.Client') as client, patch('google.genai.models.Models.generate_content') as network, patch('app.application_service.extract_document') as ocr:
            replay = self.service.draft_note(self.ids['wrong-price'], 'replay')
            self.assertEqual(replay['draft'], WRONG_PRICE)
            self.assertEqual(replay['state'], 'current')
            client.assert_not_called()
            network.assert_not_called()
            ocr.assert_not_called()
            dotenv.assert_not_called()
        self.assertEqual(self.capture().read_bytes(), raw_bytes)
        self.assertEqual(len(replay['history']), 2)
        self.assertEqual(self.service.detail(self.ids['wrong-price']), before)
        with self.assertRaisesRegex(ExtractionError, 'real Gemini'):
            ReplayNotes(self.service.note_capture_dir).generate(self.context())

    def test_invalid_responses_are_captured_before_validation(self):
        variants = [sdk(raw='not JSON'), sdk(raw=''), sdk(finish='MAX_TOKENS'), sdk(blocked=True), types.GenerateContentResponse(),
                    sdk(dict(WRONG_PRICE, extra='unexpected')), sdk(dict(WRONG_PRICE, note=123)),
                    sdk(dict(WRONG_PRICE, source_references=['PO INVENTED'])),
                    sdk(dict(WRONG_PRICE, finding_references=['invented'])),
                    sdk(dict(WRONG_PRICE, amount_references=[{'key':'result.difference_cents','cents':2001}])),
                    sdk(dict(WRONG_PRICE, note=WRONG_PRICE['note']+' The difference is $99.00.')),
                    sdk(dict(WRONG_PRICE, amount_references=[]))]
        before = self.service.detail(self.ids['wrong-price'])
        for response in variants:
            with self.subTest(response=response.candidates), patch('google.genai.models.Models.generate_content', return_value=response):
                result = self.service.draft_note(self.ids['wrong-price'])
                self.assertEqual(result['state'], 'failed')
                self.assertIsNone(result['draft'])
        captures = list(self.service.note_capture_dir.rglob('*.capture.json'))
        self.assertEqual(len(captures), len(variants))
        self.assertEqual(self.service.detail(self.ids['wrong-price']), before)
        for capture in captures:
            self.assertIn('original_response_text', json.loads(capture.read_text(encoding='utf-8')))

    def test_provider_failure_no_fallback_or_reconciliation_change(self):
        before = self.service.detail(self.ids['wrong-price'])
        exc = errors.ClientError(403, {'error': {'message': 'synthetic secret echo; never retain this'}})
        with patch('google.genai.models.Models.generate_content', side_effect=exc) as request:
            result = self.service.draft_note(self.ids['wrong-price'])
        request.assert_called_once()
        self.assertEqual(result['state'], 'failed')
        self.assertIsNone(result['draft'])
        self.assertEqual(self.service.detail(self.ids['wrong-price']), before)
        failure = next(self.service.note_capture_dir.rglob('*.failure.json')).read_text(encoding='utf-8')
        self.assertNotIn('secret echo', failure)
        self.assertNotIn('secret echo', json.dumps(result))

    def test_replay_missing_and_incompatible_model_are_explicit(self):
        self.assertEqual(self.service.draft_note(self.ids['wrong-price'], 'replay')['state'], 'unavailable')
        self.generate()
        result = self.service.draft_note(self.ids['wrong-price'], 'replay', 'gemini-other-test')
        self.assertEqual(result['state'], 'unavailable')
        self.assertIsNone(result['draft'])

    def test_replay_rejects_tampering_and_changed_prompt_schema_config(self):
        self.generate()
        path = self.capture()
        original = path.read_bytes()
        for key in ('context', 'configuration_fingerprint', 'original_response_text', 'sdk_response', 'origin', 'timestamp', 'sdk_version'):
            record = json.loads(original)
            if key == 'context': record[key]['status'] = 'unresolved'
            elif key == 'sdk_response': record[key]['model_version'] = 'changed'
            else: record[key] = 'changed'
            path.write_text(json.dumps(record),encoding='utf-8')
            with self.subTest(key=key), self.assertRaises(ExtractionError):
                ReplayNotes(self.service.note_capture_dir, allow_synthetic=True).generate(self.context())
        for key in ('prompt', 'schema', 'configuration'):
            record = json.loads(original)
            if key == 'configuration':
                record[key]['generation_settings']['temperature'] = 1
                record['configuration_fingerprint'] = json_hash(record[key])
            else: record[key] = 'incompatible'
            # A well-formed record of a different configuration still must not replay.
            record['capture_sha256'] = json_hash({k:v for k,v in record.items() if k != 'capture_sha256'})
            path.write_text(json.dumps(record),encoding='utf-8')
            with self.subTest(key=key), self.assertRaises(NoteUnavailable):
                ReplayNotes(self.service.note_capture_dir, allow_synthetic=True).generate(self.context())
        path.write_bytes(original)
        other = deepcopy(self.context()); other['sources'][0]['fields']['quantity'] = 99
        with self.assertRaises(NoteUnavailable):
            ReplayNotes(self.service.note_capture_dir, allow_synthetic=True).generate(other)

    def test_persistence_reopen_and_immutable_history(self):
        result = self.generate()
        reopened = ApplicationService(self.settings).note(self.ids['wrong-price'])
        self.assertEqual(reopened, result)
        with InvoiceStore(self.settings.db_path) as store:
            for statement in ('UPDATE discrepancy_notes SET draft=NULL', 'DELETE FROM discrepancy_notes'):
                with self.assertRaises(sqlite3.IntegrityError):
                    store.connection.execute(statement)

    def test_safe_migration_from_version_one_preserves_existing_data(self):
        before = self.service.detail(self.ids['wrong-price'])
        with closing(sqlite3.connect(self.settings.db_path, isolation_level=None)) as db:
            db.execute('DROP TABLE discrepancy_notes'); db.execute('PRAGMA user_version=1')
        with InvoiceStore(self.settings.db_path) as store:
            self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(InvoiceService(store).get_invoice(self.ids['wrong-price'])['original_evidence'], before['original_evidence'])
        self.assertEqual(self.service.detail(self.ids['wrong-price']), before)
        self.assertEqual(self.service.note(self.ids['wrong-price'])['history'], [])

    def test_staleness_after_correction_and_affected_duplicate(self):
        original = self.generate()
        self.generate('duplicate', DUPLICATE)
        self.service.correct(self.ids['wrong-price'], {'unit_price_cents':2600,'total_cents':13000}, 'Synthetic correction provenance')
        stale = self.service.note(self.ids['wrong-price'])
        self.assertEqual(stale['state'], 'outdated'); self.assertIsNone(stale['draft'])
        self.assertEqual(stale['history'][0]['draft'], original['draft'])
        self.assertNotEqual(stale['context_hash'], original['context_hash'])
        self.assertEqual(self.context()['sources'][0]['correction_provenance']['reason'], 'Synthetic correction provenance')
        self.service.correct(self.ids['clean'], {'invoice_number':'SYNTHETIC-NEW'}, 'Synthetic identity correction')
        duplicate = self.service.note(self.ids['duplicate'])
        self.assertEqual(duplicate['state'], 'not_needed')
        self.assertEqual(duplicate['history'][0]['state'], 'outdated')
        self.assertEqual(self.service.detail(self.ids['duplicate'])['reconciliation']['status'], 'reconciled')

    def test_context_changes_during_generation_no_open_database_transaction(self):
        def request(**kwargs):
            # An exclusive transaction is possible while Gemini would be running.
            with closing(sqlite3.connect(self.settings.db_path, timeout=0.1)) as db:
                db.execute('BEGIN EXCLUSIVE'); db.rollback()
            self.service.correct(self.ids['wrong-price'], {'unit_price_cents':2600,'total_cents':13000}, 'Synthetic concurrent edit')
            return sdk()
        with patch('google.genai.models.Models.generate_content', side_effect=request):
            result = self.service.draft_note(self.ids['wrong-price'])
        self.assertEqual(result['state'], 'outdated')
        self.assertTrue(result['generation_context_changed'])
        self.assertIsNone(result['draft'])
        self.assertEqual(result['history'][0]['draft'], WRONG_PRICE)
        self.assertEqual(self.service.detail(self.ids['wrong-price'])['reconciliation']['difference_cents'], 3000)

    def test_api_generate_retrieve_replay_and_safe_errors(self):
        app = create_app(self.settings, mount_ui=False)
        app.state.service.note_factory = self.service.note_factory
        app.state.service.note_replay_factory = self.service.note_replay_factory
        with TestClient(app) as client:
            url = '/api/invoices/' + self.ids['wrong-price'] + '/note'
            self.assertEqual(client.get(url).json()['state'], 'unavailable')
            with patch('google.genai.models.Models.generate_content', return_value=sdk()):
                self.assertEqual(client.post(url+'/generate').json()['state'], 'current')
            with patch.dict(os.environ, {}, clear=True), patch('google.genai.Client') as network:
                self.assertEqual(client.post(url+'/replay',json={}).json()['draft'], WRONG_PRICE)
                network.assert_not_called()
            self.assertEqual(client.get('/api/invoices/no-such-invoice/note').status_code, 404)
            self.assertEqual(client.post(url+'/replay',json={'unknown':'synthetic private data'}).status_code,422)

    def test_safe_readable_ui_and_only_explicit_buttons_request_notes(self):
        result = self.generate()
        result['draft']['note'] = '<script>alert(1)</script> Invoice INV-2'
        result['draft']['source_references'] = ['<img src=x onerror=alert(1)>']
        html = note_html(result)
        self.assertIn('&lt;script&gt;', html); self.assertNotIn('<script>',html)
        self.assertIn('&lt;img',html)
        self.assertIn('Synthetic test draft',html)
        for hidden in ('context_hash','capture','configuration','sdk_version'):
            self.assertNotIn(hidden,html)
        ui = build_ui(self.service)
        show = next(fn.fn for fn in ui.fns.values() if fn.fn.__name__=='show')
        with patch('google.genai.Client') as network:
            show(self.ids['wrong-price']); show(self.ids['clean'])
            network.assert_not_called()
        components=ui.config['components']
        self.assertFalse(any(c['type']=='json' for c in components))
        for label in ('Generate discrepancy note','Replay saved note'):
            self.assertTrue(any(c['type']=='button' and c['props']['value']==label for c in components))
        action_ids={idx for idx,fn in ui.fns.items() if fn.fn.__name__=='note_action'}
        types_by_id={c['id']:c['type'] for c in components}
        for dependency in ui.config['dependencies']:
            if dependency['id'] in action_ids:
                self.assertTrue(all(types_by_id[target[0]]=='button' and target[1]=='click' for target in dependency['targets']))


if __name__ == '__main__':
    unittest.main()
