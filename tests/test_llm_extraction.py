"""Offline tests. Every mocked response/capture here is synthetic, never real."""
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from google.genai import errors

from app.llm_extraction import (
    ExtractionError, GeminiExtractor, ReplayExtractor, RequestConfiguration,
    load_live_configuration, process_response, response_schema, json_hash,
)
from app.reconciliation import reconcile_invoice
from scripts.run_extraction import main

SYNTHETIC_OCR = 'Synthetic test document: Invoice B-7 Supplier V-8 PO ID P-9 SKU X quantity 2 USD 1.23 total USD 2.46'
SYNTHETIC_FIELDS = dict(invoice_number='B-7', supplier_id='V-8', po_id='P-9', sku='X',
                        quantity=2, unit_price_cents=123, total_cents=246)
SYNTHETIC_KEY = 'synthetic-test-credential'


def synthetic_sdk(raw=None, payload=None, finish='STOP', blocked=False):
    if raw is None:
        raw = json.dumps(payload if payload is not None else {'fields': SYNTHETIC_FIELDS, 'issues': []})
    return {'candidates': [{'finish_reason': finish, 'content': {'parts': [{'text': raw}]}}],
            'model_version': 'gemini-synthetic-test', 'usage_metadata': {'total_token_count': 25},
            'prompt_feedback': {'block_reason': 'SAFETY'} if blocked else None}


def synthetic_extractor(directory, sdk=None):
    extractor = GeminiExtractor(directory, RequestConfiguration('gemini-synthetic-test'),
                                SYNTHETIC_KEY, origin='synthetic_test')
    response = Mock()
    response.model_dump.return_value = sdk if sdk is not None else synthetic_sdk()
    extractor._request = Mock(return_value=(response, 1))
    return extractor


