# Reproducible Task B six-invoice demo

This is a fixed fictional fixture snapshot, not real supplier data. It extends the four supplied invoices with the two user-specified cases and preserves every supplied image/record. IDs INV-4/INV-5, PO-3/PO-4, RC-3/RC-4 and the two file IDs were confirmed unused in the supplied fixtures; no requested identifier was changed. The source-of-truth remains `client-ai-starter-pack/tasks/invoices/domain.md`. The starter pack's fixture-generation guidance was followed for the data; the user's separate request authorized OCR and application verification.

## Expected and actually observed

| File ID | Expected status / difference | Observed status / difference | Findings |
| --- | --- | --- | --- |
| clean | Reconciled / $0.00 | Reconciled / $0.00 | None |
| duplicate | Duplicate / $0.00, excluded | Duplicate / $0.00, excluded | Duplicate identity |
| missing-reference | Unresolved / Unknown, excluded | Unresolved / Unknown, excluded | Missing PO reference |
| quantity-mismatch | Discrepant / $20.00 | Discrepant / $20.00 | Ordered and received quantity mismatch |
| sku-mismatch | Unresolved / Unknown, excluded | Unresolved / Unknown, excluded | SKU mismatch |
| wrong-price | Discrepant / $20.00 | Discrepant / $20.00 | Unit price mismatch |

Combined actual/expected summary: **1 reconciled, 2 discrepant, 1 duplicate, 2 unresolved, 0 processing errors; supported overcharge 4000 cents ($40.00)**. The supplied missing-reference invoice intentionally has a missing-PO extraction issue; that is not a processing error.

`expected.json` is a fixed independently authored answer key based on the user's specified cases, supplied original expectations and domain rules. It is never created by reconciliation or passed into extraction. Quantity arithmetic: ordered/received 5, billed 6; 5 × 2000 = 10000 expected, billed 12000, difference 2000. The SKU case bills CAB-2 against CAB-1, so the existing conservative rule leaves expected/difference null, even though its PO amount could be calculated in isolation. Original wrong-price contributes the other 2000 cents; duplicates and unresolved cases contribute nothing. No new business rules.

## Files and evidence

- `additional/seed.json`, `additional/render_invoices.py`, `additional/images/`: two fictional inputs and two actual rendered PNGs. Renderer adapts the supplied two layouts and uses signed integer arithmetic for money; it writes only these two additional images. No randomness.
- `images/`: byte-identical copies of all six source images, giving the existing app one allowed image directory. Original supplied PNGs remain in place.
- `references.json`: supplied PO/receipt records unchanged, followed by the two additional pairs. No invoice seed values are included in this reconciliation input.
- `manifest.json`: portable repository-relative source/asset paths and hashes. Effective order is clean, duplicate, missing-reference, quantity-mismatch, sku-mismatch, wrong-price. Existing imports sort by file_id, retaining clean before duplicate.
- `runs/<run>/`: authentic OCR exports, Docling JSON and summaries, actual deterministic parser records, a hash manifest and portable source provenance. `latest.json` identifies the current saved snapshot. Every new processing run gets a unique directory; earlier evidence is retained. Existing conflicting image copies are refused, never silently overwritten.
- `captures/`: byte-identical, compatibility-checked copies of four existing genuine Gemini captures plus a provenance/hash manifest. New cases have no captures. This is the shared app capture store for optional original-image replay; new requests remain explicit.
- `observed.json`: actual result/verification report; `preservation.json`: SHA-256 baseline for 164 supplied/runtime files, including the pre-existing database and captures.
- `invoices.sqlite3`: separate generated demo database, ignored by Git and reproducible using saved evidence. Never copy it over the current `runtime/invoices.sqlite3`.

The new images were processed with real standard Docling/EasyOCR English CPU full-page OCR: quantity-mismatch **49.267 seconds**, sku-mismatch **32.050 seconds**. Original four OCR exports were reused as authentic existing evidence and copied without modification. All six parser outputs came from actual Markdown, never seed fields or expected answers. Versions in the OCR summary: Python 3.12.10, Docling 2.134.0, EasyOCR 1.7.2, Torch 2.14.1, Torchvision 0.29.1. First-time OCR requires the existing documented model downloads/cache; loading saved evidence does not.

Fictional inputs are not synthetic provider responses. The six-invoice dataset uses actual OCR + parser mode. There are **no fabricated OCR exports, no synthetic demo provider responses, and no new Gemini calls**. Synthetic provider responses remain confined to explicitly labeled isolated tests elsewhere.

## PowerShell: open the already prepared demo without a key

Run from the repository root with your `.venv` active:

```powershell
python -m pip install -r requirements.txt
Remove-Item Env:GEMINI_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:GOOGLE_API_KEY -ErrorAction SilentlyContinue
python demo/run_demo.py load
python demo/run_demo.py verify
python demo/run_demo.py preserve
python scripts/run_app.py --db demo/invoices.sqlite3 --references demo/references.json --image-dir demo/images --runtime-dir demo/review --capture-dir demo/captures --port 8003
```

Open **http://127.0.0.1:8003/ui/** and select **demo-six-parser · Text parser**. Click quantity-mismatch or sku-mismatch to see actual images, source records and findings. Both new receipt IDs are visible. Expected/difference remain Unknown for SKU mismatch. These are genuine fictional document discrepancies, not synthetic correction demonstrations. Do not click **Generate discrepancy note** or choose live extraction merely to load the demo: those are separately explicit live requests.

