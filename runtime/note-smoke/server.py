"""Synthetic browser smoke test ONLY. Never a genuine Gemini capture."""
from contextlib import closing
from functools import partial
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from test_discrepancy_notes import sdk, MODEL
from app.api import create_app
from app.application_service import Settings
from app.discrepancy_notes import GeminiNotes,ReplayNotes,NoteConfiguration
import uvicorn
folder=Path(__file__).parent
with closing(sqlite3.connect(ROOT/'runtime/invoices.sqlite3')) as source, closing(sqlite3.connect(folder/'invoices.sqlite3')) as target:
    source.backup(target)
settings=Settings(db_path=folder/'invoices.sqlite3',runtime_dir=folder,capture_dir=folder/'synthetic-test-captures')
app=create_app(settings)
service=app.state.service
service.note_factory=partial(GeminiNotes,configuration=NoteConfiguration(MODEL),api_key='synthetic-test-credential',origin='synthetic_test')
service.note_replay_factory=partial(ReplayNotes,allow_synthetic=True)  # TEST ONLY.
invoice=next(row for row in service.invoices('parser-v1') if row['file_id']=='wrong-price')
with patch('google.genai.models.Models.generate_content',return_value=sdk()) as request:
    initial=service.draft_note(invoice['id'])
    assert initial['state']=='current'
    uvicorn.run(app,host='127.0.0.1',port=8002,workers=1)
    (folder/'network-count.json').write_text(json.dumps({'origin':'synthetic_test','sdk_request_calls':request.call_count}),encoding='utf-8')
