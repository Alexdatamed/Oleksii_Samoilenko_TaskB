"""Operation-scoped SQLite ownership and bounded, sequential image batches.

API and Gradio call this same service. No OCR/network call holds a DB connection.
"""
from dataclasses import dataclass
from io import BytesIO
import json
import os
import sqlite3
from pathlib import Path
from threading import Lock
from time import perf_counter
from uuid import uuid4

from PIL import Image
from pydantic import ValidationError
from app.extraction import extract_invoice_fields
from app.invoice_service import InvoiceService, ROOT, digest, reject_credentials
from app.llm_extraction import GeminiExtractor, ReplayExtractor, ExtractionError
from app.ocr import extract_document
from app.schemas import InvoiceFields, ExtractionIssue
from app.storage import InvoiceStore, DEFAULT_DB
from app.discrepancy_notes import GeminiNotes, ReplayNotes, NoteUnavailable
from app.note_service import NoteService

BATCH_LOCK = Lock()  # One process/worker; protects the shared Docling converter.
MAX_FILES, MAX_BYTES, MAX_PIXELS = 10, 10 * 1024 * 1024, 20_000_000


class ApplicationError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Settings:
    db_path: Path = DEFAULT_DB
    runtime_dir: Path = ROOT / 'runtime/review'
    references_path: Path = ROOT / 'client-ai-starter-pack/tasks/invoices/seed.json'
    image_dir: Path = ROOT / 'client-ai-starter-pack/tasks/invoices/images'
    capture_dir: Path = ROOT / 'runtime/extraction/captures'

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, Path(getattr(self, name)).resolve())


