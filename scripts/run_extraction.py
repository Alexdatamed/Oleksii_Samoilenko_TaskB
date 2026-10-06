"""Extract invoice fields from OCR Markdown in parser, llm, or replay mode."""
import argparse
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.extraction import extract_invoice_fields
from app.llm_extraction import ExtractionError, GeminiExtractor, ReplayExtractor, RequestConfiguration


def run_batch(input_dir, output_dir, mode='parser', replay_dir=None, replay_model=None):
    input_dir, output_dir = Path(input_dir).resolve(), Path(output_dir).resolve()
    capture_dir = Path(replay_dir or ROOT / 'runtime/extraction/captures').resolve()
    if output_dir == input_dir or input_dir in output_dir.parents:
        raise ValueError('Extraction output must be separate from the OCR input directory')
    if mode not in ('parser', 'llm', 'replay'):
        raise ValueError('Unsupported extraction mode')
    if mode != 'parser' and (capture_dir == input_dir or input_dir in capture_dir.parents
                             or capture_dir == output_dir or output_dir in capture_dir.parents
                             or capture_dir in output_dir.parents):
        raise ValueError('Capture directory must be separate from OCR inputs and validated outputs')
    previous = output_dir / 'summary.json'
    if previous.is_file() and json.loads(previous.read_text(encoding='utf-8')).get('mode') != mode:
        raise ValueError('Output directory already contains results for a different mode')
    files = sorted(input_dir.glob('*.md'))
    if not files:
        raise ValueError(f'No Markdown files found in {input_dir}')
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {'mode': mode, 'files': []}
    extractor = ReplayExtractor(capture_dir, model=replay_model) if mode == 'replay' else None
    try:
        for source in files:
            started = perf_counter()
            record = {'mode': mode, 'source': str(source) if mode == 'parser' else source.name,
                      'file_id': source.stem, 'status': 'error', 'fields': None,
                      'issues': [], 'metadata': {}, 'error': None}
            try:
                markdown = source.read_text(encoding='utf-8-sig')
                if mode == 'parser':
                    fields, issues = extract_invoice_fields(markdown, source.stem)
                else:
                    if extractor is None:
                        extractor = GeminiExtractor(capture_dir)
                    result = extractor.extract(markdown, source.stem)
                    fields, issues, record['metadata'] = result.fields, result.issues, result.metadata
                record['fields'] = fields.model_dump()
                record['issues'] = [issue.model_dump() for issue in issues]
                record['status'] = 'issues' if issues else 'success'
            except Exception as exc:
                if isinstance(exc, ExtractionError):
                    record['error'], record['metadata'] = str(exc), exc.metadata
                else:
                    record['error'] = f'{type(exc).__name__}: {exc}' if mode == 'parser' else f'Processing failed ({type(exc).__name__})'
            record['duration_seconds'] = round(perf_counter() - started, 3)
            try:
                (output_dir / f'{source.name}.json').write_text(
                    json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            except Exception as exc:
                record['status'] = 'error'
                record['error'] = f'Output write failed ({type(exc).__name__})'
            summary['files'].append(record)
            print(f"{source.name}: {record['status']} ({len(record['issues'])} issues)"
                  + (f" - {record['error']}" if record['error'] else ''), flush=True)
            (output_dir / 'summary.json').write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        if mode == 'llm' and extractor is not None:
            try:
                extractor.close()
            except Exception:
                pass
    models = sorted({r['metadata'].get('requested_model') for r in summary['files']
                     if r['metadata'].get('requested_model')})
    if models:
        manifest = {'mode': mode, 'configurations': [RequestConfiguration(m).bundle()[2] for m in models],
                    'returned_models': sorted({r['metadata']['returned_model'] for r in summary['files']
                                               if r['metadata'].get('returned_model')})}
        (output_dir / 'ai_configuration.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['parser', 'llm', 'replay'], default='parser')
    parser.add_argument('--input-dir', type=Path, default=ROOT / 'runtime/ocr')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--replay-dir', type=Path, default=ROOT / 'runtime/extraction/captures',
                        help='Raw capture directory: written by llm, read by replay')
    parser.add_argument('--replay-model', help='Optionally require captures from this requested model')
    args = parser.parse_args(argv)
    output_dir = args.output_dir or ROOT / 'runtime/extraction' / args.mode
    try:
        summary = run_batch(args.input_dir, output_dir, args.mode, args.replay_dir, args.replay_model)
    except Exception as exc:
        error = str(exc) if isinstance(exc, (ExtractionError, ValueError)) else f'Batch failed ({type(exc).__name__})'
        print(error, file=sys.stderr)
        return 1
    return 0 if all(record['status'] == 'success' for record in summary['files']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
