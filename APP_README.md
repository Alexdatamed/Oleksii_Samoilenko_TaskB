# Local invoice review

The FastAPI factory (`app.api.create_app`) mounts Gradio at `/ui` and keeps API docs at `/docs` and readiness at `/health`. `ApplicationService` is shared by API handlers and Gradio callbacks. Each synchronous storage operation opens, uses, and closes its own `InvoiceStore` on the same thread. The existing reconciliation and correction transaction logic is reused unchanged. Startup neither opens the invoice database nor runs OCR/Gemini; opening the review queue performs storage reads and initializes a new database if needed. Existing datasets and correction history remain intact.

## PowerShell, with the project .venv active

Run from the project root:

```powershell
python -m pip install -r requirements.txt

# Offline demo: import saved genuine replay results, images, and OCR associations.
python scripts/run_invoices.py import --dataset replay-v1 --mode replay --summary runtime/extraction/replay/summary.json --references client-ai-starter-pack/tasks/invoices/seed.json

# Alternatively import the saved live Gemini results (no request/key needed to import).
python scripts/run_invoices.py import --dataset llm-v1 --mode llm --summary runtime/extraction/llm/summary.json --references client-ai-starter-pack/tasks/invoices/seed.json

python -m unittest discover -s tests -v
python scripts/run_app.py
```

In a second PowerShell terminal:

```powershell
Start-Process 'http://127.0.0.1:8000/ui/'
Start-Process 'http://127.0.0.1:8000/docs'
```

Same input/run name reimport is idempotent and retains corrections. A different input under an existing name is rejected; choose another dataset name. On a new machine, import the supplied saved results to create image/OCR associations with the local paths. Existing databases moved between machines may retain unavailable old paths; import under a new run name rather than rewriting immutable evidence.

Factory launch alternative:

```powershell
python -m uvicorn app.api:create_app --factory --host 127.0.0.1 --port 8000
```

Use **one worker**. `run_app.py` supports `--db`, `--runtime-dir`, `--references`, `--image-dir`, `--capture-dir`, and `--port`; these are administrator CLI configuration, not API filesystem input. This stage runs locally without authentication, sharing, deployment, or external job infrastructure.

## Review and corrections

Select a dataset and status, then click an invoice row. Summary cards show each reconciliation status and processing errors. Supported overcharge is separate and explains its exclusions. The image, editable fields, comparison, reference candidates, findings, extraction/processing messages, original extracted values, and correction history are presented as readable tables and text, with no JSON viewers or technical metadata. Change fields, supply a nonempty reason, and select **Save correction and recalculate**. The entire dataset is recalculated atomically, including other invoices whose duplicate status changes. Monetary editor values are **USD dollars**, such as `20.00`; their labels explicitly say USD. Display amounts use exact dollar strings, such as `$20.00`. Money inputs accept plain signed numbers with at most two decimal places (no dollar signs, grouping commas, exponent notation, or rounding). Integer arithmetic converts them exactly to the existing service’s cents. Quantities remain whole numbers. Negative and zero values are preserved for reconciliation to assess. Large amounts never pass through float or browser numeric controls. Blank editor values explicitly clear a field to unknown (`null`). No total or reference is inferred.

A processing-failed extraction has no valid fields to edit; process a new extraction run. Extraction issues, processing failures, and reconciliation findings remain separate.

## API

| Route | Behavior |
|---|---|
| GET `/api/datasets` | Dataset names, modes, IDs, creation timestamps |
| GET `/api/invoices?dataset=NAME&status=unresolved` | Ordered review queue; status optional |
| GET `/api/invoices/{id}` | Full evidence, fields, issues, references, outcome, history, current image availability |
| GET `/api/invoices/{id}/image` | Verified original PNG/JPEG bytes |
| GET `/api/summary?dataset=NAME` | Counts, processing errors, supported positive overcharge cents |
| PATCH `/api/invoices/{id}` | Partial correction and dataset recalculation |
| POST `/api/batches` | Multipart `dataset`, `mode`, repeated `files`; completes a bounded sequential batch |

PATCH body example:

```json
{"fields": {"po_id": null, "quantity": 5}, "reason": "Clear an unverified reference; confirm quantity"}
```

Omitted fields remain unchanged; explicit null clears them. Unknown fields and non-integer numeric types are rejected. Responses distinguish 404 missing/unavailable records, 409 conflicts/changed images/busy batches, 413 batch limits, 422 invalid input, and sanitized 500/503 processing/storage failures. POST returns 201 for a persisted dataset even when individual files fail: inspect `files[].status/error` and `summary.processing_errors`. Field extraction issues also remain visible. The supplied missing-reference invoice intentionally has a PO extraction issue and unresolved reconciliation; its successful batch processing does not repair that reference. The existing extraction CLI still exits nonzero for this intentional issue.