class ApplicationService:
    def __init__(self, settings=None, *, gemini_factory=None, replay_factory=None, note_factory=None, note_replay_factory=None):
        self.settings = settings or Settings()
        # Optional dependency injection for tests; UI/API use production defaults.
        self.gemini_factory = gemini_factory
        self.replay_factory = replay_factory
        self.note_factory = note_factory
        self.note_replay_factory = note_replay_factory

    def _operation(self, callback):
        try:
            with InvoiceStore(self.settings.db_path) as store:
                return callback(InvoiceService(store))
        except ApplicationError:
            raise
        except KeyError:
            raise ApplicationError(404, 'Invoice or dataset not found') from None
        except (ValueError, ValidationError, TypeError):
            raise ApplicationError(422, 'Invalid invoice data or correction') from None
        except sqlite3.OperationalError:
            raise ApplicationError(503, 'Storage unavailable or busy; retry later') from None
        except Exception:
            raise ApplicationError(500, 'Storage operation failed') from None

    def datasets(self):
        return self._operation(lambda service: [dict(row) for row in service.store.connection.execute(
            'SELECT id,name,mode,created_at FROM datasets ORDER BY created_at,name')])

    def invoices(self, dataset, status=None):
        return self._operation(lambda service: service.list_invoices(dataset, status))

    def detail(self, invoice_id):
        detail = self._operation(lambda service: service.get_invoice(invoice_id))
        try:
            self._image_bytes(detail)
            detail['image_available'], detail['image_error'] = True, None
        except ApplicationError as exc:
            detail['image_available'], detail['image_error'] = False, str(exc)
        return detail

    def summary(self, dataset):
        return self._operation(lambda service: service.summary(dataset))

    def correct(self, invoice_id, patch, reason):
        self._operation(lambda service: service.correct_invoice(invoice_id, patch, reason))
        return self.detail(invoice_id)

    @property
    def note_capture_dir(self):
        # Extraction searches hash folders only; this namespace is separate.
        return self.settings.capture_dir / 'notes'

    def note(self, invoice_id):
        return self._operation(lambda service: NoteService(service).get(invoice_id))

    def draft_note(self, invoice_id, mode='llm', model=None):
        if mode not in ('llm', 'replay') or (mode == 'llm' and model is not None):
            raise ApplicationError(422, 'Use live generation or offline replay; live mode uses the configured model')
        context = self._operation(lambda service: NoteService(service).snapshot(invoice_id))
        if context['status'] in (None, 'reconciled'):
            return self.note(invoice_id)
        provider, draft, meta, state = None, None, {}, 'failed'
        try:
            if mode == 'llm':
                provider = (self.note_factory or GeminiNotes)(self.note_capture_dir)
            else:
                provider = (self.note_replay_factory or ReplayNotes)(self.note_capture_dir, model=model or os.environ.get('GEMINI_MODEL') or None)
            draft, meta = provider.generate(context)
            state = 'ready'
        except NoteUnavailable:
            state = 'unavailable'
        except ExtractionError as exc:
            meta = exc.metadata
        except Exception:
            meta = {}  # Never expose raw provider/path errors or credentials.
        finally:
            if provider is not None and hasattr(provider, 'close'):
                try:
                    provider.close()
                except Exception:
                    pass  # Closing a transport must not expose provider exceptions.
        return self._operation(lambda service: NoteService(service).save(invoice_id, context, mode, state, draft, meta))

    def _image_bytes(self, detail):
        evidence = detail['original_evidence']['image']
        reference = evidence.get('reference')
        if not reference or not evidence.get('sha256') or not evidence.get('available'):
            raise ApplicationError(404, 'Original image unavailable')
        path = Path(reference).resolve()
        roots = [self.settings.image_dir.resolve(), (self.settings.runtime_dir / 'batches').resolve()]
        if not any(path.is_relative_to(root) for root in roots):
            raise ApplicationError(404, 'Original image outside configured asset directories')
        try:
            data = path.read_bytes()
            if len(data) > MAX_BYTES or digest(data) != evidence['sha256']:
                raise ApplicationError(409, 'Original image changed or exceeds size limit')
            with Image.open(BytesIO(data)) as image:
                if image.format not in ('PNG', 'JPEG') or image.width * image.height > MAX_PIXELS:
                    raise ApplicationError(404, 'Original image has an unsupported format')
                image.verify()
                media = 'image/png' if image.format == 'PNG' else 'image/jpeg'
            return data, media
        except ApplicationError:
            raise
        except Exception:
            raise ApplicationError(404, 'Original image unavailable or invalid') from None

    def image(self, invoice_id):
        return self._image_bytes(self._operation(lambda service: service.get_invoice(invoice_id)))

    def _replay_ocr(self, image_hash):
        # Immutable imported evidence binds exact image bytes to exact OCR text.
        def lookup(service):
            rows = service.store.connection.execute('SELECT id FROM invoices ORDER BY created_at,id').fetchall()
            texts = set()
            for row in rows:
                detail = service.get_invoice(row['id'])
                evidence = detail['original_evidence']
                if evidence['image'].get('sha256') != image_hash:
                    continue
                try:
                    data, _ = self._image_bytes(detail)
                except ApplicationError:
                    continue
                ocr = evidence['ocr']
                text = ocr.get('text')
                if digest(data) == image_hash and isinstance(text, str) and text.strip() and digest(text.encode()) == ocr.get('sha256'):
                    texts.add(text)
            if len(texts) != 1:
                raise ApplicationError(422, 'Replay requires one verified saved image/OCR association')
            return texts.pop()
        return self._operation(lookup)

    def _capture_storage(self):
        """One shared store, with non-destructive import of legacy batch captures.

        Keep originals and immutable evidence references. Exclusive creation
        prevents overwriting captures; a conflicting filename is an explicit error.
        Called only during an explicitly requested LLM/replay batch, not startup.
        """
        root = self.settings.capture_dir
        batches = (self.settings.runtime_dir / 'batches').resolve()
        for source in sorted(batches.glob('*/captures/*/*.json')):
            if not source.resolve().is_relative_to(batches):
                continue
            if source.name.endswith(('.capture.json', '.failure.json')):
                destination = root / source.parent.name / source.name
                data = source.read_bytes()
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with destination.open('xb') as stream:
                        stream.write(data)
                except FileExistsError:
                    if destination.read_bytes() != data:
                        raise ApplicationError(409, 'Conflicting legacy capture; originals preserved') from None
        return root

    def batch(self, dataset, mode, uploads, progress=None):
        try:
            return self._batch(dataset, mode, uploads, progress)
        except ApplicationError:
            raise
        except Exception:
            raise ApplicationError(500, 'Batch processing or persistence failed; evidence retained when available') from None

    def _batch(self, dataset, mode, uploads, progress=None):
        """uploads = [(original_filename, bytes)]; stable basename order then input index.

        Every upload receives its own ordinal file_id. Per-file errors persist.
        A completed batch can contain processing errors; never silently fallback.
        """
        if mode not in ('parser', 'llm', 'replay') or not isinstance(dataset, str) or not dataset.strip():
            raise ApplicationError(422, 'A dataset name and explicit extraction mode are required')
        try:
            reject_credentials(dataset)
        except ValueError:
            raise ApplicationError(422, 'Invalid dataset name') from None
        if not uploads or len(uploads) > MAX_FILES or any(len(data) > MAX_BYTES for _, data in uploads):
            raise ApplicationError(413, 'Use 1вЂ“10 files, at most 10 MiB each')
        if any(item['name'] == dataset for item in self.datasets()):
            raise ApplicationError(409, 'Dataset name already exists; choose a new run name')
        if not BATCH_LOCK.acquire(blocking=False):
            raise ApplicationError(409, 'Another local batch is processing')
        extractor = None
        try:
            folder = self.settings.runtime_dir / 'batches' / uuid4().hex
            images, ocr, results = [folder / name for name in ('images', 'ocr', mode)]
            for path in (images, ocr, results):
                path.mkdir(parents=True, exist_ok=False)
            captures = self._capture_storage() if mode in ('llm', 'replay') else self.settings.capture_dir
            summary = {'mode': mode, 'provenance': 'uploaded image batch', 'files': []}
            ordered = sorted(enumerate(uploads), key=lambda pair: (pair[1][0].replace('\\', '/').split('/')[-1], pair[0]))
            for ordinal, (_, (filename, data)) in enumerate(ordered):
                start = perf_counter()
                file_id = f'{ordinal:04d}'
                record = {'file_id': file_id, 'original_filename': filename, 'mode': mode,
                          'source': str(ocr / (file_id + '.md')), 'image_sha256': digest(data),
                          'status': 'error', 'fields': None, 'issues': [], 'metadata': {}, 'error': None}
                stage = 'Image validation'
                try:
                    reject_credentials(filename)
                    with Image.open(BytesIO(data)) as image:
                        if image.format not in ('PNG', 'JPEG') or image.width * image.height > MAX_PIXELS:
                            raise ValueError('Unsupported image')
                        suffix = '.png' if image.format == 'PNG' else '.jpg'
                        image.verify()
                    # Original bytes, safe internal filename; display name is evidence only.
                    image_path = images / (file_id + suffix)
                    image_path.write_bytes(data)
                    record['image_reference'] = image_path.name
                    stage = 'Replay OCR association' if mode == 'replay' else 'OCR'
                    if mode == 'replay':
                        markdown = self._replay_ocr(digest(data))
                    else:
                        document = extract_document(image_path)
                        markdown = document.markdown
                        (ocr / (file_id + '.docling.json')).write_text(json.dumps(document.docling_json), encoding='utf-8')
                    if not markdown.strip():
                        raise ApplicationError(422, 'Empty OCR content')
                    (ocr / (file_id + '.md')).write_text(markdown, encoding='utf-8')
                    record['metadata']['ocr_sha256'] = digest(markdown.encode())
                    stage = 'Extraction'
                    if mode == 'parser':
                        fields, issues = extract_invoice_fields(markdown, file_id)
                    else:
                        if extractor is None:
                            factory = (self.replay_factory or ReplayExtractor) if mode == 'replay' else (self.gemini_factory or GeminiExtractor)
                            extractor = factory(captures)
                        result = extractor.extract(markdown, file_id)
                        fields, issues = result.fields, result.issues
                        record['metadata'].update(result.metadata)
                    fields = InvoiceFields.model_validate(fields.model_dump())
                    if fields.file_id != file_id:
                        raise ApplicationError(422, 'Extractor returned a conflicting caller file_id')
                    issues = [ExtractionIssue.model_validate(issue.model_dump()) for issue in issues]
                    record.update(fields=fields.model_dump(), issues=[i.model_dump() for i in issues],
                                  status='issues' if issues else 'success')
                except Exception as exc:
                    record['error'] = f'{stage} failed ({type(exc).__name__})'
                    if isinstance(exc, (ApplicationError, ExtractionError)):
                        record['error'] += ': ' + str(exc)
                    if isinstance(exc, ExtractionError):
                        record['metadata'].update(exc.metadata)
                    # Never retain credential-bearing filenames or exception messages.
                    try:
                        reject_credentials(record)
                    except ValueError:
                        record.update(original_filename='[redacted filename]', metadata={}, fields=None, issues=[],
                                      status='error', error=f'{stage} failed: credential-bearing evidence refused')
                record['duration_seconds'] = round(perf_counter() - start, 3)
                (results / (file_id + '.json')).write_text(json.dumps(record, indent=2), encoding='utf-8')
                summary['files'].append(record)
                if progress:
                    progress(ordinal + 1, len(ordered), record['status'])
            summary_path = results / 'summary.json'
            summary_path.write_text(json.dumps(summary, indent=2), encoding='utf-8')
            def persist(service):
                if service.store.connection.execute('SELECT 1 FROM datasets WHERE name=?', (dataset,)).fetchone():
                    raise ApplicationError(409, 'Dataset name already exists; batch evidence retained')
                return service.import_results(summary_path, self.settings.references_path, dataset=dataset, mode=mode,
                                              ocr_dir=ocr, image_dir=images,
                                              capture_dir=captures)
            imported = self._operation(persist)
            return {'dataset': dataset, 'mode': mode, 'files': summary['files'],
                    'invoice_ids': imported['invoice_ids'], 'summary': imported['summary']}
        finally:
            if mode == 'llm' and extractor is not None:
                try:
                    extractor.close()
                except Exception:
                    pass
            BATCH_LOCK.release()
