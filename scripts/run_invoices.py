"""Offline SQLite invoice import, inspection, and audited corrections."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.invoice_service import InvoiceService, read_json
from app.storage import DEFAULT_DB, InvoiceStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init')
    importer = commands.add_parser('import')
    importer.add_argument('--dataset', required=True)
    importer.add_argument('--mode', choices=['parser', 'llm', 'replay'], required=True)
    importer.add_argument('--summary', type=Path, required=True)
    importer.add_argument('--references', type=Path, required=True)
    importer.add_argument('--ocr-dir', type=Path)
    importer.add_argument('--image-dir', type=Path)
    importer.add_argument('--capture-dir', type=Path)
    listing = commands.add_parser('list')
    listing.add_argument('--dataset', required=True)
    listing.add_argument('--status', choices=['reconciled', 'discrepant', 'duplicate', 'unresolved'])
    inspection = commands.add_parser('show')
    inspection.add_argument('--id', required=True)
    correction = commands.add_parser('correct')
    correction.add_argument('--id', required=True)
    correction.add_argument('--patch', type=Path, required=True, help='JSON object with canonical fields to update')
    correction.add_argument('--reason', required=True)
    summary = commands.add_parser('summary')
    summary.add_argument('--dataset', required=True)
    args = parser.parse_args(argv)
    try:
        with InvoiceStore(args.db) as store:
            service = InvoiceService(store)
            if args.command == 'init':
                result = {'initialized': True, 'database': str(args.db)}
            elif args.command == 'import':
                result = service.import_results(args.summary, args.references, dataset=args.dataset, mode=args.mode,
                                                ocr_dir=args.ocr_dir, image_dir=args.image_dir, capture_dir=args.capture_dir)
            elif args.command == 'list':
                result = service.list_invoices(args.dataset, args.status)
            elif args.command == 'show':
                result = service.get_invoice(args.id)
            elif args.command == 'correct':
                result = service.correct_invoice(args.id, read_json(args.patch), args.reason)
            else:
                result = service.summary(args.dataset)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # No raw input/credential-bearing validation errors or database strings.
        message = str(exc) if type(exc) in (ValueError, KeyError) else type(exc).__name__
        print(f'Invoice operation failed: {message}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
