# Invoice extraction: parser, Gemini, and replay

All three modes read saved OCR Markdown, not invoice
seed fields, expected results, purchase orders, or receipts. Those references
are used only by independent verification tests. The original starter pack,
OCR artifacts, OCR implementation, and reconciliation logic/tests are preserved.

## Run in PowerShell from the project root

Use the active project environment, or explicitly select it as below:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/run_extraction.py --mode parser
$LASTEXITCODE
```

Custom directories:

```powershell
.\.venv\Scripts\python.exe scripts/run_extraction.py --mode parser --input-dir runtime/ocr --output-dir runtime/extraction/parser
```

The CLI processes `.md` files in filename order. Default paths are resolved
relative to this project, independent of the shell's working directory. Results
are saved as `runtime/extraction/parser/<source-name>.json`, for example
`clean.md.json`, with mode, absolute source path, file ID, processing status,
canonical fields, issues, error, and elapsed seconds. `summary.json` records
all file outcomes. OCR artifacts are never rewritten. Output paths equal to or
inside the OCR input directory are rejected.

An individual read, parsing, or output error is recorded and processing
continues. Exit code 0 means every file was processed without extraction issues;
exit code 1 means at least one processing error or issue, or a batch error such
as an empty input directory. In the supplied batch, the missing purchase order
intentionally results in exit code 1 despite all four files being processed.
`status` in CLI records describes extraction processing only, never invoice
reconciliation.

## Shared schema and function contract

`app.schemas.InvoiceFields` is a strict Pydantic v2 model containing:
`file_id`, `invoice_number`, `supplier_id`, `po_id`, `sku`, `quantity`,
`unit_price_cents`, and `total_cents`. All extracted fields permit `None`;
`file_id` is required and supplied by the caller. Blank identifiers, booleans
and floats in integer fields, coercion from numeric strings, and extra fields
are rejected. Zero and negative integers are permitted: their business validity
belongs to reconciliation.

```python
from app.extraction import extract_invoice_fields
from app.reconciliation import reconcile_invoice

fields, issues = extract_invoice_fields(markdown, file_id="source-filename-stem")
canonical_fields = fields.model_dump()
issue_records = [issue.model_dump() for issue in issues]
result = reconcile_invoice(fields.to_reconciliation_dict(), purchase_orders, receipts)
```

The function returns `(InvoiceFields, list[ExtractionIssue])`. Issues have
`field`, `code` (`missing`, `invalid`, or `conflicting`), `message`, and
`raw_values`. Unknown or ambiguous fields are `None`, not zeros or empty strings.
Equal repeated parsed values are accepted; conflicting repeats are unresolved.
Any missing/invalid occurrence prevents a valid repeat from overriding it.
Input-type or file ID schema errors raise exceptions, which the CLI records.

The canonical schema uses `unit_price_cents`; the existing reconciler uses
`unit_cents`. `fields.to_reconciliation_dict()` explicitly maps that one key,
preserving all other values including `None`. A plain canonical `model_dump()`
is for serialization; use the adapter when calling reconciliation. This keeps
the existing reconciler unchanged and makes the same schema reusable by a future
extractor. Gemini and replay also use this schema and adapter.

## Supported text and limitations

The parser supports both supplied invoice layouts, invoice headings with or
without the title separator, heading markers, case-insensitive labels, whitespace
and line breaks, and multiple labeled fields on one line. It recognizes invoice
(number/no/id), supplier (ID), purchase order (ID)/PO ID, item / SKU or SKU,
quantity, unit price (USD), and total (USD). The observed `Total (USD}` typo is
accepted. Unlabeled identifiers are never inferred, and identifier values are
never hardcoded or corrected.

Identifiers are single tokens containing ASCII letters/digits and `.`, `_`,
`/`, or `-`. Numeric values use plain signed decimal notation; quantities must
use integer notation. Prices and totals use Decimal and convert dollars to
integer cents exactly, with at most two written decimal places. Excess precision
is rejected even when extra digits are zeros. Fractional quantities are rejected.
No missing total is calculated and inconsistent totals are left as written.

This is a single-line invoice parser, not a general document understanding
system. Unsupported labels, Markdown tables/emphasis, intervening prose, multiple
items, currency/thousands separators, exponent notation, split identifier tokens,
and other OCR spelling errors may yield missing/invalid/conflicting issues.
Repeated fields are resolved conservatively, without reference-table lookup.
Extraction correctness on these four images does not establish general OCR or
parser accuracy. No reconciliation status is determined by the parser.

## Actual verification (2026-10-06)

Tested with the existing `.venv`: Python 3.12.10 and Pydantic 2.13.5.
The complete suite passed: **32 tests in 0.065 seconds**, including the 16
existing OCR/reconciliation tests and 16 extraction tests.

The actual CLI processed all four OCR Markdown files. Clean, duplicate, and
wrong-price extraction completed without issues. Missing-reference returned
`po_id=null` and exactly one `missing` issue. CLI exit code was **1**, as required
when any extraction issue occurs; no processing errors occurred.

All four saved CLI records were independently compared with the original seed
values and validated against the shared schema. Extracted values passed through
the explicit adapter reproduce all four expected reconciliation results, with
clean processed before duplicate and overcharge total **2,000 cents**.
SHA-256 comparisons confirmed that all **51 starter-pack and OCR files** were
unchanged during this implementation. No dependency or artifact required for
verification was unavailable.

## Gemini and offline replay

The official [Google Gen AI Python SDK](https://googleapis.github.io/python-genai/)
documents `client.models.generate_content` with JSON output and
`response_json_schema`. The [current Gemini structured-output guide](https://ai.google.dev/gemini-api/docs/structured-output)
describes the supported JSON Schema subset. Although newer examples use the
Interactions API, Google states that [generateContent remains supported](https://ai.google.dev/gemini-api/docs/interactions-overview).
This implementation uses that supported SDK call, verified against installed
`google-genai==2.28.0` on 2026-10-06.

Install, test, and run from the project root in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/run_extraction.py --mode parser
.\.venv\Scripts\python.exe scripts/run_extraction.py --mode llm
.\.venv\Scripts\python.exe scripts/run_extraction.py --mode replay
$LASTEXITCODE
```

