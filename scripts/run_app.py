"""Run the local review app; one worker owns the sequential batch lock."""
import argparse
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.api import create_app
from app.application_service import Settings
import uvicorn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    defaults = Settings()
    parser.add_argument('--db', type=Path, default=defaults.db_path)
    parser.add_argument('--runtime-dir', type=Path, default=defaults.runtime_dir)
    parser.add_argument('--references', type=Path, default=defaults.references_path)
    parser.add_argument('--image-dir', type=Path, default=defaults.image_dir)
    parser.add_argument('--capture-dir', type=Path, default=defaults.capture_dir)
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    settings = Settings(db_path=args.db, runtime_dir=args.runtime_dir, references_path=args.references,
                        image_dir=args.image_dir, capture_dir=args.capture_dir)
    uvicorn.run(create_app(settings), host='127.0.0.1', port=args.port, workers=1)


if __name__ == '__main__':
    main()
