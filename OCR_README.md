# Invoice OCR stage

This stage reads only invoice image files. It does not read seed invoice fields
or expected results, extract structured invoice fields, or invoke reconciliation.
The supplied starter files and reconciliation implementation are unchanged.

## Install and run (PowerShell, from the project root)

Python 3.12 is recommended for the tested Windows environment. No Tesseract,
CUDA, GPU, or external OCR service is required.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/run_ocr.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The CLI defaults are relative to the project location, even if launched from
another working directory. It processes every PNG in
`client-ai-starter-pack/tasks/invoices/images/`, including the four supplied
invoices. Optional `--input-dir` and `--output-dir` arguments override defaults.

## Configuration and downloads

`app.ocr.extract_document(path)` returns an `ExtractedDocument` with Markdown
and native Docling JSON. A lazy cached converter is reused across sequential
calls. Image support is explicit: `InputFormat.IMAGE`, `ImageFormatOption`, and
`StandardPdfPipeline`. The pipeline uses `PdfPipelineOptions` for images too.
EasyOCR is explicitly English (`lang=['en']`), CPU (`use_gpu=False`), and full
page (`mode=OcrMode.FULL_PAGE`). The pipeline accelerator is also explicitly
CPU. Table structure extraction is disabled because this stage only needs OCR;
Docling's layout analysis remains enabled. No generative/VLM pipeline or
picture-description enrichment is enabled.

The first real conversion downloads Docling layout model artifacts from Hugging
Face and EasyOCR detection and English recognition models from its configured
model hosts (typically GitHub release assets). Internet access and writable
model caches are required. Downloads can be substantial and make the first
file slower. Later conversions reuse cached models. Defaults use the Hugging
Face cache and EasyOCR's user model cache; neither is committed to this project.
If downloads fail, the CLI records the actual error for each file and continues.
Do not infer OCR success from dependency installation alone.

## Outputs and failures

Successful files produce `runtime/ocr/<image-stem>.md` and `.json`. Native JSON
comes from `DoclingDocument.export_to_dict()`, rather than a custom invoice
schema. `runtime/ocr/summary.json` records filename, success, duration in seconds,
and an error for failures, plus installed dependency versions and configuration.
The summary is checkpointed after every file. Durations include conversion,
initial model downloads/initialization when necessary, and export writes.

Conversion errors, partial conversion statuses, and empty recognized text are
failures. Markdown image placeholders alone do not count as recognized text.
Every file is attempted independently; any failure gives CLI exit code 1.
Successful exports from earlier runs are not deleted; always consult the latest
summary before using outputs. Empty input directories fail explicitly.

## Verification

The tests include independent mock-based checks of failure isolation, empty
content rejection, export writing, missing files, and empty input directories.
Mocks establish control flow only, not real OCR accuracy. The original four
reconciliation cases are still tested against the supplied expected results.
Actual OCR run details and tested versions are recorded below and in the runtime
summary when available.

### Actual run on 2026-10-06

Tested on Windows with Python 3.12.10, Docling and docling-slim 2.134.0,
docling-core 2.99.0, EasyOCR 1.7.2, PyTorch 2.14.1, and torchvision 0.29.1.
The installed configuration was inspected: `StandardPdfPipeline`, full-page
mode, English, CPU accelerator, `use_gpu=False`, and converter identity reuse
were confirmed. `pip check` reported no broken requirements.

Real model downloads and OCR completed successfully for all four supplied PNGs:

| File | Success | Seconds |
| --- | --- | ---: |
| clean.png | yes | 58.555 |
| duplicate.png | yes | 21.856 |
| missing-reference.png | yes | 21.928 |
| wrong-price.png | yes | 22.110 |

The first file includes initialization and model download time. All four
Markdown and native Docling JSON exports were saved. The OCR text was left
unaltered, including punctuation recognition errors (for example `Total (USD}`
on the clean image). Conversion success is not a claim of perfect transcription.

Command: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`.
Result: **16 tests passed in 0.063 seconds**, including all 12 unchanged
reconciliation tests and the checks against all four supplied expected results.
