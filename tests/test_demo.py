"""Fixed fictional inputs, authentic saved OCR/parser evidence, isolated demo DBs."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.api import create_app
from app.application_service import ApplicationService, Settings
from demo import run_demo as demo
from demo.additional.render_invoices import dollars


class DemoTests(unittest.TestCase):
    def test_fixed_additional_inputs_and_identifier_integrity(self):
        original=demo.read(demo.SUPPLIED/'seed.json')
        new=demo.read(demo.DEMO/'additional/seed.json')
        for table,key in [('invoices','file_id'),('invoices','invoice_number'),('purchase_orders','po_id'),('receipts','receipt_id')]:
            self.assertFalse({r[key] for r in new[table]} & {r[key] for r in original[table]})
        self.assertEqual(new['invoices'][0],{'file_id':'quantity-mismatch','invoice_number':'INV-4','supplier_id':'S1','po_id':'PO-3','sku':'CAB-1','quantity':6,'unit_cents':2000,'total_cents':12000,'layout':'a'})
        self.assertEqual(new['invoices'][1],{'file_id':'sku-mismatch','invoice_number':'INV-5','supplier_id':'S1','po_id':'PO-4','sku':'CAB-2','quantity':5,'unit_cents':2000,'total_cents':10000,'layout':'b'})
        for inv in new['invoices']:
            po=next(p for p in new['purchase_orders'] if p['po_id']==inv['po_id'])
            receipt=next(r for r in new['receipts'] if r['po_id']==inv['po_id'])
            self.assertEqual((po['supplier_id'],po['quantity'],po['unit_cents'],po['sku']),('S1',5,2000,'CAB-1'))
            self.assertEqual((receipt['quantity'],receipt['sku']),(5,'CAB-1'))

    def test_exact_renderer_amounts(self):
        for cents,expected in [(0,'0.00'),(-1,'-0.01'),(2000,'20.00'),(9007199254740993,'90071992547409.93')]:
            self.assertEqual(dollars(cents),expected)
        with self.assertRaises(ValueError):
            dollars(20.0)

    def test_combined_refs_preserve_original_records_and_manifest_paths(self):
        original=demo.read(demo.SUPPLIED/'seed.json')
        new=demo.read(demo.DEMO/'additional/seed.json')
        combined=demo.read(demo.DEMO/'references.json')
        self.assertEqual(set(combined),{'purchase_orders','receipts'})
        for table in combined:
            self.assertEqual(combined[table],original[table]+new[table])
        manifest=demo.read(demo.DEMO/'manifest.json')
        ids=manifest['effective_processing_order']
        self.assertEqual(len(ids),6)
        self.assertLess(ids.index('clean'),ids.index('duplicate'))
        for row in manifest['files']:
            self.assertFalse(Path(row['image']).is_absolute())
            self.assertFalse(Path(row['original_image']).is_absolute())
            self.assertEqual((demo.ROOT/row['image']).read_bytes(),(demo.ROOT/row['original_image']).read_bytes())
            self.assertEqual(demo.digest(demo.ROOT/row['image']),row['image_sha256'])
        captures=demo.read(demo.DEMO/'captures/manifest.json')['files']
        self.assertEqual({r['file_id'] for r in captures},{'clean','duplicate','missing-reference','wrong-price'})
        for record in captures:
            self.assertEqual(record['origin'],'real_gemini')
            self.assertEqual((demo.ROOT/record['capture']).read_bytes(),(demo.ROOT/record['source']).read_bytes())
            self.assertEqual(demo.digest(demo.ROOT/record['capture']),record['sha256'])

    def test_fixed_answer_key_uses_original_expectations_and_independent_arithmetic(self):
        expected=demo.read(demo.DEMO/'expected.json')
        cases={case['file_id']:case for case in expected['cases']}
        for supplied in demo.read(demo.SUPPLIED/'expected-seed-results.json'):
            for key,value in supplied.items():
                self.assertEqual(cases[supplied['file_id']][key],value)
        quantity=cases['quantity-mismatch']
        self.assertEqual((quantity['billed_cents'],quantity['expected_cents'],quantity['difference_cents']),(12000,10000,2000))
        self.assertEqual(5*2000,10000)
        self.assertEqual(12000-10000,2000)
        self.assertEqual(set(quantity['findings']),{'ordered_quantity_mismatch','received_quantity_mismatch'})
        self.assertEqual(cases['sku-mismatch']['status'],'unresolved')
        self.assertIsNone(cases['sku-mismatch']['expected_cents'])
        self.assertIsNone(cases['sku-mismatch']['difference_cents'])
        self.assertEqual(cases['sku-mismatch']['findings'],['sku_mismatch'])
        self.assertEqual(expected['summary'],{'total_invoices':6,'counts_by_status':{'reconciled':1,'discrepant':2,'duplicate':1,'unresolved':2},'processing_errors':0,'overcharge_cents':4000})

    def test_prepare_in_isolated_workspace_and_refuse_overwrite_collision(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);target=root/'demo';original=root/'supplied'
            shutil.copytree(demo.SUPPLIED/'images',original/'images')
            shutil.copyfile(demo.SUPPLIED/'seed.json',original/'seed.json')
            shutil.copytree(demo.DEMO/'additional',target/'additional')
            with patch.object(demo,'ROOT',root),patch.object(demo,'DEMO',target),patch.object(demo,'SUPPLIED',original):
                first=demo.prepare();second=demo.prepare()
                self.assertEqual(first,second)
                bad=target/'images/clean.png';bad.write_bytes(b'synthetic conflict fixture')
                with self.assertRaises(ValueError):
                    demo.prepare()
                self.assertEqual(bad.read_bytes(),b'synthetic conflict fixture')
                additional=demo.read(target/'additional/seed.json')
                additional['invoices'][0]['invoice_number']='INV-1'
                demo.write(target/'additional/seed.json',additional)
                with self.assertRaises(ValueError):
                    demo.prepare()

    def test_authentic_evidence_keyless_load_idempotence_and_application_images(self):
        if not (demo.DEMO/'latest.json').exists():
            self.skipTest('Real demo OCR evidence not yet available; do not fabricate it')
        run=demo.saved_run()
        self.assertEqual(len(run['evidence']),6)
        origins={r['file_id']:r['ocr_origin'] for r in run['evidence']}
        self.assertEqual(origins['quantity-mismatch'],'actual new Docling OCR')
        self.assertEqual(origins['sku-mismatch'],'actual new Docling OCR')
        with TemporaryDirectory() as directory:
            db=Path(directory)/'demo.sqlite3'
            with patch('google.genai.Client') as network,patch('dotenv.load_dotenv') as dotenv,patch('app.ocr.extract_document') as ocr:
                first=demo.load(db);second=demo.load(db)
                self.assertFalse(first['reimported']);self.assertTrue(second['reimported'])
                self.assertEqual(first['invoice_ids'],second['invoice_ids'])
                result=demo.verify(db)
                self.assertTrue(result['passed'],result['failures'])
                network.assert_not_called();dotenv.assert_not_called();ocr.assert_not_called()
            settings=Settings(db_path=db,image_dir=demo.DEMO/'images',references_path=demo.DEMO/'references.json',runtime_dir=Path(directory)/'review',capture_dir=Path(directory)/'captures')
            service=ApplicationService(settings)
            self.assertEqual(service.summary(demo.DATASET)['overcharge_cents'],4000)
            with TestClient(create_app(settings,mount_ui=False)) as client:
                for invoice_id in first['invoice_ids'].values():
                    row=service.detail(invoice_id)
                    self.assertTrue(row['image_available'])
                    self.assertIsNotNone(row['original_evidence']['ocr']['text'])
                    self.assertEqual(row['original_evidence']['availability_notes'],[])
                    image=client.get('/api/invoices/'+invoice_id+'/image')
                    self.assertEqual(image.status_code,200)
                    self.assertEqual(image.content,(demo.DEMO/'images'/(row['file_id']+'.png')).read_bytes())

    def test_verifier_detects_changed_results_and_runtime_database_is_protected(self):
        with self.assertRaises(ValueError):demo.load(demo.DEFAULT_DB)
        with self.assertRaises(ValueError):demo.verify(demo.DEFAULT_DB)
        if not (demo.DEMO/'latest.json').exists():
            self.skipTest('Real OCR evidence unavailable')
        with TemporaryDirectory() as directory:
            db=Path(directory)/'demo.sqlite3'
            imported=demo.load(db)
            service=ApplicationService(Settings(db_path=db,image_dir=demo.DEMO/'images'))
            service.correct(imported['invoice_ids']['quantity-mismatch'],{'quantity':5,'total_cents':10000},'Synthetic verifier regression test in isolated database')
            result=demo.verify(db)
            self.assertFalse(result['passed'])
            self.assertTrue(any('quantity-mismatch' in failure for failure in result['failures']))
            self.assertEqual(result['summary']['overcharge_cents'],2000)
            with redirect_stdout(StringIO()):
                self.assertEqual(demo.main(['verify','--db',str(db)]),1)

    def test_all_original_files_and_existing_runtime_evidence_unchanged(self):
        result=demo.preservation()
        self.assertTrue(result['passed'],result['changed'])
        self.assertGreaterEqual(result['protected_files'],164)


if __name__=='__main__':
    unittest.main()
