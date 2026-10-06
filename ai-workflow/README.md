# AI workflow used for Task B

## Development tools and configuration

OpenAI Codex desktop coding assistant was used for iterative implementation, inspection, PowerShell commands and tests. The session identifies the agent as GPT-6; the exact serving model ID, desktop build, inference parameters and earlier tool versions are not exportable from this conversation. No exact model suffix or version is invented. Development-time AI is separate from the application's Google Gemini integration.

The supplied `client-ai-starter-pack/skills/generate-assignment-data/SKILL.md` was used for fixture preparation: preserve fictional rules, create fixed examples and independently establish expectations. No custom skill, hook or subagent was created. No subagents were used in the six-invoice stage; earlier-stage usage is not independently exportable from the available history. No custom project AGENTS.md is supplied.

Codex host-provided instructions and permissions affected execution: PowerShell on Windows, read-only sandbox with approved write/test commands, restricted network, no credential disclosure. The desktop host provided app tools and browser automation (cua_repl) for local smoke checks. Exact host/plugin builds and full settings are not exportable. This description is a sanitized applicable snapshot; personal paths, credentials, unrelated installed plugins and full user configuration are deliberately omitted. No MCP configuration or permission override needs installation for the submitted application.

## Application models and prompts

The official google-genai SDK 2.28.0 uses models.generate_content. Actual recorded original-four replay model: gemini-3.1-flash-lite. Extraction settings: temperature 0, max_output_tokens 4096, candidate_count 1, JSON structured output; explicit 30000 ms timeout, at most 3 application attempts and one SDK attempt. The user controls GEMINI_MODEL for future live calls. Prompt/schema versions and hashes are in ai_configuration_manifest.json and the genuine captures. Note settings are defined separately in app/discrepancy_notes.py; do not infer a note model from extraction captures.

Prompts live in prompts/ and schemas in app/schemas.py, app/llm_extraction.py and app/discrepancy_notes.py. Rules live in the original domain.md. Credentials belong only in private root .env; .env.example contains names and blank values. dotenv loads without overriding existing environment variables, in live mode only. Dependencies and tested package versions are pinned in requirements.txt; Python 3.12.10 was tested.

An earlier application report (gemini-3.8-flash, 55 tests at that stage) is preserved byte-for-byte in history/application-extraction-earlier.json. It records a separate historical run, not the current demo or a configured model to force. Current full-suite evidence: demo/observed.json, 139 passing tests. Earlier configuration versions are available only where saved captures/snapshots retain them; no complete historical Git history is claimed.

## Workflow example

User instruction excerpt: "If the invoice or receipt SKU does not match the purchase order ... leave expected_cents and difference_cents as None and exclude the case from overcharge totals."

The domain rules and subsequent explicit user clarification affected implementation. A positive billed-total SKU mismatch regression checked that amounts remain unknown and totals exclude the invoice. The later fixed demo independently expects CAB-2 versus ordered/received CAB-1 to be unresolved. Actual OCR text was parsed, imported and compared against fixed expectations; the browser showed unknown expected/difference and the actual PO-4/RC-4. This corrected the risk of presenting an unsupported amount. See tests/test_reconciliation.py, tests/test_demo.py and demo/observed.json. Synthetic corrections/responses in tests are labeled; genuine captures are not modified to match expectations.

## Restore and reproduce

Keep project files at their submitted relative paths. Activate .venv, install requirements and follow the root README. `python demo/run_demo.py load` recreates a separate local DB from saved actual parser evidence without OCR, keys or network. Four-only genuine replay command:

```powershell
$tag = Get-Date -Format 'yyyyMMdd-HHmmss'
python scripts/run_extraction.py --mode replay --input-dir runtime/ocr --output-dir "demo/replay-$tag" --replay-dir demo/captures
```

The intentional missing PO gives exit 1. Replay checks content/configuration hashes and locally validates original responses. No hooks run. Do not restore personal Codex settings; they are unnecessary for application execution.

## Decisions and limits

A small deterministic core and shared Pydantic schema make parser/LLM/replay outputs reviewable. Offline tests and saved genuine responses permit credential-free review. Structured output validates shape, not factual correctness. Two new Gemini captures and mobile checks remain pending; no live access or six-file LLM success is claimed. AI assisted implementation, while independent expectations, full tests and real browser/OCR checks supplied verification. Exact development model/build metadata and unsaved earlier configuration cannot be reconstructed reliably.
