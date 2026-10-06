"""Verified context, Gemini discrepancy drafts, and separate keyless replay.

Schema/reference checks cannot establish factual accuracy of free-form prose.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import re
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from app.invoice_service import reject_credentials
from app.llm_extraction import (ROOT, RequestConfiguration, GeminiExtractor, ExtractionError,
    json_hash, text_hash, _response_text, _unique_json, _reject_constant)

PROMPT_VERSION = 'discrepancy-note-v1'
SCHEMA_VERSION = 'discrepancy-note-response-v1'
CONTEXT_VERSION = 'verified-invoice-note-v1'


def usd(cents):
    if cents is None:
        return None
    if type(cents) is not int:
        raise ValueError('Verified amounts must be integer cents')
    whole, fraction = divmod(abs(cents), 100)
    return f"{'-' if cents < 0 else ''}${whole:,}.{fraction:02d}"


def provenance(row):
    history = row.get('correction_history') or []
    return {key: history[-1][key] for key in ('timestamp', 'changes', 'reason')} if history else None


def build_context(row, earlier_rows=()):
    """Only confirmed references are sources; candidates are explicitly unconfirmed."""
    fields = row.get('current_fields') or {}
    result = row.get('reconciliation') or {}
    codes = result.get('findings', [])
    references = row.get('supporting_references') or {}
    pos, receipts = references.get('purchase_orders', []), references.get('receipts', [])
    sources = [{'label': 'Invoice ' + (fields.get('invoice_number') or '(number unavailable)'), 'type': 'invoice',
                'identifiers': {'invoice_number': fields.get('invoice_number'), 'supplier_id': fields.get('supplier_id')},
                'fields': {k: v for k, v in fields.items() if k != 'file_id'}, 'correction_provenance': provenance(row)}]
    confirmed_po = (len(pos) == 1 and pos[0].get('po_id') == fields.get('po_id')
                    and pos[0].get('supplier_id') == fields.get('supplier_id')
                    and not any(code in codes for code in ('ambiguous_po_reference', 'supplier_reference_conflict', 'unknown_po_reference', 'missing_po_reference')))
    confirmed_receipt = (confirmed_po and len(receipts) == 1
                         and receipts[0].get('po_id') == fields.get('po_id')
                         and receipts[0].get('supplier_id', fields.get('supplier_id')) == fields.get('supplier_id')
                         and not any(code in codes for code in ('ambiguous_receipt_reference', 'ambiguous_receipt_supplier', 'missing_receipt_reference')))
    if confirmed_po:
        sources.append({'label': 'PO ' + str(pos[0]['po_id']), 'type': 'purchase_order',
                        'identifiers': {k: pos[0].get(k) for k in ('po_id', 'supplier_id')}, 'record': pos[0]})
    if confirmed_receipt:
        receipt = receipts[0]
        sources.append({'label': 'Receipt ' + (str(receipt['receipt_id']) if receipt.get('receipt_id') else '(identifier unavailable)'), 'type': 'receipt',
                        'identifiers': {k: receipt.get(k) for k in ('receipt_id', 'po_id', 'supplier_id')}, 'record': receipt})
    earlier_identity = None
    if result.get('status') == 'duplicate':
        for previous in earlier_rows:
            prior = previous.get('current_fields') or {}
            if (prior.get('supplier_id'), prior.get('invoice_number')) == (fields.get('supplier_id'), fields.get('invoice_number')) and fields.get('supplier_id') and fields.get('invoice_number'):
                earlier_identity = {'invoice_number': prior['invoice_number'], 'supplier_id': prior['supplier_id'],
                                    'source_file_id': previous['file_id'], 'dataset_order': previous['ordinal']}
                sources.append({'label': 'Earlier invoice ' + prior['invoice_number'], 'type': 'earlier_invoice',
                                'identifiers': earlier_identity, 'correction_provenance': provenance(previous)})
                break
    amounts = {'result.' + name: {'cents': result.get(name), 'usd': usd(result.get(name))}
               for name in ('billed_cents', 'expected_cents', 'difference_cents')}
    for index, source in enumerate(sources):
        for key, value in source.get('fields', source.get('record', {})).items():
            if key.endswith('_cents'):
                amounts[f'source.{index}.{key}'] = {'cents': value, 'usd': usd(value)}
    def candidates(records):
        return [{key: record.get(key) for key in ('po_id', 'supplier_id', 'receipt_id', 'sku') if key in record} for record in records]
    context = {'context_version': CONTEXT_VERSION, 'status': result.get('status'), 'findings': codes,
               'sources': sources, 'amounts': amounts, 'earlier_invoice_identity': earlier_identity,
               'unconfirmed_candidates': {'purchase_orders': [] if confirmed_po else candidates(pos),
                                          'receipts': [] if confirmed_receipt else candidates(receipts)},
               'uncertainty': 'Unconfirmed candidates are not selected references. Null means unknown; never fill it.',
               'overcharge_excluded': result.get('status') in ('duplicate', 'unresolved')}
    reject_credentials(context)
    return context


class AmountReference(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    key: str
    cents: int


class NoteDraft(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    note: str = Field(min_length=1, max_length=1600)
    source_references: list[str] = Field(min_length=1)
    finding_references: list[str]
    amount_references: list[AmountReference]


def response_schema():
    return {'type': 'object', 'additionalProperties': False,
            'required': ['note', 'source_references', 'finding_references', 'amount_references'],
            'properties': {'note': {'type': 'string'},
                'source_references': {'type': 'array', 'items': {'type': 'string'}},
                'finding_references': {'type': 'array', 'items': {'type': 'string'}},
                'amount_references': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
                    'required': ['key', 'cents'], 'properties': {'key': {'type': 'string'}, 'cents': {'type': 'integer'}}}}}}


@dataclass(frozen=True)
class NoteConfiguration(RequestConfiguration):
    def bundle(self):
        prompt = (ROOT / 'prompts/discrepancy_note_v1.txt').read_text(encoding='utf-8')
        schema = response_schema()
        _, _, configuration = super().bundle()
        configuration.update(prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
                             prompt_sha256=text_hash(prompt), schema_sha256=json_hash(schema),
                             purpose='discrepancy-note', context_version=CONTEXT_VERSION)
        return prompt, schema, configuration


class NoteUnavailable(ExtractionError):
    pass


def process_note(raw, sdk, context):
    feedback = sdk.get('prompt_feedback') or {}
    candidates = sdk.get('candidates') or []
    if feedback.get('block_reason') not in (None, 'BLOCK_REASON_UNSPECIFIED') or len(candidates) != 1 or candidates[0].get('finish_reason') != 'STOP' or not raw.strip():
        raise ExtractionError('Gemini note was empty, blocked, or incomplete')
    try:
        draft = NoteDraft.model_validate(json.loads(raw, object_pairs_hook=_unique_json, parse_constant=_reject_constant))
        reject_credentials(draft.model_dump())
    except (ValueError, TypeError, ValidationError):
        raise ExtractionError('Gemini note failed local schema validation') from None
    labels = {source['label'] for source in context['sources']}
    if (not draft.note.strip() or len(set(draft.source_references)) != len(draft.source_references)
            or not set(draft.source_references) <= labels
            or any(label not in draft.note for label in draft.source_references)):
        raise ExtractionError('Gemini note contains invalid or uncited source references')
    if not draft.finding_references or not set(draft.finding_references) <= set(context['findings']):
        raise ExtractionError('Gemini note cites unsupported findings')
    mentioned = set()
    for reference in draft.amount_references:
        amount = context['amounts'].get(reference.key)
        if amount is None or amount['cents'] is None or reference.cents != amount['cents'] or amount['usd'] not in draft.note:
            raise ExtractionError('Gemini note cites unsupported amounts')
        mentioned.add(amount['usd'])
    # All explicit dollar literals need a structured reference too. Not a proof of prose accuracy.
    if any(amount not in mentioned for amount in re.findall(r'-?\$[0-9][0-9,]*(?:\.[0-9]+)?', draft.note)):
        raise ExtractionError('Gemini note contains an unsupported USD amount')
    return draft.model_dump()


def _write(directory, record, suffix):
    folder = Path(directory) / record['context_sha256']
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid4().hex + suffix)
    record['capture_sha256'] = json_hash(record)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    return path.relative_to(directory).as_posix()


def metadata(record, capture):
    sdk = record.get('sdk_response') or {}
    candidates = sdk.get('candidates') or []
    return {'capture': capture, 'origin': record['origin'], 'context_sha256': record['context_sha256'],
            'configuration': record['configuration'], 'configuration_fingerprint': record['configuration_fingerprint'],
            'returned_model': sdk.get('model_version'), 'usage': sdk.get('usage_metadata'),
            'finish_reason': candidates[0].get('finish_reason') if candidates else None,
            'sdk_version': record['sdk_version'], 'timestamp': record['timestamp'], 'attempts': record.get('attempts')}


class GeminiNotes(GeminiExtractor):
    """Reuse SDK configuration, request, timeout and retries, with separate evidence."""
    def __init__(self, capture_dir, configuration=None, api_key=None, *, origin='real_gemini'):
        super().__init__(capture_dir, configuration, api_key, origin=origin)
        self.configuration = NoteConfiguration(self.configuration.model, self.configuration.timeout_ms, self.configuration.max_attempts)

    def generate(self, context):
        prompt, schema, config = self.configuration.bundle()
        record = {'capture_version': 1, 'purpose': 'discrepancy-note', 'origin': self.origin,
                  'timestamp': datetime.now(timezone.utc).isoformat(), 'context': context,
                  'context_sha256': json_hash(context), 'prompt': prompt, 'schema': schema,
                  'configuration': config, 'configuration_fingerprint': json_hash(config),
                  'sdk_version': version('google-genai')}
        reject_credentials(record)
        if self._api_key and self._api_key in json.dumps(record, ensure_ascii=False):
            raise ExtractionError('Note input contains credential material')
        try:
            response, attempts = self._request(json.dumps(context, sort_keys=True, ensure_ascii=False), prompt, schema)
        except ExtractionError as exc:
            record.update(error=str(exc), attempts=exc.metadata.get('attempts'))
            capture = _write(self.capture_dir, record, '.failure.json')
            raise ExtractionError('Gemini note request failed', metadata(record, capture)) from None
        sdk = response.model_dump(mode='json', exclude={'sdk_http_response'})
        raw = _response_text(sdk)
        record.update(sdk_response=sdk, original_response_text=raw, response_sha256=text_hash(raw),
                      sdk_response_sha256=json_hash(sdk), attempts=attempts)
        if self._api_key and self._api_key in json.dumps(record, ensure_ascii=False):
            raise ExtractionError('Note response contains credential material; capture suppressed')
        reject_credentials(record)
        capture = _write(self.capture_dir, record, '.capture.json')  # Before validation.
        meta = metadata(record, capture)
        try:
            draft = process_note(raw, sdk, context)
        except ExtractionError as exc:
            raise ExtractionError(str(exc), meta) from None
        return draft, meta


class ReplayNotes:
    """No dotenv, credentials, SDK client, OCR, or network construction."""
    def __init__(self, capture_dir, model=None, *, allow_synthetic=False):
        self.capture_dir, self.model, self.allow_synthetic = Path(capture_dir), model, allow_synthetic

    def generate(self, context):
        context_hash = json_hash(context)
        paths = sorted((self.capture_dir / context_hash).glob('*.capture.json'), reverse=True)
        for path in paths:
            try:
                record = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_json, parse_constant=_reject_constant)
                if not isinstance(record, dict) or record.get('capture_sha256') != json_hash({key: value for key, value in record.items() if key != 'capture_sha256'}):
                    raise ExtractionError('Saved note capture integrity mismatch')
                reject_credentials(record)
                if record['purpose'] != 'discrepancy-note' or record['capture_version'] != 1 or record['context_sha256'] != context_hash or json_hash(record['context']) != context_hash or record['context'] != context:
                    raise ExtractionError('Saved note context integrity mismatch')
                if record['origin'] != 'real_gemini' and not (self.allow_synthetic and record['origin'] == 'synthetic_test'):
                    raise ExtractionError('Saved note requires a real Gemini capture')
                saved = record['configuration']
                if record['configuration_fingerprint'] != json_hash(saved):
                    raise ExtractionError('Saved note configuration integrity mismatch')
                if self.model is not None and saved['requested_model'] != self.model:
                    continue
                prompt, schema, current = NoteConfiguration(saved['requested_model']).bundle()
                if saved != current or record['prompt'] != prompt or record['schema'] != schema:
                    continue
                raw, sdk = record['original_response_text'], record['sdk_response']
                if record['response_sha256'] != text_hash(raw) or record['sdk_response_sha256'] != json_hash(sdk) or raw != _response_text(sdk):
                    raise ExtractionError('Saved note response integrity mismatch')
                return process_note(raw, sdk, context), metadata(record, path.relative_to(self.capture_dir).as_posix())
            except (ValueError, KeyError, TypeError, OSError):
                raise ExtractionError('Saved note capture is malformed or unreadable') from None
        raise NoteUnavailable('No compatible saved note exists for the current verified context')
