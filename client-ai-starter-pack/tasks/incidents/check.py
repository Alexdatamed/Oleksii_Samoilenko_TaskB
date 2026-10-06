"""Compare a supplied local module with fixed reference cases."""
import argparse, importlib.util, json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('module', help='Path to baseline.py or a separate candidate copy')
parser.add_argument('--case', help='Run one case by ID')
args = parser.parse_args()
root = Path(__file__).resolve().parent
cases = json.loads((root / 'reference-cases.json').read_text())
if args.case:
    cases = [c for c in cases if c['id'] == args.case]
    if not cases: parser.error('Unknown reference case')
try:
    spec = importlib.util.spec_from_file_location('candidate', Path(args.module).resolve())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, 'unit_price')
except Exception as exc:
    print(json.dumps({'load_error': type(exc).__name__, 'message': str(exc)}))
    raise SystemExit(1)
results = []
for case in cases:
    try:
        actual = function(*case['args'])
        results.append(dict(case, actual=actual, passed=actual == case['expected']))
    except Exception as exc:
        results.append(dict(case, error=type(exc).__name__, passed=False))
print(json.dumps(results, indent=2))
raise SystemExit(0 if all(r['passed'] for r in results) else 1)
