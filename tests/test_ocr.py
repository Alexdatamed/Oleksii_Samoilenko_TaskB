from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.ocr import ExtractedDocument, extract_document
from scripts.run_ocr import run_batch


class OcrTests(unittest.TestCase):
    def test_batch_continues_after_failure_and_saves_exports(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'images'
            images.mkdir()
            for name in ('a.png', 'b.png', 'c.png', 'd.png'):
                (images / name).touch()
            document = ExtractedDocument('Recognized invoice', {'texts': [{'text': 'Recognized invoice'}]})
            with patch('scripts.run_ocr.extract_document', side_effect=[
                    ValueError('Empty OCR content'), document, RuntimeError('conversion failed'), document]) as extract:
                summary = run_batch(images, root / 'outputs')
            self.assertEqual(extract.call_count, 4)
            self.assertEqual([f['success'] for f in summary['files']], [False, True, False, True])
            self.assertIn('Empty OCR content', summary['files'][0]['error'])
            self.assertTrue(all(f['duration_seconds'] >= 0 for f in summary['files']))
            self.assertTrue((root / 'outputs/summary.json').is_file())
            self.assertEqual((root / 'outputs/b.md').read_text(), 'Recognized invoice')
            self.assertTrue((root / 'outputs/d.json').is_file())
            self.assertFalse((root / 'outputs/a.md').exists())

    def test_empty_recognized_text_is_failure_even_with_markdown_placeholder(self):
        status = SimpleNamespace(SUCCESS='success')
        fake_module = SimpleNamespace(ConversionStatus=status)
        document = SimpleNamespace(texts=[], tables=[], export_to_markdown=lambda: '<!-- image -->')
        converter = SimpleNamespace(convert=lambda *a, **kw: SimpleNamespace(
            status='success', document=document))
        with TemporaryDirectory() as directory:
            image = Path(directory) / 'empty.png'
            image.touch()
            with patch.dict('sys.modules', {'docling.datamodel.base_models': fake_module}), \
                    patch('app.ocr.get_converter', return_value=converter):
                with self.assertRaisesRegex(ValueError, 'Empty OCR content'):
                    extract_document(image)

    def test_missing_file_fails_before_converter_initialization(self):
        with TemporaryDirectory() as directory, patch('app.ocr.get_converter') as get_converter:
            with self.assertRaises(FileNotFoundError):
                extract_document(Path(directory) / 'missing.png')
            get_converter.assert_not_called()

    def test_no_images_is_explicit_failure(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'No PNG images'):
                run_batch(directory, Path(directory) / 'outputs')
