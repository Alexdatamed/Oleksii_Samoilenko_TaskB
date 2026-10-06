"""Prepare, process, keylessly load, and independently verify the six-invoice demo.

All writes are demo-local (or an explicitly separate test database). Expectations
are read only by verification. Seed invoice values never enter OCR/extraction.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.invoice_service import InvoiceService
from app.storage import InvoiceStore, DEFAULT_DB

DEMO = ROOT/'demo'
SUPPLIED = ROOT/'client-ai-starter-pack/tasks/invoices'
DATASET = 'demo-six-parser'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def relative(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


def write(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def copy_preserving(source,destination):
    """Exact bytes; conflicting prior evidence is never silently overwritten."""
    source,destination=Path(source),Path(destination)
    data=source.read_bytes()
    if destination.exists():
        if destination.read_bytes()!=data:
            raise ValueError('Destination differs from source; preserve it and choose a new demo snapshot')
    else:
        destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open('xb') as handle:
            handle.write(data)


def prepare():
    original, additional = read(SUPPLIED/'seed.json'), read(DEMO/'additional/seed.json')
    for table,key in [('invoices','file_id'),('invoices','invoice_number'),('purchase_orders','po_id'),('receipts','receipt_id')]:
        new=[row[key] for row in additional[table]]
        if len(new)!=len(set(new)) or set(new)&{row[key] for row in original[table]}:
            raise ValueError('Additional identifiers collide with supplied records')
    references={key:original[key]+additional[key] for key in ('purchase_orders','receipts')}
    # No invoice field values are included in the reference input to reconciliation.
    write(DEMO/'references.json',references)
    sources={inv['file_id']:SUPPLIED/'images'/(inv['file_id']+'.png') for inv in original['invoices']}
    sources.update({inv['file_id']:DEMO/'additional/images'/(inv['file_id']+'.png') for inv in additional['invoices']})
    ids=sorted(sources)
    if len(ids)!=6 or ids.index('clean')>=ids.index('duplicate'):
        raise ValueError('The six-case order must keep clean before duplicate')
    records=[]
    for file_id in ids:
        target=DEMO/'images'/(file_id+'.png')
        copy_preserving(sources[file_id],target)
        records.append({'file_id':file_id,'image':relative(target),'original_image':relative(sources[file_id]),
                        'image_sha256':digest(target),'fixture_kind':'fictional invoice',
                        'layout_source':'supplied' if file_id in {inv['file_id'] for inv in original['invoices']} else 'additional'})
    manifest={'version':1,'dataset':DATASET,'extraction_mode':'parser','effective_processing_order':ids,
              'references':'demo/references.json','references_sha256':digest(DEMO/'references.json'),'files':records,
              'application_configuration':{'db':'demo/invoices.sqlite3','references':'demo/references.json',
                  'image_dir':'demo/images','runtime_dir':'demo/review','capture_dir':'demo/captures'},
              'evidence_policy':'Fictional source images; actual saved OCR and actual parser outputs. No provider responses are fabricated.'}
    write(DEMO/'manifest.json',manifest)
    return manifest


def process():
    """Run real OCR for two new images; reuse authentic saved OCR for supplied four."""
    from scripts.run_ocr import run_batch as run_ocr
    from scripts.run_extraction import run_batch as run_parser
    manifest=prepare()
    folder=DEMO/'runs'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid4().hex[:8])
    ocr_dir,parser_dir=folder/'ocr',folder/'parser'
    ocr_dir.mkdir(parents=True,exist_ok=False)
    new_summary=run_ocr(DEMO/'additional/images',folder/'additional-ocr')
    original_summary=read(ROOT/'runtime/ocr/summary.json')
    original_results={r['filename']:r for r in original_summary['files']}
    new_results={r['filename']:r for r in new_summary['files']}
    evidence=[]
    errors={}
    for record in manifest['files']:
        file_id=record['file_id']
        supplied=record['layout_source']=='supplied'
        source_dir=ROOT/'runtime/ocr' if supplied else folder/'additional-ocr'
        result=(original_results if supplied else new_results).get(file_id+'.png',{})
        item={'file_id':file_id,'image':record['image'],'image_sha256':record['image_sha256'],
              'ocr_origin':'reused actual saved Docling OCR' if supplied else 'actual new Docling OCR',
              'source_summary':relative(ROOT/'runtime/ocr/summary.json' if supplied else folder/'additional-ocr/summary.json'),
              'ocr_result':result,'artifacts':[]}
        try:
            if not result.get('success'):
                raise ValueError('Authentic successful OCR output is unavailable')
            for suffix in ('.md','.json'):
                source=source_dir/(file_id+suffix)
                destination=ocr_dir/(file_id+suffix)
                copy_preserving(source,destination)
                item['artifacts'].append({'path':relative(destination),'sha256':digest(destination),'source':relative(source)})
            if not (ocr_dir/(file_id+'.md')).read_text(encoding='utf-8-sig').strip():
                raise ValueError('Saved OCR content is empty')
        except (ValueError,OSError) as exc:
            errors[file_id]='OCR evidence unavailable ('+type(exc).__name__+')'
        evidence.append(item)
    summary=run_parser(ocr_dir,parser_dir,mode='parser') if list(ocr_dir.glob('*.md')) else {'mode':'parser','files':[]}
    records={r['file_id']:r for r in summary['files']}
    for source in manifest['files']:
        file_id=source['file_id']
        record=records.get(file_id,{'file_id':file_id,'mode':'parser','status':'error','fields':None,'issues':[], 'metadata':{},'error':errors.get(file_id,'OCR evidence missing')})
        if file_id in errors:
            record.update(status='error',fields=None,error=errors[file_id])
        # Portable source references and provenance do not change parsed fields/issues.
        record['source']=relative(ocr_dir/(file_id+'.md'))
        record['image_reference']=file_id+'.png'
        record['image_sha256']=source['image_sha256']
        if (ocr_dir/(file_id+'.md')).is_file():
            text=(ocr_dir/(file_id+'.md')).read_text(encoding='utf-8-sig')
            record.setdefault('metadata',{})['ocr_sha256']=hashlib.sha256(text.encode('utf-8')).hexdigest()
        write(parser_dir/(file_id+'.md.json'),record)
        records[file_id]=record
    summary.update(provenance='Fictional invoice fixtures; actual Docling OCR and deterministic parser; no Gemini responses',files=[records[file_id] for file_id in manifest['effective_processing_order']])
    write(parser_dir/'summary.json',summary)
    run={'version':1,'manifest':manifest,'evidence':evidence,'summary':relative(parser_dir/'summary.json'),
         'summary_sha256':digest(parser_dir/'summary.json'),'ocr_dir':relative(ocr_dir),
         'parser_files':[{'path':relative(path),'sha256':digest(path)} for path in sorted(parser_dir.glob('*.md.json'))]}
    write(folder/'run.json',run)
    write(DEMO/'latest.json',{'run':relative(folder/'run.json'),'sha256':digest(folder/'run.json')})
    return {'run':relative(folder/'run.json'),'processing_errors':sum(r['status']=='error' for r in summary['files']),
            'extraction_issues':sum(bool(r['issues']) for r in summary['files'])}


def saved_run():
    pointer=read(DEMO/'latest.json')
    run_path=ROOT/pointer['run']
    if not run_path.resolve().is_relative_to((DEMO/'runs').resolve()) or digest(run_path)!=pointer['sha256']:
        raise ValueError('Saved demo run integrity mismatch')
    run=read(run_path)
    assets=[{'path':run['summary'],'sha256':run['summary_sha256']},
            {'path':run['manifest']['references'],'sha256':run['manifest']['references_sha256']}]
    assets+=run['parser_files']
    assets += [{'path':r['image'],'sha256':r['image_sha256']} for r in run['manifest']['files']]
    assets += [artifact for item in run['evidence'] for artifact in item['artifacts']]
    for asset in assets:
        path=(ROOT/asset['path']).resolve()
        if not path.is_relative_to(DEMO.resolve()) or digest(path)!=asset['sha256']:
            raise ValueError('Saved demo evidence integrity mismatch')
    return run


def load(database=DEMO/'invoices.sqlite3',dataset=DATASET):
    database=Path(database).resolve()
    if database==DEFAULT_DB.resolve() or database.is_relative_to((ROOT/'runtime').resolve()):
        raise ValueError('Use a separate demo database; existing runtime databases are protected')
    run=saved_run()
    with InvoiceStore(database) as store:
        return InvoiceService(store).import_results(ROOT/run['summary'],ROOT/run['manifest']['references'],dataset=dataset,
            mode='parser',ocr_dir=ROOT/run['ocr_dir'],image_dir=DEMO/'images',capture_dir=DEMO/'captures')


def verify(database=DEMO/'invoices.sqlite3',dataset=DATASET):
    """Compare actual stored outcomes to a fixed answer key; never write the key."""
    database=Path(database).resolve()
    if database==DEFAULT_DB.resolve() or database.is_relative_to((ROOT/'runtime').resolve()):
        raise ValueError('Use the separate demo database for verification')
    expected=read(DEMO/'expected.json')
    with InvoiceStore(database) as store:
        service=InvoiceService(store)
        rows=service.list_invoices(dataset)
        summary=service.summary(dataset)
    actual={row['file_id']:row for row in rows}
    failures=[]
    if set(actual)!={case['file_id'] for case in expected['cases']}:
        failures.append('Invoice file IDs differ from the six-case answer key')
    for case in expected['cases']:
        outcome=(actual.get(case['file_id'],{}).get('reconciliation') or {})
        for key,value in case.items():
            if key in ('file_id','explanation'):
                continue
            found=outcome.get(key)
            mismatch = set(found or []) != set(value) if key == 'findings' else found != value
            if mismatch:
                failures.append(case['file_id']+': '+key+' differs from fixed expectation')
    for key,value in expected['summary'].items():
        if summary.get(key)!=value:
            failures.append('Summary '+key+' differs from fixed expectation')
    return {'passed':not failures,'failures':failures,'summary':summary,
            'cases':[{'file_id':row['file_id'], 'status':(row['reconciliation'] or {}).get('status'),'findings':row['findings'],
                      'billed_cents':(row['reconciliation'] or {}).get('billed_cents'),
                      'expected_cents':(row['reconciliation'] or {}).get('expected_cents'),
                      'difference_cents':(row['reconciliation'] or {}).get('difference_cents')} for row in rows]}


def preservation():
    baseline=read(DEMO/'preservation.json')
    changed=[name for name,sha in baseline.items() if not (ROOT/name).is_file() or digest(ROOT/name)!=sha]
    return {'passed':not changed,'protected_files':len(baseline),'changed':changed}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','process','load','verify','preserve'])
    parser.add_argument('--db',type=Path,default=DEMO/'invoices.sqlite3')
    parser.add_argument('--dataset',default=DATASET)
    args=parser.parse_args(argv)
    try:
        result={'prepare':prepare,'process':process,'load':lambda:load(args.db,args.dataset),'verify':lambda:verify(args.db,args.dataset),'preserve':preservation}[args.command]()
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
        if args.command=='process':
            return 1 if result['processing_errors'] or result['extraction_issues'] else 0
        return 0 if result.get('passed',True) else 1
    except Exception as exc:
        print('Demo '+args.command+' failed ('+type(exc).__name__+'): '+str(exc),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