Create a local project-root `.env` using the credential-free `.env.example` as
a template, and set `GEMINI_API_KEY` and `GEMINI_MODEL` there. Do not overwrite an
existing `.env`. Live mode calls `load_dotenv(ROOT / '.env', override=False)`;
existing environment variables take precedence. No key is printed, captured,
or included in error messages. The existing tracked `.env` was removed from the
Git index, while the local file was retained; `.gitignore` excludes it. No Git
history was rewritten. Replay does not load `.env` or require any credentials.

The configured model is used exactly; no fallback model, parser fallback,
provider switch, or billing change is implemented. Only the OCR document and
versioned extraction instructions are sent. OCR is untrusted document data in
a separate user message; extraction instructions are the system instruction.
No tools, filenames, seed values, expected results, reference tables, or parser
answers are sent. The wire schema excludes file_id and derives nullable field
definitions from InvoiceFields; the caller's file_id is added only locally.

`GeminiExtractor.extract(markdown, file_id)` and
`ReplayExtractor.extract(markdown, file_id)` return
`ExtractionResult(fields: InvoiceFields, issues: list[ExtractionIssue], metadata: dict)`.
Extraction fields/issues remain the same types as parser mode; metadata is kept
separate. Invalid JSON/types/schema, contradictory issue/value pairs, empty,
blocked, or truncated responses are processing errors, not reconciliation
findings. Null or omitted fields get issues if the model omitted them. Valid
zero/negative values and inconsistent totals are passed through unchanged.

The SDK gets a 30-second request timeout and one attempt. The application alone
makes at most three attempts for 408/429/500/502/503/504 or connection/timeouts.
It honors numeric/date Retry-After and google.rpc.RetryInfo delays. A requested
wait above 30 seconds aborts rather than retrying too early. Nontransient errors
are not retried. The same client is reused across the batch and closed afterward.
Provider messages/URLs and raw validation exceptions are suppressed because they
can contain sensitive data; errors retain sanitized HTTP code/reason.

### Captures and replay lookup

Defaults:

- Validated parser results: `runtime/extraction/parser/`
- Validated live results: `runtime/extraction/llm/`
- Validated replay results: `runtime/extraction/replay/`
- Append-only raw captures: `runtime/extraction/captures/`

Use `--input-dir`, `--output-dir`, and `--replay-dir` to override directories.
`--replay-dir` is written by live mode and read by replay mode. Mode result
directories cannot overwrite one another or the OCR input directory. Raw captures
are separate from validated fields/issues. A `*.failure.json` record documents
an exhausted provider request without pretending there was a model response.

