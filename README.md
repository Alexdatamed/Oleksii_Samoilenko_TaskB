# Task B: Invoice Reconciliation

Local invoice review application: PNG invoices -> standard Docling/EasyOCR -> deterministic parser or explicit Gemini extraction -> reconciliation -> SQLite audit history -> FastAPI/Gradio review. Offline replay uses genuine saved responses. The source of truth is `client-ai-starter-pack/tasks/invoices/domain.md`; supplied files are unchanged.

## Quick start (PowerShell)

Python 3.12.10 was tested. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python demo/run_demo.py load
python demo/run_demo.py verify
python scripts/run_app.py --db demo/invoices.sqlite3 --references demo/references.json --image-dir demo/images --runtime-dir demo/review --capture-dir demo/captures --port 8003
```

Open http://127.0.0.1:8003/ui/ and select **demo-six-parser**. Saved demo loading requires neither model downloads nor credentials. SQLite is created locally; do not commit database files. The latest completed suite passed **139 tests in 21.559 seconds**; see `demo/observed.json`.

## Expected demo

Six fictional invoices: the original four plus quantity mismatch and SKU mismatch. Expected counts: **1 Reconciled, 2 Discrepant, 1 Duplicate, 2 Unresolved, 0 Processing errors**. Supported overcharge is **$40.00**: $20.00 wrong price plus $20.00 quantity mismatch. Duplicate and unresolved invoices are excluded. SKU mismatch has unknown expected amount/difference. Clean is processed before duplicate. Expectations are fixed independently in `demo/expected.json`.

The two new invoices have actual successful OCR exports; the original four reuse authentic saved exports. No seed values are passed into extraction. See `demo/README.md` for rendering, provenance, hashes, durations, saved evidence and commands. Reprocessing exits 1 for the intentional missing-PO extraction issue; this is not a processing failure.

## Genuine keyless Gemini replay

```powershell
$tag = Get-Date -Format 'yyyyMMdd-HHmmss'
python scripts/run_extraction.py --mode replay --input-dir runtime/ocr --output-dir "demo/replay-$tag" --replay-dir demo/captures
```

Four genuine compatible captures are supplied; their recorded model is **gemini-3.1-flash-lite**. Replay performs no network calls and does not load `.env`. Exit 1 is expected for the missing-PO issue. The two additional cases have no genuine Gemini captures. Synthetic test responses are explicitly labeled and rejected by production replay.

Live extraction is optional and requires a private root `.env` based on `.env.example`. The configured model is used without switching. Never commit credentials. Explicit note-generation actions also require live access; notes never change reconciliation or send messages.

## Rules, corrections and limitations

Integer USD cents are used throughout. Match by supplier and PO identifiers; ambiguous, conflicting or missing references remain unresolved. Expected amount is ordered quantity times agreed price. Negative quantities/money are unresolved; zero is preserved without inventing a zero-specific prohibition. Invoice/receipt SKU mismatch conservatively leaves expected/difference unknown. Missing totals are never calculated. These policies and tests document domain ambiguities.

Edit a field in the UI, enter a reason, and save/recalculate. Audit history and original evidence are retained; affected duplicate records and totals refresh. For a reproducible explicitly synthetic correction example, see `STORAGE_README.md` and `runtime/storage-demo/`: billed total 13000 -> 12000 cents changes difference 3000 -> 2000 cents.

This is a local single-worker application, without authentication or deployment. Desktop browser checks passed; mobile verification remains pending. Saved evidence demonstrates these fixtures, not general OCR/LLM accuracy. No new live Gemini requests were made for the six-invoice demo. First real OCR use may download models; see `OCR_README.md`.

## Documentation

- `APP_README.md`: API, UI and upload/replay behavior.
- `OCR_README.md`, `EXTRACTION_README.md`: dependencies, modes and validation.
- `STORAGE_README.md`, `NOTES_README.md`: audited corrections and optional notes.
- `demo/README.md`: six-invoice reproduction and actual verification.
- `ai-workflow/README.md`, `ai-workflow/manifest.json`: development AI workflow and sanitized configuration.
- `ai_configuration_manifest.json`: current demo/replay index; earlier report is retained verbatim under `ai-workflow/history/`.

Older stage-specific test counts in documentation describe their historical runs. The current completed full-suite count is 139.