class GeminiExtractionTests(unittest.TestCase):
    def test_valid_structured_response_and_reconciliation(self):
        sdk = synthetic_sdk()
        fields, issues = process_response(sdk['candidates'][0]['content']['parts'][0]['text'], sdk, 'caller')
        self.assertEqual(fields.model_dump(), dict(file_id='caller', **SYNTHETIC_FIELDS))
        self.assertEqual(issues, [])
        po = dict(po_id='P-9', supplier_id='V-8', sku='X', quantity=2, unit_cents=123)
        receipt = dict(receipt_id='R-10', po_id='P-9', sku='X', quantity=2)
        result = reconcile_invoice(fields.to_reconciliation_dict(), [po], [receipt])
        self.assertEqual((result['status'], result['difference_cents']), ('reconciled', 0))

    def test_null_and_omitted_fields_are_reported(self):
        for omit in (False, True):
            values = dict(SYNTHETIC_FIELDS, po_id=None)
            if omit:
                del values['po_id']
            sdk = synthetic_sdk(payload={'fields': values, 'issues': []})
            fields, issues = process_response(sdk['candidates'][0]['content']['parts'][0]['text'], sdk, 'caller')
            self.assertIsNone(fields.po_id)
            self.assertEqual([(i.field, i.code) for i in issues], [('po_id', 'missing')])

    def test_explicit_invalid_and_conflicting_issues(self):
        for code in ('invalid', 'conflicting', 'missing'):
            payload = {'fields': dict(SYNTHETIC_FIELDS, quantity=None),
                       'issues': [{'field': 'quantity', 'code': code, 'message': 'Synthetic evidence issue',
                                   'raw_values': ['synthetic evidence']}]}
            sdk = synthetic_sdk(payload=payload)
            fields, issues = process_response(json.dumps(payload), sdk, 'caller')
            self.assertIsNone(fields.quantity)
            self.assertEqual(issues[0].code, code)

    def test_invalid_types_schema_and_extra_file_id(self):
        for values in (dict(SYNTHETIC_FIELDS, quantity='2'), dict(SYNTHETIC_FIELDS, quantity=2.5),
                       dict(SYNTHETIC_FIELDS, quantity=True), dict(SYNTHETIC_FIELDS, unit_price_cents=123.0),
                       dict(SYNTHETIC_FIELDS, supplier_id=''), dict(SYNTHETIC_FIELDS, file_id='from-model')):
            with self.subTest(values=values):
                raw = json.dumps({'fields': values, 'issues': []})
                with self.assertRaises(ExtractionError):
                    process_response(raw, synthetic_sdk(raw=raw), 'caller')
        for payload in ({'fields': SYNTHETIC_FIELDS}, {'fields': [], 'issues': []},
                        {'fields': SYNTHETIC_FIELDS, 'issues': [], 'status': 'reconciled'},
                        {'fields': SYNTHETIC_FIELDS, 'issues': [{'field': 'quantity', 'code': 'business', 'message': 'x'}]}):
            raw = json.dumps(payload)
            with self.assertRaises(ExtractionError):
                process_response(raw, synthetic_sdk(raw=raw), 'caller')

    def test_issue_for_known_field_is_schema_failure(self):
        payload = {'fields': SYNTHETIC_FIELDS, 'issues': [{'field': 'po_id', 'code': 'missing', 'message': 'x'}]}
        with self.assertRaisesRegex(ExtractionError, 'non-null'):
            process_response(json.dumps(payload), synthetic_sdk(payload=payload), 'caller')

    def test_invalid_json_duplicate_keys_and_nonfinite_values(self):
        for raw in ('not JSON', '{', '```json\n{}\n```', '{"fields":{},"fields":{},"issues":[]}',
                    '{"fields":{"quantity":NaN},"issues":[]}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ExtractionError, 'JSON'):
                process_response(raw, synthetic_sdk(raw=raw), 'caller')

    def test_empty_blocked_truncated_and_no_candidates(self):
        for sdk in (synthetic_sdk(raw=''), synthetic_sdk(blocked=True),
                    synthetic_sdk(finish='MAX_TOKENS'), synthetic_sdk(finish='SAFETY'),
                    {'candidates': []}):
            with self.subTest(sdk=sdk), self.assertRaises(ExtractionError):
                raw = sdk.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '') if sdk.get('candidates') else ''
                process_response(raw, sdk, 'caller')

    def test_zero_negative_and_inconsistent_total_are_not_corrected(self):
        for values in (dict(SYNTHETIC_FIELDS, quantity=0, unit_price_cents=0, total_cents=0),
                       dict(SYNTHETIC_FIELDS, quantity=-2, unit_price_cents=-123, total_cents=-999)):
            raw = json.dumps({'fields': values, 'issues': []})
            fields, issues = process_response(raw, synthetic_sdk(raw=raw), 'caller')
            self.assertEqual(fields.model_dump(), dict(file_id='caller', **values))
            self.assertEqual(issues, [])

    def test_raw_capture_precedes_validation_and_preserves_each_response(self):
        for sdk in (synthetic_sdk(raw='{invalid JSON'), synthetic_sdk(finish='MAX_TOKENS'),
                    synthetic_sdk(blocked=True), synthetic_sdk(payload={'fields': dict(SYNTHETIC_FIELDS, quantity='2'), 'issues': []})):
            with TemporaryDirectory() as directory:
                extractor = synthetic_extractor(directory, sdk)
                for _ in range(2):
                    with self.assertRaises(ExtractionError) as caught:
                        extractor.extract(SYNTHETIC_OCR, 'caller')
                    self.assertIn('capture', caught.exception.metadata)
                paths = list(Path(directory).rglob('*.capture.json'))
                self.assertEqual(len(paths), 2)
                capture = json.loads(paths[0].read_text())
                self.assertEqual(capture['origin'], 'synthetic_test')
                self.assertEqual(capture['sdk_response'], sdk)
                self.assertEqual(capture['ocr_text'], SYNTHETIC_OCR)
                self.assertNotIn('fields', capture)
                self.assertNotIn(SYNTHETIC_KEY, paths[0].read_text())

    def test_replay_without_credentials_network_or_dotenv_and_renamed_source(self):
        with TemporaryDirectory() as directory:
            live = synthetic_extractor(directory).extract(SYNTHETIC_OCR, 'original')
            with patch.dict(os.environ, {}, clear=True), patch('google.genai.Client', side_effect=AssertionError('network client forbidden')), \
                    patch('dotenv.load_dotenv', side_effect=AssertionError('dotenv forbidden')):
                replay = ReplayExtractor(directory, allow_synthetic=True).extract(SYNTHETIC_OCR, 'renamed')
            self.assertEqual(replay.fields.file_id, 'renamed')
            self.assertEqual(replay.fields.invoice_number, 'B-7')
            self.assertEqual(replay.metadata['capture'], live.metadata['capture'])
            self.assertEqual(replay.issues, [])
            with self.assertRaisesRegex(ExtractionError, 'real Gemini'):
                ReplayExtractor(directory).extract(SYNTHETIC_OCR, 'caller')

    def test_replay_missing_content_and_model_configuration_mismatch(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ExtractionError, 'No captured'):
                ReplayExtractor(directory).extract(SYNTHETIC_OCR, 'caller')
            synthetic_extractor(directory).extract(SYNTHETIC_OCR, 'caller')
            with self.assertRaisesRegex(ExtractionError, 'No captured'):
                ReplayExtractor(directory, allow_synthetic=True).extract(SYNTHETIC_OCR + 'changed', 'caller')
            with self.assertRaisesRegex(ExtractionError, 'configuration'):
                ReplayExtractor(directory, model='gemini-other', allow_synthetic=True).extract(SYNTHETIC_OCR, 'caller')

    def test_replay_detects_tampered_ocr_config_prompt_schema_and_response(self):
        for mutation, message in (('ocr', 'hash'), ('config', 'configuration'),
                                  ('prompt', 'configuration'), ('schema', 'configuration'),
                                  ('response', 'integrity')):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                synthetic_extractor(directory).extract(SYNTHETIC_OCR, 'caller')
                path = next(Path(directory).rglob('*.capture.json'))
                record = json.loads(path.read_text())
                if mutation == 'ocr':
                    record['ocr_text'] += 'changed'
                elif mutation == 'config':
                    record['configuration']['generation_settings']['temperature'] = 1
                    record['configuration_fingerprint'] = json_hash(record['configuration'])
                elif mutation == 'prompt':
                    record['prompt'] += 'changed'
                elif mutation == 'schema':
                    record['schema']['required'] = []
                else:
                    record['original_response_text'] = '{}'
                path.write_text(json.dumps(record), encoding='utf-8')
                with self.assertRaisesRegex(ExtractionError, message):
                    ReplayExtractor(directory, allow_synthetic=True).extract(SYNTHETIC_OCR, 'caller')

    def test_replay_uses_same_validation_including_invalid_newest_capture(self):
        with TemporaryDirectory() as directory:
            # Define a genuinely older synthetic capture; do not rely on clock resolution.
            with patch('app.llm_extraction.datetime') as clock:
                clock.now.return_value = datetime(2000, 1, 1, tzinfo=timezone.utc)
                synthetic_extractor(directory).extract(SYNTHETIC_OCR, 'caller')
            with self.assertRaises(ExtractionError):
                synthetic_extractor(directory, synthetic_sdk(raw='invalid')).extract(SYNTHETIC_OCR, 'caller')
            with self.assertRaisesRegex(ExtractionError, 'JSON'):
                ReplayExtractor(directory, allow_synthetic=True).extract(SYNTHETIC_OCR, 'caller')

    def test_wire_schema_excludes_file_id_and_has_nullable_integer_cents(self):
        schema = response_schema()
        properties = schema['properties']['fields']['properties']
        self.assertNotIn('file_id', properties)
        self.assertEqual(properties['unit_price_cents']['type'], ['integer', 'null'])
        self.assertEqual(set(schema['properties']['fields']['required']), set(SYNTHETIC_FIELDS))

    def test_dotenv_does_not_override_environment(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / '.env').write_text('GEMINI_API_KEY=synthetic-dotenv-key\nGEMINI_MODEL=gemini-file-model\n')
            with patch('app.llm_extraction.ROOT', Path(directory)), \
                    patch.dict(os.environ, {'GEMINI_API_KEY': SYNTHETIC_KEY, 'GEMINI_MODEL': 'gemini-env-model'}):
                configuration, key = load_live_configuration()
                self.assertEqual(configuration.model, 'gemini-env-model')
                self.assertEqual(key, SYNTHETIC_KEY)
        with patch('dotenv.load_dotenv'), patch.dict(os.environ, {'GEMINI_API_KEY': ''}):
            with self.assertRaisesRegex(ExtractionError, 'not configured'):
                load_live_configuration()

    def test_sdk_timeout_single_attempt_and_only_ocr_prompt_are_sent(self):
        extractor = GeminiExtractor('unused', RequestConfiguration('gemini-synthetic-test'), SYNTHETIC_KEY, origin='synthetic_test')
        with patch('google.genai.Client') as client:
            prompt, schema, _ = extractor.configuration.bundle()
            extractor._request(SYNTHETIC_OCR, prompt, schema)
            options = client.call_args.kwargs['http_options']
            self.assertEqual(options.timeout, 30000)
            self.assertEqual(options.retry_options.attempts, 1)
            request = client.return_value.models.generate_content.call_args.kwargs
            self.assertEqual(request['contents'], SYNTHETIC_OCR)
            self.assertEqual(request['model'], 'gemini-synthetic-test')
            self.assertEqual(request['config'].system_instruction, prompt)
            self.assertEqual(request['config'].response_json_schema, schema)

    def test_bounded_transient_retry_and_retry_after(self):
        response = SimpleNamespace(headers={'Retry-After': '2'})
        transient = errors.ClientError(429, {'error': {'message': SYNTHETIC_KEY, 'status': 'RESOURCE_EXHAUSTED'}}, response)
        extractor = GeminiExtractor('unused', RequestConfiguration('gemini-synthetic-test'), SYNTHETIC_KEY, origin='synthetic_test')
        extractor._client = Mock()
        extractor._client.models.generate_content.side_effect = transient
        prompt, schema, _ = extractor.configuration.bundle()
        with patch('app.llm_extraction.time.sleep') as sleep:
            with self.assertRaises(ExtractionError) as caught:
                extractor._request(SYNTHETIC_OCR, prompt, schema)
        self.assertEqual(extractor._client.models.generate_content.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 2])
        self.assertIn('HTTP 429', str(caught.exception))
        self.assertNotIn(SYNTHETIC_KEY, str(caught.exception))

    def test_nontransient_failure_no_retry_and_provider_guidance_over_limit(self):
        for code, headers in ((403, {}), (429, {'Retry-After': '120'})):
            extractor = GeminiExtractor('unused', RequestConfiguration('gemini-synthetic-test'), SYNTHETIC_KEY, origin='synthetic_test')
            extractor._client = Mock()
            extractor._client.models.generate_content.side_effect = errors.ClientError(
                code, {'error': {'message': SYNTHETIC_KEY}}, SimpleNamespace(headers=headers))
            with patch('app.llm_extraction.time.sleep') as sleep, self.assertRaises(ExtractionError):
                extractor._request(SYNTHETIC_OCR, *extractor.configuration.bundle()[:2])
            sleep.assert_not_called()
            self.assertEqual(extractor._client.models.generate_content.call_count, 1)

    def test_request_failure_saved_without_exception_details(self):
        with TemporaryDirectory() as directory:
            extractor = synthetic_extractor(directory)
            extractor._request.side_effect = ExtractionError('Gemini request failed (HTTP 403: access denied)', {'attempts': 1})
            with self.assertRaises(ExtractionError):
                extractor.extract(SYNTHETIC_OCR, 'caller')
            failures = list(Path(directory).rglob('*.failure.json'))
            self.assertEqual(len(failures), 1)
            self.assertNotIn(SYNTHETIC_KEY, failures[0].read_text())
            self.assertEqual(list(Path(directory).rglob('*.capture.json')), [])

    def test_credential_echo_is_not_saved(self):
        with TemporaryDirectory() as directory:
            extractor = synthetic_extractor(directory, synthetic_sdk(raw=SYNTHETIC_KEY))
            with self.assertRaisesRegex(ExtractionError, 'credential material'):
                extractor.extract(SYNTHETIC_OCR, 'caller')
            self.assertEqual(list(Path(directory).rglob('*.capture.json')), [])

    def test_sdk_initialization_errors_are_sanitized(self):
        extractor = GeminiExtractor('unused', RequestConfiguration('gemini-synthetic-test'), SYNTHETIC_KEY, origin='synthetic_test')
        with patch('google.genai.Client', side_effect=RuntimeError(SYNTHETIC_KEY)):
            with self.assertRaises(ExtractionError) as caught:
                extractor._request(SYNTHETIC_OCR, *extractor.configuration.bundle()[:2])
        self.assertNotIn(SYNTHETIC_KEY, str(caught.exception))
        self.assertIn('initialization failed', str(caught.exception))

    def test_input_credential_material_is_neither_sent_nor_saved(self):
        with TemporaryDirectory() as directory:
            extractor = synthetic_extractor(directory)
            with self.assertRaisesRegex(ExtractionError, 'credential material'):
                extractor.extract(SYNTHETIC_OCR + SYNTHETIC_KEY, 'caller')
            extractor._request.assert_not_called()
            self.assertEqual(list(Path(directory).rglob('*.json')), [])

    def test_llm_batch_continues_after_failure_without_parser_fallback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / 'input'
            inputs.mkdir()
            (inputs / 'a.md').write_text(SYNTHETIC_OCR)
            (inputs / 'b.md').write_text(SYNTHETIC_OCR)
            extractor = synthetic_extractor(root / 'captures')
            result = extractor.extract(SYNTHETIC_OCR, 'b')
            extractor.extract = Mock(side_effect=[ExtractionError('Synthetic test processing failure'), result])
            output, errors_output = StringIO(), StringIO()
            with patch('scripts.run_extraction.GeminiExtractor', return_value=extractor), \
                    patch('scripts.run_extraction.extract_invoice_fields', side_effect=AssertionError('parser fallback forbidden')), \
                    patch('sys.stdout', output), patch('sys.stderr', errors_output):
                exit_code = main(['--mode', 'llm', '--input-dir', str(inputs), '--output-dir', str(root / 'llm'),
                                  '--replay-dir', str(root / 'captures')])
            self.assertEqual(exit_code, 1)
            summary = json.loads((root / 'llm/summary.json').read_text())
            self.assertEqual([r['status'] for r in summary['files']], ['error', 'success'])
            self.assertNotIn(SYNTHETIC_KEY, output.getvalue())
            self.assertEqual(errors_output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
