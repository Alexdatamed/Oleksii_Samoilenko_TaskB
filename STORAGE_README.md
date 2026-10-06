# SQLite persistence and manual invoice corrections

This stage is offline. It imports saved extraction summaries and uses the
existing reconciliation code. It never invokes OCR, parser extraction, Gemini,
or dotenv. Python's built-in sqlite3 supplies persistence; no new package is
required beyond the project's existing Pydantic dependency.

## PowerShell commands (project root, existing .venv)

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/run_invoices.py init
.\.venv\Scripts\python.exe scripts/run_invoices.py import --dataset parser-v1 --mode parser --summary runtime/extraction/parser/summary.json --references client-ai-starter-pack/tasks/invoices/seed.json
.\.venv\Scripts\python.exe scripts/run_invoices.py list --dataset parser-v1
.\.venv\Scripts\python.exe scripts/run_invoices.py list --dataset parser-v1 --status unresolved
.\.venv\Scripts\python.exe scripts/run_invoices.py summary --dataset parser-v1
```

The default database is project-root `runtime/invoices.sqlite3`. Use `--db`
before the subcommand for a different path:

```powershell
.\.venv\Scripts\python.exe scripts/run_invoices.py --db runtime/other.sqlite3 init
```

Select saved LLM or replay data explicitly, without calling their extractors:

```powershell
.\.venv\Scripts\python.exe scripts/run_invoices.py import --dataset llm-v1 --mode llm --summary runtime/extraction/llm/summary.json --references client-ai-starter-pack/tasks/invoices/seed.json
.\.venv\Scripts\python.exe scripts/run_invoices.py import --dataset replay-v1 --mode replay --summary runtime/extraction/replay/summary.json --references client-ai-starter-pack/tasks/invoices/seed.json
```

Import also accepts `--ocr-dir`, `--image-dir`, and `--capture-dir` to locate
local evidence. Defaults are `runtime/ocr`, the supplied invoice image folder,
and `runtime/extraction/captures`. No remote artifacts are fetched.

Get an internal invoice ID from import output or the list command:

```powershell
$rows = .\.venv\Scripts\python.exe scripts/run_invoices.py list --dataset parser-v1 | ConvertFrom-Json
$invoiceId = ($rows | Where-Object { $_.file_id -eq 'wrong-price' }).id
.\.venv\Scripts\python.exe scripts/run_invoices.py show --id $invoiceId
```

Corrections use canonical field names and a JSON patch file. The following
explicitly confirms the two values already written on this invoice:

```powershell
'{"unit_price_cents":2400,"total_cents":12000}' | Set-Content -Encoding utf8 runtime/reviewer-patch.json
.\.venv\Scripts\python.exe scripts/run_invoices.py correct --id $invoiceId --patch runtime/reviewer-patch.json --reason 'Confirmed unit price and total against invoice text'
.\.venv\Scripts\python.exe scripts/run_invoices.py show --id $invoiceId
.\.venv\Scripts\python.exe scripts/run_invoices.py summary --dataset parser-v1
```

An explicit JSON null clears a field (for example `{"po_id":null}`). Omitted
fields remain unchanged. Repeating a known value is an explicit reviewer
confirmation and is still audited. The CLI returns 0 for a successful database
operation and 1 for an invalid/failed operation. Successfully importing a run
containing extraction failures returns 0 while reporting those failures in
`processing_errors`; it does not claim those invoices reconciled. Unresolved
business results or extraction issues are visible in the saved data/summary.

## Service/storage contract

`InvoiceStore(path)` is a context manager. `InvoiceService(store)` provides:

- `import_results(summary_path, references_path, dataset=..., mode=..., ...)`
- `list_invoices(dataset, status=None)`
- `get_invoice(internal_invoice_id)`
- `correct_invoice(internal_invoice_id, partial_patch, nonempty_reason)`
- `summary(dataset)`

Each selected run has a unique human-readable dataset name and a stored UUID.
Internal invoice UUIDs derive from that dataset UUID plus immutable source
file_id. They remain stable across process restarts/reimports and are distinct
from source file_id and business invoice_number. Alternative extraction runs
are isolated datasets: their records never become duplicates merely because
another run already contains the same source. Summaries are per selected run;
there is intentionally no global sum across alternative runs.

The same dataset name and exact canonical import fingerprint (saved summary and
PO/receipt snapshots) are idempotent, leaving reviewer corrections and evidence
untouched. Reusing a name with changed inputs is rejected; choose a new run
name. Source artifact availability is snapshotted at the initial import and is
not refreshed on reimport. This avoids replacing original evidence after review.

Processing order is ascending case-sensitive file_id, frozen as an ordinal in
SQLite. It is independent of incoming summary array order and puts `clean`
before `duplicate` for the supplied files. Every recalculation starts with an
empty processed-identity set and passes the entire eligible dataset through
`reconcile_invoices` using `InvoiceFields.to_reconciliation_dict()`. An invoice
cannot compare against itself. Supplier/invoice-number changes can therefore
update duplicate classification for later records.

SQLite stores immutable original extraction records (including fields/issues,
source identity, mode, processing error, provenance, and capture metadata), OCR
text/hash, image reference/hash, capture reference/file hash, full PO and receipt
snapshots, current fields/issues, current reconciliation JSON/status, review
state, and UTC timestamps. Retrieval includes findings, matched supporting
records, full reference snapshots, and correction history. Source images and raw
Gemini response files remain external; the database preserves references/hashes,
not copies of their binary/full response contents.

Missing images, OCR text, or referenced captures are explicitly listed in
`original_evidence.availability_notes`. They are never reconstructed from seed
invoice fields or other references. If local OCR text fails its recorded hash,
it is not accepted. A valid referenced capture can supply its exact original
OCR text when the Markdown is unavailable. Capture lookup cannot escape its
configured directory. The source file ID is never guessed from invoice text.

Only `purchase_orders` and `receipts` are used from the reference input, even
when that file also contains seed invoices. Extraction fields come exclusively
from saved results. Strict schema failures in saved fields/issues remain visible
as processing errors with original invalid input retained. They get no business
reconciliation result and do not participate in duplicate detection or totals.
Structural import errors (mixed modes, conflicting/repeated file IDs, malformed
summaries) roll back the whole import. Original processing failures are also
retained separately from reconciliation statuses.

Corrections accept only the seven extracted fields other than file_id. Internal
IDs, file_id, extraction mode, source evidence, timestamps, and review state are
not editable through that operation. Merged fields use the existing strict
Pydantic schema, including nullable fields and integer cents. Zero and negative
values pass through to the existing business rules. No invoice total is derived
from quantity and unit price, and no discrepancy is silently repaired.

Current issues are removed only for explicitly corrected fields set to known
values, including zero/negative integers. Unrelated issues and all original
issues remain. Clearing a field retains existing issues and ensures a visible
missing issue. Review state becomes `corrected`, never automatically approved;
approval is outside this stage. Processing-failed records without validated
fields cannot be manually filled through this operation: import a valid saved
extraction as a new run instead.

Every correction uses one `BEGIN IMMEDIATE` transaction covering its current
fields/issues, review state, the whole dataset's recalculated outcomes, and an
append-only history event. History records explicit before/after values, reason,
UTC timestamp, corrected invoice outcome, and all calculated dataset outcomes.
Any calculation/persistence failure rolls everything back. SQL values are
parameterized, foreign keys are enabled, and SQLite triggers reject changes to
original evidence, reference snapshots, or existing history. Summary counts and
positive overcharges use current persisted outcomes and the existing duplicate/
unresolved exclusions. Credentials are never loaded; credential-bearing keys
and recognizable Gemini key material are rejected rather than copied into SQL.

## Separately labeled synthetic correction demonstration

`runtime/storage-demo/synthetic-summary.json` is a new, labeled copy of parser
results with a deliberately injected error on wrong-price: unit price 2600 cents
and total 13000 cents. This is NOT a real Gemini mistake. Genuine extraction
results and model captures remain unchanged.

Independent expectation: PO agrees 5 * 2000 = 10000 cents. Synthetic billed
13000 gives a 3000-cent discrepancy. The actual invoice bills 5 * 2400 = 12000;
explicitly correcting both fields gives a 2000-cent discrepancy. The resulting
status remains discrepant because correcting extraction does not remove the
invoice's genuine price discrepancy against the PO.

Reproduce/import the supplied demo snapshot:

```powershell
$demo = .\.venv\Scripts\python.exe scripts/run_invoices.py import --dataset synthetic-correction-demo --mode parser --summary runtime/storage-demo/synthetic-summary.json --references client-ai-starter-pack/tasks/invoices/seed.json | ConvertFrom-Json
$demoId = $demo.invoice_ids.'wrong-price'
.\.venv\Scripts\python.exe scripts/run_invoices.py show --id $demoId
.\.venv\Scripts\python.exe scripts/run_invoices.py correct --id $demoId --patch runtime/storage-demo/correction.json --reason 'Remove explicitly labeled synthetic extraction error using invoice text'
.\.venv\Scripts\python.exe scripts/run_invoices.py summary --dataset synthetic-correction-demo
```

The local demo database has already been corrected. Reimporting
intentionally preserves that state, so use a different dataset name (such as
`synthetic-correction-demo-new`) to observe the 3000-cent before state afresh.
SQLite database files are ignored by Git; JSON demo snapshots/reports are separate.

## Actual verification (2026-10-06)

Environment: Python 3.12.10, built-in SQLite 3.49.1, Pydantic 2.13.5.
The complete offline suite passed: **79 tests in 1.247 seconds**. The existing
replay test's older synthetic capture now has an explicit earlier timestamp,
removing a clock-resolution-dependent fixture ordering failure without changing
production extraction behavior or genuine captures.

Actual CLI imports of the current parser, LLM, and replay summaries each
reproduce all four independently supplied expected reconciliation outcomes:
1 reconciled, 1 discrepant, 1 duplicate, 1 unresolved, and **2000 cents** positive
overcharge. The missing PO remains unresolved. All required local artifacts were
available at import time; no evidence was fabricated. Each run has independent
records and totals. No new Gemini requests were made.

The actual synthetic demo changed billed 13000/difference 3000 to billed
12000/difference 2000, retained original extracted total 13000 and original
issues, removed the corrected current price issue, and recorded one history
entry with review state corrected. Closing the process and querying in a fresh
process returned the same ID, current fields, immutable evidence, history, and
outcome. Reimport after restart preserved the correction/history. Reports:
`runtime/storage-demo/report.json` and `runtime/storage-demo/import-verification.json`.

A verification run with socket connections forbidden checked all persisted
values against independent seed expectations and detected zero network calls.
SHA-256 comparisons confirmed all **67 starter, OCR, and Gemini capture files**
remained unchanged.

Known limits: a small local store with schema version 2 and an atomic additive migration from version 1 (see [NOTES_README.md](NOTES_README.md)), no approval
workflow, and no cross-dataset business deduplication or global recovery total.
Dataset selection is explicit. Artifact availability records describe the
initial import, not continuous file monitoring. JSON snapshots trade query
flexibility for a small implementation; they preserve exact cents and reference
records. Reviewers are responsible for the factual correctness of manual edits;
Pydantic only enforces shape/types. OCR, extraction, and genuine captures are
never rewritten by corrections.
