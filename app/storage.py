"""Small SQLite store. Original evidence/reference snapshots and history are immutable."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

DEFAULT_DB = Path(__file__).resolve().parents[1] / 'runtime/invoices.sqlite3'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class InvoiceStore:
    def __init__(self, path=DEFAULT_DB):
        self.path = str(path)
        if self.path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys = ON')
        version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2):
            self.close()
            raise ValueError('Unsupported invoice database schema version')
        self.connection.executescript('''
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS datasets (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                mode TEXT NOT NULL CHECK(mode IN ('parser','llm','replay')),
                fingerprint TEXT NOT NULL, source TEXT NOT NULL,
                purchase_orders TEXT NOT NULL, receipts TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invoices (
                id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
                file_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                original_evidence TEXT NOT NULL,
                processing_status TEXT NOT NULL CHECK(processing_status IN ('success','issues','error')),
                current_fields TEXT, current_issues TEXT NOT NULL,
                reconciliation TEXT, reconciliation_status TEXT,
                review_state TEXT NOT NULL CHECK(review_state IN ('unreviewed','corrected')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(dataset_id,file_id), UNIQUE(dataset_id,ordinal)
            );
            CREATE TABLE IF NOT EXISTS corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id TEXT NOT NULL REFERENCES invoices(id),
                changes TEXT NOT NULL, reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
                created_at TEXT NOT NULL, outcome TEXT NOT NULL,
                dataset_outcomes TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS immutable_evidence
                BEFORE UPDATE OF id,dataset_id,file_id,ordinal,original_evidence,processing_status,created_at ON invoices
                BEGIN SELECT RAISE(ABORT,'Original invoice evidence is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_invoice_delete BEFORE DELETE ON invoices
                BEGIN SELECT RAISE(ABORT,'Invoice evidence cannot be deleted'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_dataset BEFORE UPDATE ON datasets
                BEGIN SELECT RAISE(ABORT,'Reference snapshots are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_dataset_delete BEFORE DELETE ON datasets
                BEGIN SELECT RAISE(ABORT,'Reference snapshots cannot be deleted'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_history_update BEFORE UPDATE ON corrections
                BEGIN SELECT RAISE(ABORT,'Correction history is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_history_delete BEFORE DELETE ON corrections
                BEGIN SELECT RAISE(ABORT,'Correction history is immutable'); END;
            CREATE TABLE IF NOT EXISTS discrepancy_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id TEXT NOT NULL REFERENCES invoices(id),
                context_hash TEXT NOT NULL, context TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('llm','replay')),
                state TEXT NOT NULL CHECK(state IN ('ready','failed','unavailable')),
                draft TEXT, metadata TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS notes_by_invoice ON discrepancy_notes(invoice_id,id);
            CREATE TRIGGER IF NOT EXISTS immutable_notes_update BEFORE UPDATE ON discrepancy_notes
                BEGIN SELECT RAISE(ABORT,'Note history is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_notes_delete BEFORE DELETE ON discrepancy_notes
                BEGIN SELECT RAISE(ABORT,'Note history cannot be deleted'); END;
            PRAGMA user_version = 2;
            COMMIT;
        ''')

    @contextmanager
    def transaction(self):
        """One writer transaction covering correction, all outcomes, and audit history."""
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield self.connection
            self.connection.execute('COMMIT')
        except BaseException:
            self.connection.execute('ROLLBACK')
            raise

    def dataset(self, name):
        row = self.connection.execute('SELECT * FROM datasets WHERE name = ?', (name,)).fetchone()
        if row is None:
            raise KeyError('Dataset not found')
        return row

    def invoice(self, invoice_id):
        row = self.connection.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
        if row is None:
            raise KeyError('Invoice not found')
        return row

    def rows(self, dataset_id, status=None):
        if status is None:
            return self.connection.execute('SELECT * FROM invoices WHERE dataset_id = ? ORDER BY ordinal',
                                           (dataset_id,)).fetchall()
        return self.connection.execute('''SELECT * FROM invoices WHERE dataset_id = ?
                                          AND reconciliation_status = ? ORDER BY ordinal''',
                                       (dataset_id, status)).fetchall()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