## Image batches and offline replay

Use the collapsed **Upload invoices** section or the multipart API. Choose images, select Text parser, Live Gemini, or Offline replay, then **Process invoices**. Progress and per-file outcomes are shown in plain language and a table. The completed dataset is selected with all invoices visible. Accepted decoded formats are PNG/JPEG, at most 10 files, 10 MiB each, and 20 million pixels each. Image content is validated with Pillow. Original bytes, filename, and SHA-256 are retained; paths use a random batch directory and four-digit ordinal names, so source names cannot traverse directories or collide. Identical content remains separate invoices. Processing order is case-sensitive source basename order, then upload index for ties; ordinal file IDs freeze this order. The supplied `clean.png` precedes `duplicate.png` regardless of upload order.

Parser/live modes run the existing standard Docling CPU EasyOCR pipeline, then the explicitly selected extractor. Docling is initialized lazily once and reused sequentially. Its first-run model downloads and dependencies are described in `OCR_README.md`. Live Gemini uses existing `.env` loading, configured model, timeouts, bounded retries, structured output validation, and raw captures; see `EXTRACTION_README.md`. No parser fallback is used. No database connection/transaction stays open during OCR or network requests.

New batch artifacts are isolated under `runtime/review/batches/<random-id>/images`, `ocr`, and the selected extraction mode. Live uploads write raw captures to the shared configured `--capture-dir` (`runtime/extraction/captures` by default), alongside imported captures and separate from validated outputs. Replay and evidence import use that same store. Before an explicit live/replay batch, legacy `runtime/review/batches/*/captures` records are copied there without moving originals or changing immutable evidence references. Identical copies are reused; conflicting filenames are rejected without overwriting either record. Each per-file status, duration, and sanitized error is retained. UI progress reports completed files. Existing artifacts/captures are never overwritten.

**Uploaded replay** skips OCR entirely. It requires an immutable imported image hash/OCR association, verifies the original image still exists unchanged within configured directories, verifies saved OCR text/hash, and refuses absent or ambiguous associations. The existing replay extractor then verifies the content hash and configuration fingerprint and locally validates the original genuine captured response, without a key or network request. A renamed upload can reuse matching content; its caller-supplied ordinal file_id remains its own. No association or compatible capture produces an explicit persisted processing error. Importing the saved replay dataset above enables the four-image offline demo without OCR downloads.

Images are served only by invoice ID after directory confinement, SHA-256, format and size checks. Changed/missing images are explicitly shown as unavailable. No project-wide static mount or broad Gradio `allowed_paths` is used; images in the UI are verified bytes rendered as data URLs. Gradio receives only its own uploaded temporary files. Parser/live/replay modes are labeled; existing synthetic demonstrations retain their explicit provenance and should not be presented as real model mistakes.

## Verification and limits

Default tests use temporary databases and explicitly synthetic images/model responses, with no OCR downloads or Gemini calls. Current real smoke reports are in `runtime/review-smoke/`; they use an isolated database copy, leaving the original correction history unchanged. Actual results are recorded below.

This is a small synchronous local app: no background jobs, pagination, cancellation, multi-worker processing, optimistic correction version checks, or retry UI. SQLite preserves atomic writes; concurrent valid corrections use last committed field changes with immutable audit records. Batch artifacts can remain after persistence failure for inspection. Existing single-line invoice schema/parser limitations apply. Fresh live Gemini requests depend on configured credentials/model/quota; this app stage does not claim new live verification without a recorded real request. Discrepancy-note drafts are now available through explicit actions; see [NOTES_README.md](NOTES_README.md). Deployment remains future work.