```powershell
.\.venv\Scripts\python.exe scripts/run_extraction.py --mode llm --input-dir runtime/ocr --output-dir runtime/extraction/llm --replay-dir runtime/extraction/captures
.\.venv\Scripts\python.exe scripts/run_extraction.py --mode replay --input-dir runtime/ocr --output-dir runtime/extraction/replay --replay-dir runtime/extraction/captures --replay-model gemini-3.8-flash
```

Successful SDK responses, including ones subsequently rejected by validation,
are saved before application validation as unique, exclusively created
`<OCR-SHA256>/<UTC-timestamp>_<UUID>.capture.json` files. A capture stores the
original final response text and serialized SDK response, exact OCR string sent
and its UTF-8 SHA-256, full prompt/schema and their versions/hashes, configuration
fingerprint, requested/returned model where available, generation settings,
timestamp, SDK version, usage, finish metadata, and request attempt count.
Credential-bearing transport HTTP headers are omitted from SDK serialization.
If a response unexpectedly echoes the credential, capture is refused instead
of saving the key. Captures have no local absolute source or machine paths and
can be included in the submission.

Replay lookup uses the OCR hash, not the filename. It verifies OCR, prompt,
schema, configuration, and original response hashes, then runs the same local
processing code as live mode. It uses the newest capture compatible with the
current prompt/schema/generation settings; it never hides a validation failure
by falling back to an older valid response. Renaming a source retains the new
caller-supplied file_id. By default the recorded requested model identifies the
configuration; `--replay-model` can require a specific one. Replay never
constructs a client or calls the network. Missing, mismatched, or corrupted
captures produce explicit errors. Hashes detect changes, not cryptographic
proof of capture provenance.

Tests explicitly label mocked responses as `synthetic_test`, use temporary
capture directories, and never publish them as real captures. Production replay
rejects synthetic records. No real response is invented or repaired.

Each mode preserves the exit convention: nonzero for any processing error or
extraction issue. Once it is successfully extracted, the supplied
missing-reference invoice intentionally has a missing PO issue and therefore
makes the batch return 1 even if every request succeeds.

### Actual Gemini verification (2026-10-06)

Environment: Python 3.12.10, Pydantic 2.13.5, google-genai 2.28.0,
python-dotenv 1.2.4. `pip check` found no broken requirements.
The full offline suite passed: **55 tests in 0.361 seconds**, including all
32 existing tests. No real provider request is made by the test suite.

Requested and returned model for the successful real response:
**gemini-3.8-flash**. Prompt version: **invoice-gemini-v1**. Schema version:
**invoice-fields-issues-v1**. Settings: temperature 0, 4096 maximum output tokens,
one candidate, application/json. Model-default thinking/safety settings were
left unchanged; no tools or automatic fallback were configured.
The sanitized root `ai_configuration_manifest.json` and per-mode
`ai_configuration.json` record hashes, versions, transport, and returned model.

| Input | Live outcome | Live attempts | Replay outcome |
| --- | --- | ---: | --- |
| clean.md | valid real response | 1 | valid |
| duplicate.md | HTTP 503 provider server error | 3 | valid; reuses identical clean OCR content |
| missing-reference.md | HTTP 429 quota/rate limit | 3 | explicit missing capture error |
| wrong-price.md | HTTP 429 quota/rate limit | 1 | explicit missing capture error |

The last request aborted without retrying because the provider retry guidance
exceeded the maximum wait. Live and replay both returned exit code **1**.
One real response and three sanitized request failure records were preserved.
No additional live requests were made after this batch.

Real replay was run with API credentials removed, SDK client construction and
socket connects blocked, and dotenv loading forbidden. Verified counts were
zero for all three; renamed-source file_id was also checked. Available live and
replay values match independent seed values without field discrepancies.
Partial reconciliation reproduces clean=reconciled and duplicate=duplicate,
with clean processed first. The partial overcharge total is zero; the complete
Gemini batch could not be verified because two distinct OCR contents lack real
responses. The existing parser tests still verify the full expected 2,000-cent
seed total independently.

`runtime/extraction/verification.json` records the independent comparisons and
unavailable cases. All 51 original starter-pack/OCR files were unchanged by
SHA-256 comparison. A local credential-content scan found no API key in the new
code or saved capture/results/manifest artifacts.

Remaining limits: structured output/Pydantic validate shape and types, not
factual grounding. The model can still misread or invent schema-valid values;
review and independent references are necessary. Temperature zero does not
promise deterministic live results. Replay requires compatible authentic
captures and pinned prompt/schema settings. Full live verification remains
blocked by the observed provider server error and quota/rate limits; there is
no fabricated result for the missing cases.