`Settings.image_dir` controls permitted original images; `Settings.references_path` controls references for future uploaded batches. Imported datasets retain their own immutable reference snapshots. All configured paths above stay demo-local, including future uploads and note/extraction captures. Application defaults are unchanged; no new app configuration feature was necessary. Saved import checks image/OCR/parser/reference hashes, constructs no OCR or Gemini client, never loads `.env`, and requires no key even if `.env` remains present. Repeat `load` of the same saved snapshot/dataset is idempotent and preserves manual corrections. Verification will correctly fail if you intentionally correct the demo away from its fixed original expectations.

## PowerShell: reproduce rendering and processing

```powershell
python demo/additional/render_invoices.py
python demo/run_demo.py prepare
python demo/run_demo.py process
```

`process` calls the existing OCR runner for **only the two new images**, reuses the four existing successful saved OCR exports, then calls the existing deterministic parser runner on the six actual texts. It reads no expected file and passes no invoice seed values to either stage. It records real failures and continues through file failures; missing authentic evidence is reported, not invented. Sources/summaries are normalized to portable paths without changing parsed values or issues.

The expected process exit code is **1**, solely because the supplied missing-PO case produces an extraction issue. Inspect the reported processing-error count: the completed run has **0**. The verification command returns **0** only if every file ID, status, finding set and amount matches the fixed answer key and all counts/overcharge match. Failed verification returns **1**.

After a new processing run, use a new dataset name to retain the previous immutable snapshot rather than overwriting it:

```powershell
python demo/run_demo.py load --dataset demo-six-parser-rerun
python demo/run_demo.py verify --dataset demo-six-parser-rerun
```

To verify the original prepared snapshot use the default dataset. A newly processed snapshot may have different durations or OCR bytes; importing it under an existing dataset name with a different fingerprint is deliberately rejected. Use saved loading when you only need the reproducible keyless demo. The demo runner refuses existing runtime database paths; its default database is separate.

For comparison, the existing standalone runners remain available (`scripts/run_ocr.py`, `scripts/run_extraction.py`, `scripts/run_invoices.py`). The demo wrapper adds portable provenance, guarded saved-evidence loading and six-case verification without changing their defaults.

## Existing genuine captures and optional future live work

A separate **actual keyless replay of the original four pre-existing genuine captures** passed compatibility checks. Their original raw bytes were copied without modification into the configured `demo/captures` store, preserving originals and making compatible original-image replay available through the app. The two new cases intentionally report unavailable replay until genuine captures exist. Model recorded by those captures: **gemini-3.1-flash-lite**. All original extracted fields independently matched the supplied fixture values. Zero SDK-client, dotenv and OCR calls occurred; zero processing errors, one intentional missing-PO issue. See `gemini-four-replay/` and `gemini-four-verification.json`. This does not make the six-invoice parser dataset an LLM dataset. The two new images have no genuine Gemini extraction or discrepancy-note captures yet; those checks are **pending** and unnecessary for the parser demo.

To reproduce four-only genuine replay, choose a new output directory so existing saved evidence remains intact:

```powershell
$tag = Get-Date -Format 'yyyyMMdd-HHmmss'
python scripts/run_extraction.py --mode replay --input-dir runtime/ocr --output-dir "demo/gemini-four-replay-$tag" --replay-dir demo/captures
```

This accepts only genuine compatible captures through the existing replay validator; no fallback or response fabrication. Exit code 1 is expected for missing-reference. To make an independently chosen future **live six-invoice extraction**, privately configure root `.env` first, then explicitly run:

```powershell
$run = Get-Content -Raw demo/latest.json | ConvertFrom-Json
$evidence = Get-Content -Raw $run.run | ConvertFrom-Json
$tag = Get-Date -Format 'yyyyMMdd-HHmmss'
python scripts/run_extraction.py --mode llm --input-dir $evidence.ocr_dir --output-dir "demo/live-$tag" --replay-dir demo/captures
```

That optional command makes live requests for all six; it was **not executed** here. It uses the configured model and saves raw captures before local validation. Keep that mode's evidence/dataset separate from parser results; never relabel synthetic tests as real captures. Discrepancy-note generation/capture steps remain in `NOTES_README.md` and are likewise separate explicit actions.

## Actual checks

```powershell
python -m unittest discover -s tests -v
python demo/run_demo.py verify
python demo/run_demo.py preserve
```

**139 tests passed in 21.559 seconds**, exit 0, including eight focused demo tests with isolated databases. All original 131 tests passed. Tests cover fixed fixtures/arithmetic, original expectations, combined reference records and byte-identical assets, safe preparation, exact cents rendering, guarded/idempotent keyless import, real six-image API availability, verification failure after an isolated synthetic correction, and preservation hashes. Existing Starlette httpx deprecation and Windows Gradio event-loop warnings remained non-failing. Real OCR emitted existing CPU/deprecation warnings but both files succeeded.

Browser smoke passed: all six rows, correct counts/$40.00, both new original images, quantity comparison 5/5/6 and two findings, PO-3/RC-3, and CAB-1/CAB-1/CAB-2 with unknown expected/difference and PO-4/RC-4. No UI corrections were made to the demo during this check. All 164 protected supplied/runtime files remained byte-identical. Mobile layout and new genuine Gemini captures remain pending. The temporary server was stopped after verification; start it with the command above.
