"""Import saved extraction evidence and apply audited, transactional corrections.

Datasets are isolated evaluation runs, not a cross-run business invoice ledger.
Duplicate detection/totals always use one selected dataset in file_id order.
This module never invokes extraction, OCR, Gemini, or loads credentials.
"""
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError

from app.reconciliation import reconcile_invoices, overcharge_total
from app.schemas import InvoiceFields, ExtractionIssue
from app.storage import encode

ROOT = Path(__file__).resolve().parents[1]
EDITABLE = set(InvoiceFields.model_fields) - {'file_id'}
STATUSES = ('reconciled', 'discrepant', 'duplicate', 'unresolved')
SECRET_KEYS = {'api_key', 'gemini_api_key', 'google_api_key', 'authorization',
               'access_token', 'refresh_token', 'password', 'client_secret'}


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return sha256(value).hexdigest()


def reject_credentials(value):
    """Refuse credential keys/common Gemini key material; never read .env."""
    if isinstance(value, dict):
        if any(str(key).lower() in SECRET_KEYS for key in value):
            raise ValueError('Credential-bearing input cannot be stored')
        for item in value.values():
            reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            reject_credentials(item)
    elif isinstance(value, str) and re.search(r'AIza[A-Za-z0-9_-]{35}', value):
        raise ValueError('Credential-bearing input cannot be stored')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def asset_path(directory, relative):
    if not isinstance(relative, str):
        return None
    root = Path(directory).resolve()
    path = (root / relative).resolve()
    return path if path.is_relative_to(root) else None


def source_evidence(record, ocr_dir, image_dir, capture_dir):
    """Snapshot available artifacts; absence/mismatch is recorded, never repaired."""
    notes = []
    metadata = record.get('metadata') or {}
    capture_ref = metadata.get('capture') or metadata.get('failure_record')
    capture = {'reference': capture_ref, 'available': False, 'file_sha256': None}
    captured_ocr = None
    if capture_ref:
        path = asset_path(capture_dir, capture_ref)
        if path is not None and path.suffix == '.json' and path.is_file():
            data = path.read_bytes()
            capture.update(available=True, file_sha256=digest(data))
            content = json.loads(data)
            text = content.get('ocr_text')
            if isinstance(text, str) and digest(text.encode()) == content.get('ocr_sha256'):
                if metadata.get('ocr_sha256') in (None, content['ocr_sha256']):
                    captured_ocr = text
                else:
                    notes.append('Capture OCR hash conflicts with extraction metadata')
        else:
            notes.append('Referenced model capture is unavailable')
    source = record.get('source')
    candidate = Path(source) if isinstance(source, str) else None
    if candidate is None or not candidate.is_absolute() or not candidate.is_file():
        candidate = asset_path(ocr_dir, Path(source).name if isinstance(source, str) else record['file_id'] + '.md')
    ocr = {'reference': str(candidate) if candidate is not None else None,
           'text': None, 'sha256': None, 'file_sha256': None}
    if candidate is not None and candidate.suffix.lower() == '.md' and candidate.is_file():
        data = candidate.read_bytes()
        text = candidate.read_text(encoding='utf-8-sig')
        ocr['file_sha256'] = digest(data)
        expected = metadata.get('ocr_sha256')
        if expected is None or digest(text.encode()) == expected:
            ocr.update(text=text, sha256=digest(text.encode()))
        else:
            notes.append('OCR source hash mismatch; source text was not accepted')
    if ocr['text'] is None and captured_ocr is not None:
        ocr.update(text=captured_ocr, sha256=digest(captured_ocr.encode()))
        notes.append('OCR source unavailable/unverified; exact text preserved from referenced capture')
    if ocr['text'] is None:
        notes.append('OCR text is unavailable')
    path = asset_path(image_dir, record.get('image_reference', record['file_id'] + '.png'))
    image = {'reference': str(path) if path is not None else None, 'sha256': None, 'available': False}
    if path is not None and path.is_file():
        actual = digest(path.read_bytes())
        if record.get('image_sha256') in (None, actual):
            image.update(sha256=actual, available=True)
        else:
            notes.append('Source image hash mismatch')
    else:
        notes.append('Source image is unavailable')
    return {'ocr': ocr, 'image': image, 'capture': capture, 'availability_notes': notes}


