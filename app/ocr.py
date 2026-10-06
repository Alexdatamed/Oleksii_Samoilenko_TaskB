"""CPU EasyOCR through Docling's standard (non-generative) image pipeline."""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class ExtractedDocument:
    markdown: str
    docling_json: dict


@lru_cache(maxsize=1)
def get_converter():
    """Lazily construct one converter, reused for sequential extractions."""
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import EasyOcrOptions, OcrMode, PdfPipelineOptions
    from docling.document_converter import DocumentConverter, ImageFormatOption
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

    options = PdfPipelineOptions(
        do_ocr=True,
        do_table_structure=False,
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU),
        ocr_options=EasyOcrOptions(lang=['en'], use_gpu=False, mode=OcrMode.FULL_PAGE),
    )
    return DocumentConverter(
        allowed_formats=[InputFormat.IMAGE],
        format_options={InputFormat.IMAGE: ImageFormatOption(
            pipeline_cls=StandardPdfPipeline, pipeline_options=options)},
    )


def extract_document(path):
    """Return Markdown and native Docling JSON; raise on failure or empty OCR.

    Only the input image is read. No fixture fields or reconciliation rules are
    used to construct content. The caller controls where exports are saved.
    """
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    from docling.datamodel.base_models import ConversionStatus

    result = get_converter().convert(source, raises_on_error=True)
    if result.status != ConversionStatus.SUCCESS:
        raise RuntimeError(f'Docling conversion status: {result.status}; errors: {result.errors}')
    document = result.document
    has_text = any(item.text.strip() for item in document.texts)
    has_table_text = any(cell.text.strip() for table in document.tables
                         for cell in table.data.table_cells)
    markdown = document.export_to_markdown()
    if not (has_text or has_table_text) or not markdown.strip():
        raise ValueError('Empty OCR content: no recognized text')
    return ExtractedDocument(markdown=markdown, docling_json=document.export_to_dict())
