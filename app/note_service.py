"""Immutable note attempts; freshness is derived from the current verified context."""
import json
from app.discrepancy_notes import build_context
from app.invoice_service import now_utc, reject_credentials
from app.llm_extraction import json_hash
from app.storage import encode

MESSAGES = {'not_needed': 'No discrepancy note needed',
            'requires_reconciliation': 'A verified reconciliation result is required before drafting a note.',
            'unavailable': 'No compatible saved note is available. Generate a draft explicitly to create one.',
            'failed': 'The note could not be generated or validated. Check Gemini access/configuration or try again.',
            'outdated': 'This draft is outdated because the verified invoice context changed. Generate or replay a compatible note.',
            'current': 'AI-generated draft — review before use'}


class NoteService:
    def __init__(self, invoice_service):
        self.invoices = invoice_service
        self.store = invoice_service.store

    def context(self, invoice_id):
        row = self.invoices.get_invoice(invoice_id)
        earlier = [self.invoices.get_invoice(r['id']) for r in self.store.rows(row['dataset_id']) if r['ordinal'] < row['ordinal']]
        return build_context(row, earlier)

    def snapshot(self, invoice_id):
        self.store.connection.execute('BEGIN')  # Consistent read, closed before network.
        try:
            return self.context(invoice_id)
        finally:
            self.store.connection.execute('COMMIT')

    def get(self, invoice_id):
        self.store.connection.execute('BEGIN')
        try:
            context = self.context(invoice_id)
            context_hash = json_hash(context)
            rows = self.store.connection.execute('SELECT * FROM discrepancy_notes WHERE invoice_id=? ORDER BY id DESC', (invoice_id,)).fetchall()
            history = []
            for row in rows:
                item = dict(row)
                for key in ('context', 'draft', 'metadata'):
                    item[key] = json.loads(item[key]) if item[key] is not None else None
                item['state'] = ('current' if row['state'] == 'ready' else row['state']) if row['context_hash'] == context_hash else 'outdated'
                history.append(item)
            if context['status'] == 'reconciled':
                state = 'not_needed'
            elif context['status'] is None:
                state = 'requires_reconciliation'
            else:
                state = history[0]['state'] if history else 'unavailable'
            latest = history[0] if history else None
            return {'state': state, 'message': MESSAGES[state], 'context_hash': context_hash,
                    'draft': latest['draft'] if latest and state == 'current' else None,
                    'latest': latest, 'history': history}
        finally:
            self.store.connection.execute('COMMIT')

    def save(self, invoice_id, context, mode, state, draft, metadata):
        reject_credentials([context, draft, metadata])
        with self.store.transaction() as db:
            current_hash = json_hash(self.context(invoice_id))
            original_hash = json_hash(context)
            db.execute('INSERT INTO discrepancy_notes(invoice_id,context_hash,context,mode,state,draft,metadata,created_at) VALUES(?,?,?,?,?,?,?,?)',
                       (invoice_id, original_hash, encode(context), mode, state,
                        encode(draft) if draft is not None else None, encode(metadata), now_utc()))
        result = self.get(invoice_id)
        if current_hash != original_hash:
            result['generation_context_changed'] = True
        return result