class InvoiceService:
    def __init__(self, store):
        self.store = store

    def import_results(self, summary_path, references_path, *, dataset, mode,
                       ocr_dir=None, image_dir=None, capture_dir=None):
        if not isinstance(dataset, str) or not dataset.strip():
            raise ValueError('A nonempty dataset name is required')
        summary, reference_data = read_json(summary_path), read_json(references_path)
        if mode not in ('parser', 'llm', 'replay') or summary.get('mode') != mode:
            raise ValueError('Explicit extraction mode must match the saved summary')
        records = summary.get('files')
        if not isinstance(records, list) or not records:
            raise ValueError('Extraction summary must contain records; seed invoices are not extraction results')
        pos, receipts = reference_data.get('purchase_orders'), reference_data.get('receipts')
        if not all(isinstance(table, list) and all(isinstance(row, dict) for row in table) for table in (pos, receipts)):
            raise ValueError('Reference data must contain purchase_orders and receipts lists')
        # Only reference tables are retained; seed invoice fields are never used.
        reject_credentials([summary, pos, receipts, dataset])
        fingerprint = digest(encode({'summary': summary, 'purchase_orders': pos, 'receipts': receipts}).encode())
        with self.store.transaction() as db:
            existing = db.execute('SELECT * FROM datasets WHERE name = ?', (dataset,)).fetchone()
            if existing is not None:
                if existing['fingerprint'] != fingerprint:
                    raise ValueError('Dataset name already identifies different input; select a new run name')
                dataset_id, reimported = existing['id'], True
            else:
                for record in records:
                    if not isinstance(record, dict) or record.get('mode') != mode or record.get('status') not in ('success', 'issues', 'error'):
                        raise ValueError('Malformed or mixed-mode extraction record')
                    InvoiceFields(file_id=record.get('file_id'))
                records = sorted(records, key=lambda record: record['file_id'])
                if len({r['file_id'] for r in records}) != len(records):
                    raise ValueError('Repeated source file_id within a dataset')
                dataset_id, reimported, timestamp = str(uuid4()), False, now_utc()
                db.execute('''INSERT INTO datasets(id,name,mode,fingerprint,source,purchase_orders,receipts,created_at)
                              VALUES(?,?,?,?,?,?,?,?)''',
                           (dataset_id, dataset, mode, fingerprint, str(summary_path), encode(pos), encode(receipts), timestamp))
                for ordinal, record in enumerate(records):
                    fields, issues, processing = None, [], record['status']
                    import_problems = []
                    try:
                        issues = [ExtractionIssue.model_validate(issue).model_dump() for issue in record.get('issues', [])]
                        if processing != 'error' and record.get('fields') is not None:
                            fields = InvoiceFields.model_validate(record['fields']).model_dump()
                            if fields['file_id'] != record['file_id']:
                                raise ValueError('Source file_id conflicts with extracted file_id')
                        else:
                            processing = 'error'
                            import_problems.append('Extraction processing failed or fields are unavailable')
                    except (ValidationError, TypeError):
                        fields, issues, processing = None, [], 'error'
                        import_problems.append('Saved fields/issues failed strict schema validation')
                    evidence = {'record': record, 'dataset_provenance': summary.get('provenance'),
                                'import_problems': import_problems,
                                **source_evidence(record, ocr_dir or ROOT / 'runtime/ocr',
                                                  image_dir or ROOT / 'client-ai-starter-pack/tasks/invoices/images',
                                                  capture_dir or ROOT / 'runtime/extraction/captures')}
                    reject_credentials(evidence)
                    invoice_id = str(uuid5(UUID(dataset_id), record['file_id']))
                    db.execute('''INSERT INTO invoices(id,dataset_id,file_id,ordinal,original_evidence,processing_status,
                                  current_fields,current_issues,review_state,created_at,updated_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                               (invoice_id, dataset_id, record['file_id'], ordinal, encode(evidence), processing,
                                encode(fields) if fields is not None else None, encode(issues), 'unreviewed', timestamp, timestamp))
                self._recalculate(dataset_id, timestamp)
        return {'dataset': dataset, 'dataset_id': dataset_id, 'mode': mode, 'reimported': reimported,
                'invoice_ids': {row['file_id']: row['id'] for row in self.store.rows(dataset_id)},
                'summary': self.summary(dataset)}

    def _save_outcome(self, invoice_id, outcome, timestamp):
        self.store.connection.execute('''UPDATE invoices SET reconciliation=?,reconciliation_status=?,updated_at=?
                                         WHERE id=?''',
                                      (encode(outcome) if outcome is not None else None,
                                       outcome['status'] if outcome is not None else None, timestamp, invoice_id))

    def _recalculate(self, dataset_id, timestamp):
        db = self.store.connection
        dataset = db.execute('SELECT * FROM datasets WHERE id=?', (dataset_id,)).fetchone()
        rows = self.store.rows(dataset_id)
        eligible = [row for row in rows if row['current_fields'] is not None]
        invoices = [InvoiceFields.model_validate(json.loads(row['current_fields'])).to_reconciliation_dict() for row in eligible]
        batch = reconcile_invoices(invoices, json.loads(dataset['purchase_orders']), json.loads(dataset['receipts']))
        outcomes = {row['id']: outcome for row, outcome in zip(eligible, batch['results'])}
        for row in rows:
            self._save_outcome(row['id'], outcomes.get(row['id']), timestamp)
        return outcomes

    def list_invoices(self, dataset, status=None):
        if status is not None and status not in STATUSES:
            raise ValueError('Unknown reconciliation status filter')
        selected = self.store.dataset(dataset)
        return [self.get_invoice(row['id']) for row in self.store.rows(selected['id'], status)]

    def get_invoice(self, invoice_id):
        row = self.store.invoice(invoice_id)
        original = json.loads(row['original_evidence'])
        outcome = json.loads(row['reconciliation']) if row['reconciliation'] is not None else None
        dataset = self.store.connection.execute('SELECT * FROM datasets WHERE id=?', (row['dataset_id'],)).fetchone()
        history = self.store.connection.execute('SELECT * FROM corrections WHERE invoice_id=? ORDER BY id', (invoice_id,)).fetchall()
        return {'id': row['id'], 'dataset': dataset['name'], 'dataset_id': row['dataset_id'],
                'file_id': row['file_id'], 'ordinal': row['ordinal'], 'extraction_mode': dataset['mode'],
                'processing_status': row['processing_status'], 'original_evidence': original,
                'original_fields': original['record'].get('fields'), 'original_issues': original['record'].get('issues'),
                'current_fields': json.loads(row['current_fields']) if row['current_fields'] is not None else None,
                'current_issues': json.loads(row['current_issues']), 'reconciliation': outcome,
                'findings': outcome['findings'] if outcome is not None else [],
                'supporting_references': outcome['references'] if outcome is not None else None,
                'reference_tables': {'purchase_orders': json.loads(dataset['purchase_orders']), 'receipts': json.loads(dataset['receipts'])},
                'review_state': row['review_state'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
                'correction_history': [{'id': h['id'], 'changes': json.loads(h['changes']), 'reason': h['reason'],
                                        'timestamp': h['created_at'], 'outcome': json.loads(h['outcome']),
                                        'dataset_outcomes': json.loads(h['dataset_outcomes'])} for h in history]}

    def correct_invoice(self, invoice_id, patch, reason):
        if not isinstance(patch, dict) or not patch or set(patch) - EDITABLE:
            raise ValueError('A nonempty partial update of editable invoice fields is required')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('A nonempty correction reason is required')
        reject_credentials([patch, reason])
        with self.store.transaction() as db:
            row = self.store.invoice(invoice_id)
            if row['current_fields'] is None:
                raise ValueError('Processing-failed extraction has no fields to correct; import a valid extraction run')
            before = json.loads(row['current_fields'])
            try:
                after = InvoiceFields.model_validate({**before, **patch}).model_dump()
            except ValidationError:
                raise ValueError('Correction failed strict InvoiceFields validation') from None
            issues = [issue for issue in json.loads(row['current_issues'])
                      if issue['field'] not in patch or after[issue['field']] is None]
            for field in patch:
                if after[field] is None and not any(i['field'] == field and i['code'] == 'missing' for i in issues):
                    issues.append(ExtractionIssue(field=field, code='missing', message='Field explicitly cleared by reviewer').model_dump())
            timestamp = now_utc()
            db.execute('UPDATE invoices SET current_fields=?,current_issues=?,review_state=?,updated_at=? WHERE id=?',
                       (encode(after), encode(issues), 'corrected', timestamp, invoice_id))
            outcomes = self._recalculate(row['dataset_id'], timestamp)
            changes = {field: {'before': before[field], 'after': after[field]} for field in patch}
            db.execute('''INSERT INTO corrections(invoice_id,changes,reason,created_at,outcome,dataset_outcomes)
                          VALUES(?,?,?,?,?,?)''',
                       (invoice_id, encode(changes), reason.strip(), timestamp, encode(outcomes[invoice_id]), encode(outcomes)))
        return self.get_invoice(invoice_id)

    def summary(self, dataset):
        selected = self.store.dataset(dataset)
        rows = self.store.rows(selected['id'])
        outcomes = [json.loads(row['reconciliation']) for row in rows if row['reconciliation'] is not None]
        counts = Counter(outcome['status'] for outcome in outcomes)
        return {'dataset': dataset, 'dataset_id': selected['id'], 'mode': selected['mode'], 'total_invoices': len(rows),
                'counts_by_status': {status: counts[status] for status in STATUSES},
                'processing_errors': sum(row['processing_status'] == 'error' for row in rows),
                'current_extraction_issue_invoices': sum(bool(json.loads(row['current_issues'])) for row in rows),
                'overcharge_cents': overcharge_total(outcomes)}
