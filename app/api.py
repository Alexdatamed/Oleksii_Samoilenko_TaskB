"""Local FastAPI factory; Gradio mounted at /ui, API documentation at /docs."""
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator
from starlette.concurrency import run_in_threadpool
from app.application_service import ApplicationService, Settings, ApplicationError, MAX_BYTES, MAX_FILES
from app.schemas import InvoiceFields, ExtractionIssue

Mode = Literal['parser', 'llm', 'replay']
Status = Literal['reconciled', 'discrepant', 'duplicate', 'unresolved']

# Same field types/constraints as extraction; file_id is deliberately not editable.
PatchFields = create_model('PatchFields', __config__=ConfigDict(strict=True, extra='forbid'),
                           **{name: (field.annotation, deepcopy(field))
                              for name, field in InvoiceFields.model_fields.items() if name != 'file_id'})


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    fields: PatchFields
    reason: str = Field(min_length=1)

    @field_validator('reason')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('A correction reason is required')
        return value


class NoteReplayRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    model: str | None = None


class DatasetResponse(BaseModel):
    id: str
    name: str
    mode: Mode
    created_at: str


class SummaryResponse(BaseModel):
    dataset: str
    dataset_id: str
    mode: Mode
    total_invoices: int
    counts_by_status: dict[str, int]
    processing_errors: int
    current_extraction_issue_invoices: int
    overcharge_cents: int


class InvoiceResponse(BaseModel):
    id: str
    dataset: str
    dataset_id: str
    file_id: str
    ordinal: int
    extraction_mode: Mode
    processing_status: Literal['success', 'issues', 'error']
    original_evidence: dict[str, Any]
    original_fields: dict[str, Any] | None
    original_issues: list[dict[str, Any]] | None
    current_fields: InvoiceFields | None
    current_issues: list[ExtractionIssue]
    reconciliation: dict[str, Any] | None
    findings: list[Any]
    supporting_references: dict[str, Any] | None
    reference_tables: dict[str, Any]
    review_state: Literal['unreviewed', 'corrected']
    created_at: str
    updated_at: str
    correction_history: list[dict[str, Any]]
    image_available: bool = False
    image_error: str | None = None


class BatchResponse(BaseModel):
    dataset: str
    mode: Mode
    files: list[dict[str, Any]]
    invoice_ids: dict[str, str]
    summary: SummaryResponse


def create_app(settings: Settings | None = None, *, db_path=None, mount_ui=True):
    if db_path is not None:
        if settings is not None:
            raise ValueError('Use settings or db_path, not both')
        settings = Settings(db_path=db_path)
    service = ApplicationService(settings)
    app = FastAPI(title='Local invoice review', version='1.0')
    app.state.service = service

    @app.exception_handler(ApplicationError)
    async def application_error(request, exc):
        return JSONResponse(status_code=exc.status, content={'detail': str(exc)})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI defaults echo invalid values; never reflect credentials or raw input.
        return JSONResponse(status_code=422, content={'detail': 'Invalid request fields or types'})

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.get('/api/datasets', response_model=list[DatasetResponse])
    def datasets():
        return service.datasets()

    @app.get('/api/invoices', response_model=list[InvoiceResponse])
    def invoices(dataset: str, status: Status | None = None):
        return [service.detail(item['id']) for item in service.invoices(dataset, status)]

    @app.get('/api/invoices/{invoice_id}', response_model=InvoiceResponse)
    def invoice(invoice_id: str):
        return service.detail(invoice_id)

    @app.get('/api/invoices/{invoice_id}/note')
    def note(invoice_id: str):
        return service.note(invoice_id)

    @app.post('/api/invoices/{invoice_id}/note/generate')
    def generate_note(invoice_id: str):
        return service.draft_note(invoice_id, 'llm')

    @app.post('/api/invoices/{invoice_id}/note/replay')
    def replay_note(invoice_id: str, request: NoteReplayRequest = NoteReplayRequest()):
        return service.draft_note(invoice_id, 'replay', request.model)

    @app.get('/api/invoices/{invoice_id}/image')
    def image(invoice_id: str):
        data, media = service.image(invoice_id)
        return Response(data, media_type=media, headers={'Cache-Control': 'no-store'})

    @app.get('/api/summary', response_model=SummaryResponse)
    def summary(dataset: str):
        return service.summary(dataset)

    @app.patch('/api/invoices/{invoice_id}', response_model=InvoiceResponse)
    def correct(invoice_id: str, request: CorrectionRequest):
        return service.correct(invoice_id, request.fields.model_dump(exclude_unset=True), request.reason)

    @app.post('/api/batches', response_model=BatchResponse, status_code=201)
    async def batch(request: Request, dataset: str = Form(...), mode: Mode = Form(...),
                    files: list[UploadFile] = File(...)):
        form = await request.form()
        if set(form) - {'dataset', 'mode', 'files'} or len(form.getlist('dataset')) != 1 or len(form.getlist('mode')) != 1:
            raise ApplicationError(422, 'Unknown or repeated batch fields')
        if not 1 <= len(files) <= MAX_FILES:
            raise ApplicationError(413, 'Use 1–10 files')
        uploads = []
        try:
            for file in files:
                data = await file.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ApplicationError(413, 'Each file must be at most 10 MiB')
                uploads.append((file.filename or 'unnamed', data))
            return await run_in_threadpool(service.batch, dataset, mode, uploads)
        finally:
            for file in files:
                await file.close()

    if mount_ui:
        import gradio as gr
        from app.ui import build_ui
        app = gr.mount_gradio_app(app, build_ui(service), path='/ui', server_name='127.0.0.1',
                                  allowed_paths=[], blocked_paths=[str(service.settings.references_path.resolve()),
                                      str(service.settings.db_path.resolve()),
                                      str((service.settings.capture_dir).resolve()),
                                      str(Path(__file__).resolve().parents[1] / '.env')],
                                  max_file_size=MAX_BYTES, show_error=False, ssr_mode=False,
                                  mcp_server=False, run_history=False, footer_links=[])
        app.state.service = service
    return app
