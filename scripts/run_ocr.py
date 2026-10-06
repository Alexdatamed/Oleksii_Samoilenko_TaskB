"""Process invoice PNGs independently and save OCR exports plus a run summary."""
import argparse
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.ocr import extract_document


def installed_versions():
    versions = {'python': platform.python_version()}
    for package in ('docling', 'docling-slim', 'docling-core', 'easyocr', 'torch', 'torchvision'):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = None
    return versions


def run_batch(input_dir, output_dir):
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    images = sorted(input_dir.glob('*.png'))
    if not images:
        raise ValueError(f'No PNG images found in {input_dir}')
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {'versions': installed_versions(),
               'configuration': {'pipeline': 'StandardPdfPipeline', 'format': 'IMAGE',
                                 'ocr': 'EasyOCR', 'language': 'en', 'device': 'cpu',
                                 'mode': 'full_page', 'table_structure': False},
               'files': []}
    for image in images:
        started = perf_counter()
        entry = {'filename': image.name, 'success': False, 'error': None}
        try:
            document = extract_document(image)
            # Exports are only written after conversion and empty-content checks.
            (output_dir / f'{image.stem}.md').write_text(document.markdown, encoding='utf-8')
            (output_dir / f'{image.stem}.json').write_text(
                json.dumps(document.docling_json, ensure_ascii=False, indent=2), encoding='utf-8')
            entry['success'] = True
        except Exception as exc:
            entry['error'] = f'{type(exc).__name__}: {exc}'
        entry['duration_seconds'] = round(perf_counter() - started, 3)
        summary['files'].append(entry)
        print(f"{image.name}: {'success' if entry['success'] else 'failure'} "
              f"({entry['duration_seconds']}s)" + (f" - {entry['error']}" if entry['error'] else ''),
              flush=True)
        # Checkpoint after each file so completed work remains inspectable.
        (output_dir / 'summary.json').write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path,
                        default=ROOT / 'client-ai-starter-pack/tasks/invoices/images')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'runtime/ocr')
    args = parser.parse_args()
    try:
        summary = run_batch(args.input_dir, args.output_dir)
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0 if all(entry['success'] for entry in summary['files']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
