import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from app.invoice_service import InvoiceService
from app.storage import InvoiceStore

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / 'runtime/extraction/parser/summary.json'
REFERENCES = ROOT / 'client-ai-starter-pack/tasks/invoices/seed.json'


class StorageTests(unittest.TestCase):
    def test_close_reopen_preserves_evidence_current_history_and_outcome(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'nested/invoices.sqlite3'
            with InvoiceStore(path) as store:
                service = InvoiceService(store)
                imported = service.import_results(SUMMARY, REFERENCES, dataset='parser-run', mode='parser')
                invoice_id = imported['invoice_ids']['wrong-price']
                original = service.get_invoice(invoice_id)['original_evidence']
                corrected = service.correct_invoice(invoice_id, {'unit_price_cents': 2600, 'total_cents': 13000}, 'Synthetic restart test')
                self.assertEqual(corrected['reconciliation']['difference_cents'], 3000)
            with InvoiceStore(path) as store:
                restored = InvoiceService(store).get_invoice(invoice_id)
                self.assertEqual(restored, corrected)
                self.assertEqual(restored['original_evidence'], original)
                self.assertEqual(restored['current_fields']['unit_price_cents'], 2600)
                self.assertEqual(restored['correction_history'][0]['changes']['total_cents'], {'before': 12000, 'after': 13000})
                self.assertEqual(restored['review_state'], 'corrected')

    def test_database_guards_original_evidence_reference_snapshots_and_history(self):
        with InvoiceStore(':memory:') as store:
            service = InvoiceService(store)
            ids = service.import_results(SUMMARY, REFERENCES, dataset='immutable', mode='parser')['invoice_ids']
            service.correct_invoice(ids['clean'], {'quantity': 5}, 'Confirmed quantity')
            for statement, values in (
                ('UPDATE invoices SET original_evidence=? WHERE id=?', ('{}', ids['clean'])),
                ('UPDATE invoices SET file_id=? WHERE id=?', ('changed', ids['clean'])),
                ('UPDATE datasets SET purchase_orders=? WHERE name=?', ('[]', 'immutable')),
                ('UPDATE corrections SET reason=? WHERE invoice_id=?', ('changed', ids['clean'])),
                ('DELETE FROM corrections WHERE invoice_id=?', (ids['clean'],)),
            ):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    with store.transaction() as db:
                        db.execute(statement, values)
            self.assertEqual(service.get_invoice(ids['clean'])['file_id'], 'clean')
            self.assertEqual(len(service.get_invoice(ids['clean'])['correction_history']), 1)

    def test_explicit_transaction_rolls_back_and_foreign_keys_are_enabled(self):
        with InvoiceStore(':memory:') as store:
            self.assertEqual(store.connection.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            with self.assertRaises(RuntimeError):
                with store.transaction() as db:
                    db.execute('''INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?)''',
                               ('id', 'name', 'parser', 'fingerprint', 'source', '[]', '[]', 'timestamp'))
                    raise RuntimeError('Synthetic rollback')
            self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM datasets').fetchone()[0], 0)

    def test_parameterized_dataset_name_preserves_sql_like_text(self):
        with InvoiceStore(':memory:') as store:
            name = "sample'); DROP TABLE invoices; --"
            service = InvoiceService(store)
            service.import_results(SUMMARY, REFERENCES, dataset=name, mode='parser')
            self.assertEqual(service.summary(name)['total_invoices'], 4)


if __name__ == '__main__':
    unittest.main()