Integration APIs were checked against official [FastAPI file uploads](https://fastapi.tiangolo.com/tutorial/request-files/), [FastAPI error handling](https://fastapi.tiangolo.com/tutorial/handling-errors/), [Gradio mounting](https://gradio.app/main/docs/gradio/mount_gradio_app), and [Gradio file access](https://gradio.app/guides/file-access), and against installed signatures. Tested new dependencies: FastAPI 0.142.2, Gradio 6.29.1, uvicorn 0.54.0, python-multipart 0.0.32, Pillow 12.3.0, httpx 0.28.1; Python 3.12.10. Existing dependency pins remain in requirements.txt.

### Actual verification for this stage

- Complete offline command: `python -m unittest discover -s tests -v`: **97 tests passed in 3.885 seconds**, exit 0. All original 79 tests remain passing; 18 application tests added. The installed Starlette TestClient emitted its httpx deprecation warning, and mounted Gradio TestClient checks emitted Windows event-loop ResourceWarnings; these were not hidden and did not fail tests. `python -m pip check`: no broken requirements.
- Real standard Docling CPU OCR + deterministic parser: all four original PNGs processed and persisted in an isolated database; three success records and the intentional missing-PO issue, zero processing errors. Individual durations: clean 52.438 s, duplicate 31.769 s, missing-reference 31.519 s, wrong-price 31.922 s. Extracted values independently match seed fields; reconciliation independently matches every original expected result; supported overcharge **2000 cents**. Standard Docling/PyTorch CPU/deprecation warnings occurred. See `runtime/review-smoke/parser-verification.json`.
- Real uploaded replay via FastAPI TestClient: genuine existing captures, credentials absent, **zero Gemini client and zero OCR calls**, all four fields/outcomes match the independent fixtures, total **2000 cents**. See `runtime/review-smoke/replay-verification.json`.
- Actual in-app browser smoke: `/ui` loaded datasets and source images; selected wrong-price, saved an explicitly synthetic edit in the isolated database, verified queue/current fields/review state and total refreshed from 2000 to 0. Original total 12000 remained in evidence and correction history persisted. Status filter showed the missing-reference record with blank PO and its extraction issue. Uploaded all four actual images through Gradio in replay mode, observed progress, verified new dataset selection/summary and persisted outcomes (all original statuses; 2000 cents). `/docs` rendered all routes/request schemas. See `runtime/review-smoke/ui-verification.json`.
- Hash verification: **67 starter, original OCR, and original capture files unchanged**. Original database retained its one existing correction and original parser total 2000; all smoke edits/runs used a database copy. No new live Gemini request was made in this app stage; its orchestration was tested with explicitly synthetic mocked responses/failures.
- Temporary smoke server stopped after testing. Start the normal application with the commands above. No deployment or generated LLM discrepancy notes were added.

### Capture lookup regression fix

Live uploads and replay now share the configured capture store, including imported captures. Offline regression tests use the actual Gemini capture writer and real ReplayExtractor, mocking only the SDK network request and OCR. Test responses are labeled `synthetic_test`; only test-injected replay allows that origin. Production replay continues to require genuine captures. Legacy captures remain in place and immutable evidence is preserved.

Fix verification: `python -m unittest discover -s tests -v` completed with **99 tests passing in 5.435 seconds**, exit 0 (all prior 97 plus two regressions). The upload/replay regression verified unchanged captured bytes and original evidence, identical fields/issues, new caller IDs including `0001`, zero OCR/network calls during replay, and rejection of synthetic captures by production defaults. Migration tests verified idempotent copies, preserved source/failure records, and no overwrite on conflict. All 67 protected starter/OCR/capture files remain unchanged. Existing Starlette httpx deprecation and Windows mounted-Gradio event-loop warnings remained non-failing. No live Gemini request or real OCR run was made for this fix.

### Reviewer interface redesign

All visible JSON components and metadata dumps have been removed; JSON storage, APIs, captures, replay, business logic and audited corrections are unchanged. The queue opens details by row selection. Empty filters clear details; processing-failed records have disabled correction controls. Unknown invoice values stay unknown, and ambiguous reference candidates are all displayed without selecting one for the comparison. Synthetic datasets/correction demonstrations are visibly labeled. New UI tests cover scalar tables, escaped HTML, exact USD parsing/formatting (including large, zero and negative amounts), missing values, candidate ambiguity, histories, and updated duplicate totals.

Redesign verification: `python -m unittest discover -s tests -v` finished with **114 tests passing in 5.079 seconds**, exit 0. All earlier tests remain passing (currency UI expectations updated), with 15 focused presentation tests added. The final browser verified summary cards, row selection, image/form layout, reference/comparison tables, exact USD edits, excess-precision rejection, correction history, empty filters, and disabled controls for a processing failure. A real offline replay upload of all four supplied invoices plus an explicitly synthetic invalid image produced readable progress/outcome tables and selected the new dataset. A synthetic identity correction in the isolated smoke database updated another invoice from Duplicate to Reconciled. All 67 protected files and the original database history remained unchanged. See `runtime/ui-redesign-smoke/verification.json`. No fresh live Gemini request or real OCR was run for this presentation stage. Existing Starlette TestClient deprecation and Windows Gradio event-loop ResourceWarnings remained non-failing.

UI limitations: desktop browser smoke tested; mobile layout was not separately tested. USD editors accept plain signed numbers such as `20.00`, not dollar signs or grouping commas. Gradio built-in transient loading announcements may follow the browser locale; all application labels, findings and messages are English. Metadata remains available in the existing API and files, rather than in the review screen. The temporary smoke server is stopped after verification.

### Discrepancy notes

Explicit Gemini drafting and keyless note replay are documented in [NOTES_README.md](NOTES_README.md), including safe v1-to-v2 SQLite migration, immutable note history, context-based freshness, API endpoints and exact PowerShell steps for one genuine capture. No note request runs during selection, filters, refresh or corrections. Notes only draft text for human review; they never change reconciliation or send messages.
