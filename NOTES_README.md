# Discrepancy drafts

Use the existing active `.venv` and pinned dependencies (`python -m pip install -r requirements.txt`). Start `python scripts/run_app.py`, then open http://127.0.0.1:8000/ui/. Select a discrepant, duplicate or unresolved invoice and explicitly click **Generate discrepancy note** or **Replay saved note**. Selection, filters, refresh and corrections never call Gemini. Reconciled invoices show **No discrepancy note needed**; processing failures require a verified reconciliation result. The UI shows readable escaped draft text and source labels, with no JSON or provider details. No messages or emails are sent.

Live mode uses the root `.env` through the existing python-dotenv configuration (`override=False`), `GEMINI_API_KEY` and the configured `GEMINI_MODEL`, without switching models or enabling billing. Never paste keys into commands or logs. `.env` remains ignored and `.env.example` credential-free. Replay never loads `.env`, constructs an SDK client, runs OCR or requests a key.

The existing official google-genai `models.generate_content` transport is reused: explicit structured JSON schema, temperature 0, max output 4096, one candidate, 30-second timeout, SDK attempts 1, application attempts at most 3 for transient failures, honoring retry guidance with a maximum 30-second wait. Checked against the official [SDK structured JSON example](https://googleapis.github.io/python-genai/#json-response-schema); existing installed SDK 2.28.0, Python 3.12.10 and Gradio 6.29.1. No new dependencies.

## Evidence and validation

Only current invoice fields, deterministic status/findings/amounts, confirmed source records, source identifiers and relevant correction provenance are supplied. Integer cents accompany exact USD strings; no missing amounts are calculated. Ambiguous candidates include their identifiers but are explicitly unconfirmed and are excluded from the citation allowlist. Earlier duplicate identity comes from preceding processed invoice records in the same dataset, never from a guess. Invoice text identifiers and correction reasons are document data, not instructions.

Prompt `prompts/discrepancy_note_v1.txt` has version `discrepancy-note-v1`; schema `discrepancy-note-response-v1`; context `verified-invoice-note-v1`. Captures store their hashes, exact verified context and canonical SHA-256, requested/returned model, generation/transport settings, UTC timestamp, SDK version, token usage and finish metadata. Genuine original response text and serializable SDK response are saved **before validation**, including invalid, blocked or truncated responses. Credential echoes are refused instead of stored. Transport failures have sanitized failure records; a configuration failure before a request has no provider response to capture.

Default note captures are separate from extraction replay at `runtime/extraction/captures/notes/<context-sha256>/*.capture.json`, or `<configured capture-dir>/notes`. Unique exclusive filenames preserve prior captures. Replay checks a whole-capture checksum (including origin, timestamp and metadata), context, response and SDK hashes, prompt/schema content, model and configuration fingerprint, and uses the same local validation. If no model is requested/configured in the environment, keyless replay uses a stored requested model only when its complete current note configuration matches. Incompatible/missing capture is unavailable; tampered/invalid capture is failed. Production replay rejects synthetic captures. Tests alone explicitly opt into `synthetic_test`.

Pydantic rejects invalid types/extra fields, empty/oversized notes, unknown or uncited source references, unsupported finding codes and incorrect structured cents. Explicit dollar amounts must use supplied exact strings with structured amount references. Capture hashes detect modification; they are unsigned integrity checks, not proof of provider authenticity against deliberate rewriting. Notes remain drafts: checks do not prove factual accuracy, English fluency, complete coverage, appropriate tone, or semantic correctness of free text. Sentence count is prompt guidance, not a brittle punctuation rule. Review against original evidence before use.

## Persistence and freshness

Opening an existing schema-v1 database atomically adds the `discrepancy_notes` table, index and immutable-history triggers and upgrades `PRAGMA user_version` to 2. Fresh databases use v2. Invoice evidence, reference snapshots, correction history and prior data are preserved; unsupported newer schema versions are refused. Back up the database before normal application upgrades as desired.

Validated drafts, references, context snapshots/hashes and attempt metadata are stored separately from invoices and reconciliation. Failures/unavailable attempts are recorded; no template is substituted or labeled Gemini output. Historical attempts are immutable and returned by the retrieval API. Freshness is derived on every retrieval from the current context, rather than mutating historical notes: changed-context drafts are **outdated**, including duplicate relationships changed by corrections to another invoice. Reconciled records suppress drafts with **No discrepancy note needed**; stale historical drafts remain in API history. Explicit generation/replay is required to create a new attempt. Selection and correction callbacks refresh the visible freshness state.

A consistent read snapshot is closed before any provider request. After the request a short SQLite transaction rechecks the context and saves the original-context response. A changed-context response is historical/outdated; it never replaces current invoice values. Context is rechecked again during retrieval/display. As with the existing local UI, another user can edit after a response is delivered; refresh before using a draft.

API endpoints:
- `GET /api/invoices/{invoice_id}/note`: state, current draft (if current), latest attempt and complete historical records.
- `POST /api/invoices/{invoice_id}/note/generate`: configured live generation, only for explicit user actions.
- `POST /api/invoices/{invoice_id}/note/replay`: optional JSON `{ "model": "<captured requested model>" }`; no credentials required.

These return HTTP 200 for explicit note states (`current`, `outdated`, `unavailable`, `failed`, `not_needed`, `requires_reconciliation`); unknown invoice is 404 and invalid request shape is 422. Inspect `state`, not just HTTP success. User-facing failure messages suppress provider details and paths; capture metadata stays available in APIs/files.

## Generate one genuine note and verify keyless replay (PowerShell)

In terminal 1, with the active `.venv` and root `.env` configured privately:

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/run_app.py
```

In terminal 2, choose the existing parser dataset (use another dataset name if appropriate), locate wrong-price, and generate once. This is the **only live request** in these instructions:

```powershell
$base = 'http://127.0.0.1:8000'
$rows = Invoke-RestMethod "$base/api/invoices?dataset=parser-v1"
$invoice = $rows | Where-Object { $_.file_id -eq 'wrong-price' }
if (-not $invoice) { throw 'Choose the correct dataset and invoice first.' }
$noteUrl = "$base/api/invoices/$($invoice.id)/note"
$live = Invoke-RestMethod -Method Post "$noteUrl/generate"
if ($live.state -ne 'current') { throw $live.message }
$model = $live.latest.metadata.configuration.requested_model
$live.draft.note
$live.draft.source_references
New-Item -ItemType Directory -Force runtime/notes-verification | Out-Null
$live | ConvertTo-Json -Depth 30 | Set-Content -Encoding utf8 runtime/notes-verification/live-result.json
```

The genuine raw response is already in the note capture directory; validated fields/references are in SQLite. Do not edit the capture or correct the invoice between generation and this replay check.

Stop the server in terminal 1 with Ctrl+C. Remove API-key environment variables and restart it; replay does not load `.env` even if the file remains present:

```powershell
Remove-Item Env:GEMINI_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:GOOGLE_API_KEY -ErrorAction SilentlyContinue
python scripts/run_app.py
```

Then in terminal 2 (which retained `$live`, `$model`, `$noteUrl`):

```powershell
$body = @{ model = $model } | ConvertTo-Json
$replay = Invoke-RestMethod -Method Post "$noteUrl/replay" -ContentType 'application/json' -Body $body
if ($replay.state -ne 'current') { throw $replay.message }
if ($replay.draft.note -cne $live.draft.note) { throw 'Replay draft differs.' }
if (($replay.draft.source_references | ConvertTo-Json -Compress) -cne ($live.draft.source_references | ConvertTo-Json -Compress)) { throw 'Replay references differ.' }
$replay | ConvertTo-Json -Depth 30 | Set-Content -Encoding utf8 runtime/notes-verification/replay-result.json
'Replay matched the genuine saved draft without credentials.'
```

The capture is unchanged, and SQLite gains a separate replay attempt. Offline tests assert zero SDK-client/network/OCR calls and zero dotenv calls in replay. Blocking outgoing network locally is an optional further verification; do not alter captures to obtain a match.

## Actual verification

The complete offline suite passed **131 tests in 21.648 seconds**, exit 0, including 14 new note tests. Existing Starlette httpx deprecation and Windows Gradio event-loop ResourceWarnings remain non-failing. Browser results are recorded separately after the smoke check. No live Gemini request was made for implementation/tests, and no genuine note capture is claimed. New notes tests use isolated databases and actual capture serialization/storage/replay, with only provider network responses mocked and explicitly labeled synthetic.

Browser smoke passed on an isolated database at `runtime/note-smoke/`: reconciled no-note-needed state, saved synthetic draft/source labels, explicit synthetic replay, outdated state after correction, unavailable replay for changed context, and failed local validation without reconciliation changes. Two raw synthetic captures were retained (one valid, one invalid); no live Gemini request occurred. The test server has no unmocked Gemini network path. All 67 protected starter/OCR/extraction-capture files are unchanged, and the original database remains schema-v1 with its one original correction. Details: `runtime/note-smoke/verification.json`. The temporary server was stopped after verification.
