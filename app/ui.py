"""Readable Gradio presentation; storage, extraction and reconciliation stay in services."""
import base64
from datetime import datetime, timezone
from functools import wraps
from html import escape
from pathlib import Path
import re
import gradio as gr
from app.application_service import ApplicationError, MAX_FILES, MAX_BYTES
from app.llm_extraction import FIELD_NAMES

LABELS = {'invoice_number': 'Invoice number', 'supplier_id': 'Supplier ID',
          'po_id': 'Purchase order', 'sku': 'SKU', 'quantity': 'Quantity',
          'unit_price_cents': 'Unit price (USD)', 'total_cents': 'Invoice total (USD)'}
MONEY_FIELDS = {'unit_price_cents', 'total_cents'}
STATUSES = {'reconciled': 'Reconciled', 'discrepant': 'Discrepant', 'duplicate': 'Duplicate',
            'unresolved': 'Unresolved', 'processing_error': 'Processing error'}
MODES = {'parser': 'Text parser', 'llm': 'Live Gemini', 'replay': 'Offline replay'}
# HTML css_template is scoped by Gradio to its own component.
STATUS_CSS = """
.invoice-badge {display:inline-block;padding:3px 9px;border-radius:6px;font-size:16px;font-weight:600;}
.status-reconciled {color:#14532d;background:#dcfce7;}
.status-discrepant {color:#7c2d12;background:#ffedd5;}
.status-duplicate,.status-processing_error {color:#7f1d1d;background:#fee2e2;}
.status-unresolved {color:#374151;background:#f3f4f6;}
.summary-cards {display:flex;gap:10px;flex-wrap:wrap;}
.summary-card {flex:1;min-width:130px;border:1px solid #ddd;border-radius:8px;padding:12px;}
.summary-count {display:block;font-size:24px;}
"""


def status_badge(status, label=None):
    key = status if status in STATUSES else 'unresolved'
    return f'<span class="invoice-badge status-{key}">{escape(label or STATUSES.get(status, "Unavailable"))}</span>'

EXPLANATIONS = {
    'reconciled': 'The invoice matches the agreed item, quantity and unit price.',
    'discrepant': 'The billed details differ from the purchase order or receipt. Review the findings below.',
    'duplicate': 'This supplier and invoice number already appear earlier in this dataset. This invoice is excluded from supported overcharge.',
    'unresolved': 'A reference or billed value could not be verified. This invoice is excluded from supported overcharge.',
    'processing_error': 'The document could not be processed. No editable extraction is available; try a new upload.'}
FINDINGS = {
    'duplicate_identity': 'Duplicate invoice: the same supplier and invoice number were processed earlier in this dataset.',
    'unit_price_mismatch': 'The billed unit price differs from the price agreed in the purchase order.',
    'ordered_quantity_mismatch': 'The billed quantity differs from the quantity ordered.',
    'received_quantity_mismatch': 'The billed quantity differs from the quantity received.',
    'missing_invoice_identity': 'The supplier or invoice number is missing, so the invoice identity cannot be verified.',
    'missing_po_reference': 'A supplier or purchase-order reference is missing. A purchase order cannot be matched.',
    'unknown_po_reference': 'No purchase order matches the supplied reference.',
    'supplier_reference_conflict': 'The purchase-order reference belongs to a different supplier. No match is confirmed.',
    'ambiguous_po_reference': 'More than one purchase order matches. All candidates are shown; none has been chosen.',
    'missing_receipt_reference': 'No receipt matches the purchase order.',
    'ambiguous_receipt_reference': 'More than one receipt matches. All candidates are shown; none has been chosen.',
    'ambiguous_receipt_supplier': 'The receipt does not identify its supplier and the purchase-order reference is shared by suppliers.',
    'sku_mismatch': 'The billed SKU differs from the purchase-order SKU. The expected amount and difference remain unknown.',
    'receipt_sku_mismatch': 'The receipt SKU differs from the purchase-order SKU. The expected amount and difference remain unknown.',
    'missing_sku': 'An item SKU is missing from the invoice or supporting records.',
    'missing_or_non_integer_amount': 'A quantity or amount is missing or invalid in the invoice or supporting records.',
    'negative_quantity_or_amount': 'A quantity or monetary value is negative. The existing rules leave this invoice unresolved.',
    'inconsistent_billed_total': 'The written invoice total does not equal its billed quantity times unit price. No amount has been corrected automatically.'}


def scalar(value, missing='Unknown'):
    if value is None:
        return missing
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else 'Unavailable'


def money_text(cents):
    """Exact editable USD text from integer cents, without float or grouping."""
    if cents is None:
        return ''
    if type(cents) is not int:
        return ''
    sign = '-' if cents < 0 else ''
    whole, fraction = divmod(abs(cents), 100)
    return f'{sign}{whole}.{fraction:02d}'


def format_usd(cents):
    if cents is None:
        return 'Unknown'
    if type(cents) is not int:
        return 'Unavailable'
    whole, fraction = divmod(abs(cents), 100)
    return f"{'-' if cents < 0 else ''}${whole:,}.{fraction:02d}"


def parse_usd(text):
    """Plain signed USD decimals, at most two decimal places; blank means None."""
    text = text.strip()
    if not text:
        return None
    if not re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]{1,2})?', text):
        raise ApplicationError(422, 'Enter a USD amount such as 20.00, with at most two decimal places. No rounding is applied.')
    sign = -1 if text.startswith('-') else 1
    unsigned = text.lstrip('+-')
    whole, _, fraction = unsigned.partition('.')
    return sign * (int(whole) * 100 + int(fraction.ljust(2, '0') or '0'))


def field_value(field, value):
    return format_usd(value) if field in MONEY_FIELDS else scalar(value)


def source_name(row):
    evidence = row['original_evidence']
    name = (evidence['record'].get('original_filename') or evidence.get('image', {}).get('reference')
            or evidence['record'].get('source') or row['file_id'])
    return str(name).replace('\\', '/').split('/')[-1]


def row_status(row):
    return (row.get('reconciliation') or {}).get('status', 'processing_error')


def html_list(title, messages, empty):
    items = ''.join('<li>' + escape(str(message)) + '</li>' for message in messages)
    return f'<h3>{escape(title)}</h3>' + (f'<ul>{items}</ul>' if items else f'<p>{escape(empty)}</p>')


def issue_messages(issues):
    meanings = {'missing': 'A value could not be found in the invoice text.',
                'invalid': 'The value in the invoice text could not be read reliably.',
                'conflicting': 'The invoice text contains conflicting values; no value was chosen.'}
    return [f"{LABELS.get(issue.get('field'), 'Invoice field')}: {meanings.get(issue.get('code'), 'This value needs review.')}"
            for issue in issues if isinstance(issue, dict)]


def processing_message(record):
    error = record.get('error') or ''
    mode = record.get('mode')
    if 'Image validation' in error:
        return 'The file could not be read as a supported PNG or JPEG image.'
    if 'Replay OCR association' in error:
        return 'This image has no verified saved invoice text. Use Text parser or Live Gemini for a new extraction.'
    if 'OCR' in error or 'Empty OCR' in error:
        return 'The invoice text could not be read from the image. Try a clearer image.'
    if mode == 'replay':
        return 'A compatible saved extraction is unavailable for this image. Try a new extraction.'
    if mode == 'llm':
        return 'Gemini could not complete a valid extraction. Check the configured access or try again.'
    return 'The invoice could not be processed. Try a new upload.'


def summary_html(summary):
    counts = summary.get('counts_by_status', {})
    cards = [(key, STATUSES[key], counts.get(key, 0)) for key in ('reconciled', 'discrepant', 'duplicate', 'unresolved')]
    cards.append(('processing_error', 'Processing errors', summary.get('processing_errors', 0)))
    content = ''.join(f'<div class="summary-card">{status_badge(key, label)}<strong class="summary-count">{escape(str(value))}</strong></div>' for key, label, value in cards)
    amount = format_usd(summary.get('overcharge_cents')) if summary else 'Unavailable'
    return '<div class="summary-cards">' + content + '</div>' + f'<h2>Supported overcharge: {escape(amount)}</h2><p>Positive differences from supported matches only. Duplicate and unresolved invoices are excluded.</p>'



def invoice_view(row):
    """Presentation values only: scalar tables and escaped HTML; no metadata dump."""
    current, outcome = row.get('current_fields') or {}, row.get('reconciliation') or {}
    references = row.get('supporting_references') or {}
    pos, receipts = references.get('purchase_orders', []), references.get('receipts', [])
    codes = row.get('findings', [])
    # Candidate records are displayed, but never arbitrarily selected for comparison.
    po = pos[0] if len(pos) == 1 and pos[0].get('supplier_id') == current.get('supplier_id') and pos[0].get('po_id') == current.get('po_id') and 'ambiguous_po_reference' not in codes else {}
    receipt = receipts[0] if po and len(receipts) == 1 and not any(code in codes for code in ('ambiguous_receipt_reference', 'ambiguous_receipt_supplier')) else {}
    comparison = [
        ['SKU', scalar(po.get('sku')), scalar(receipt.get('sku')), scalar(current.get('sku'))],
        ['Quantity', scalar(po.get('quantity')), scalar(receipt.get('quantity')), scalar(current.get('quantity'))],
        ['Unit price', format_usd(po.get('unit_cents')), '—', format_usd(current.get('unit_price_cents'))],
        ['Expected amount', '—', '—', format_usd(outcome.get('expected_cents'))],
        ['Billed amount', '—', '—', format_usd(outcome.get('billed_cents'))],
        ['Difference', '—', '—', format_usd(outcome.get('difference_cents'))]]
    po_table = [[scalar(p.get('po_id')), scalar(p.get('supplier_id')), scalar(p.get('sku')),
                 scalar(p.get('quantity')), format_usd(p.get('unit_cents'))] for p in pos]
    receipt_table = [[scalar(r.get('receipt_id')), scalar(r.get('po_id')), scalar(r.get('supplier_id')),
                      scalar(r.get('sku')), scalar(r.get('quantity'))] for r in receipts]
    original = row.get('original_fields')
    original_table = [[LABELS[name], field_value(name, original.get(name)) if isinstance(original, dict) else 'Unavailable'] for name in FIELD_NAMES]
    history = []
    for entry in row.get('correction_history', []):
        try:
            timestamp = datetime.fromisoformat(entry['timestamp']).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        except (ValueError, KeyError):
            timestamp = 'Unavailable'
        result = entry.get('outcome') or {}
        for field, change in entry.get('changes', {}).items():
            history.append([timestamp, LABELS.get(field, 'Invoice field'), field_value(field, change.get('before')),
                            field_value(field, change.get('after')), scalar(entry.get('reason')),
                            STATUSES.get(result.get('status'), 'Unavailable'), format_usd(result.get('difference_cents'))])
    evidence = row['original_evidence']
    synthetic = ('synthetic' in str(evidence.get('dataset_provenance', '')).lower()
                 or evidence['record'].get('metadata', {}).get('origin') == 'synthetic_test'
                 or 'synthetic' in row.get('dataset', '').lower()
                 or any('synthetic' in str(entry.get('reason', '')).lower() for entry in row.get('correction_history', [])))
    demo = '<p><strong>Synthetic demonstration:</strong> these example values or corrections were created for testing.</p>' if synthetic else ''
    status = row_status(row)
    heading = f'<h2>{escape(source_name(row))} · {status_badge(status)}</h2><p>{escape(EXPLANATIONS.get(status, "Review this invoice below."))}</p>' + demo
    problems = issue_messages(row.get('current_issues', []))
    if row.get('processing_status') == 'error':
        problems.append(processing_message(evidence['record']))
    if evidence.get('import_problems'):
        problems.append('The saved extraction was incomplete or invalid. A new extraction may be needed.')
    for note in evidence.get('availability_notes', []):
        if 'image' in note.lower():
            message = 'The original image is unavailable or could not be verified.'
        elif 'capture' in note.lower():
            message = 'Some saved extraction evidence is unavailable.'
        else:
            message = 'Some saved invoice text is unavailable or could not be verified.'
        if message not in problems:
            problems.append(message)
    if row.get('image_error') and 'The original image is unavailable or could not be verified.' not in problems:
        problems.append('The original image is unavailable or could not be verified.')
    return {'heading': heading, 'values': [money_text(current.get(name)) if name in MONEY_FIELDS else (scalar(current.get(name)) if current.get(name) is not None else '') for name in FIELD_NAMES],
            'editable': row.get('current_fields') is not None, 'comparison': comparison,
            'findings': html_list('Findings', [FINDINGS.get(code, 'This invoice needs further review.') for code in codes], 'No reconciliation differences found.' if outcome else 'No reconciliation result is available.'),
            'po_caption': '<p>' + ('One matching purchase order.' if po else 'All available purchase-order candidates are shown. No match is confirmed.' if pos else 'No matching purchase order is available.') + '</p>',
            'po_table': po_table,
            'receipt_caption': '<p>' + ('One matching receipt.' if receipt else 'All available receipt candidates are shown. No match is confirmed.' if receipts else 'No matching receipt is available.') + '</p>',
            'receipt_table': receipt_table, 'issues': html_list('Extraction and processing messages', problems, 'No extraction or processing issues.'),
            'original': original_table, 'history': history,
            'history_visible': bool(history), 'history_empty': '' if history else '<p>No corrections yet</p>'}


class ReviewController:
    def __init__(self, service):
        self.service = service

    def queue(self, dataset, status='all'):
        if not dataset:
            return [], {}, []
        rows = self.service.invoices(dataset, None if status in ('all', 'processing_error') else status)
        if status == 'processing_error':
            rows = [row for row in rows if row.get('processing_status') == 'error']
        table, ids = [], []
        for row in rows:
            fields, outcome = row['current_fields'] or {}, row['reconciliation'] or {}
            table.append([source_name(row), scalar(fields.get('invoice_number')), scalar(fields.get('supplier_id')),
                          STATUSES.get(row_status(row), 'Unavailable'), 'Corrected' if row['review_state'] == 'corrected' else 'Not reviewed',
                          format_usd(outcome.get('difference_cents'))])
            ids.append(row['id'])
        return table, self.service.summary(dataset), ids

    def save(self, invoice_id, values, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ApplicationError(422, 'A correction reason is required.')
        if not invoice_id:
            raise ApplicationError(422, 'Select an invoice first.')
        before = self.service.detail(invoice_id)['current_fields']
        if before is None:
            raise ApplicationError(422, 'This invoice has no editable extraction. Try a new upload.')
        if len(values) != len(FIELD_NAMES):
            raise ApplicationError(422, 'Some invoice fields are unavailable. Refresh this invoice.')
        patch = {}
        for field, text in zip(FIELD_NAMES, values):
            text = text.strip()
            value = text or None
            if field in MONEY_FIELDS:
                value = parse_usd(text)
            elif field == 'quantity' and value is not None:
                if not re.fullmatch(r'[+-]?[0-9]+', text):
                    raise ApplicationError(422, 'Quantity must be a whole number.')
                value = int(text)
            if value != before[field]:
                patch[field] = value
        if not patch:
            raise ApplicationError(422, 'Change at least one field before saving.')
        return self.service.correct(invoice_id, patch, reason)

    def batch_table(self, result):
        table = []
        for record in result['files']:
            status = record['status']
            messages = issue_messages(record.get('issues', []))
            if status == 'error':
                messages = [processing_message(record)]
            duration = record.get('duration_seconds')
            table.append([source_name({'original_evidence': {'record': record}, 'file_id': record['file_id']}),
                          {'success': 'Ready for review', 'issues': 'Extracted; check issues', 'error': 'Processing failed'}[status],
                          f'{duration:.2f} s' if isinstance(duration, (int, float)) else 'Unavailable',
                          ' '.join(messages) or 'Invoice extracted successfully.'])
        return table


def guarded(callback):
    @wraps(callback)
    def run(*args, **kwargs):
        try:
            return callback(*args, **kwargs)
        except ApplicationError as exc:
            raise gr.Error(str(exc)) from None
        except Exception:
            raise gr.Error('The review action could not be completed. Check your entries or try again.') from None
    return run


def note_html(result):
    """Safe presentation only; captures, hashes, provider bodies stay in APIs/files."""
    if not isinstance(result, dict):
        result = {'state': 'unavailable', 'message': 'No compatible saved note is available.'}
    content = '<h3>Discrepancy note</h3><p><strong>AI-generated draft — review before use</strong></p>'
    content += '<p>' + escape(str(result.get('message', 'No saved note is available.'))) + '</p>'
    draft = result.get('draft')
    if result.get('state') == 'current' and isinstance(draft, dict):
        latest = result.get('latest') or {}
        if latest.get('metadata', {}).get('origin') == 'synthetic_test':
            content += '<p><strong>Synthetic test draft; not a genuine Gemini response.</strong></p>'
        content += '<p>' + escape(str(draft.get('note', ''))) + '</p>'
        content += html_list('Sources', draft.get('source_references', []), 'No sources available.')
    return content


def build_ui(service):
    controller = ReviewController(service)
    with gr.Blocks(title='Invoice review', analytics_enabled=False) as ui:
        selected, row_ids = gr.State(None), gr.State([])
        gr.Markdown('# Invoice review\nChoose a dataset, then click an invoice row to review it and make corrections.')
        with gr.Row():
            dataset = gr.Dropdown(label='Dataset · extraction mode', choices=[])
            status = gr.Dropdown(label='Status filter', choices=[('All invoices', 'all')] + [(value, key) for key, value in STATUSES.items()], value='all')
            refresh = gr.Button('Refresh datasets')
        totals = gr.HTML(summary_html({}), css_template=STATUS_CSS)
        queue_notice = gr.HTML('<p>No dataset selected.</p>')
        queue = gr.Dataframe(headers=['Source filename', 'Invoice number', 'Supplier', 'Status', 'Review state', 'Difference'], datatype='str', type='array', interactive=False, wrap=True, label='Review queue', buttons=[], column_widths=['24%', '15%', '11%', '14%', '16%', '20%'])
        heading = gr.HTML('<h2>Select an invoice</h2>', css_template=STATUS_CSS)
        findings = gr.HTML()
        issues = gr.HTML()
        note = gr.HTML(note_html({'message': 'Select an invoice to review its note.'}))
        with gr.Row():
            generate_note = gr.Button('Generate discrepancy note', interactive=False)
            replay_note = gr.Button('Replay saved note', interactive=False)
        with gr.Row():
            image = gr.HTML('<p>No invoice selected.</p>')
            with gr.Column():
                gr.Markdown('### Current invoice values\nAmounts are in USD, for example 20.00. Blank fields mean unknown. Values are never filled or calculated automatically.')
                fields = [gr.Textbox(label=LABELS[name], interactive=False) for name in FIELD_NAMES]
                reason = gr.Textbox(label='Correction reason', interactive=False)
                save = gr.Button('Save correction and recalculate', variant='primary', interactive=False)
        comparison = gr.Dataframe(headers=['Detail', 'Purchase order', 'Receipt', 'Invoice / result'], label='Invoice comparison', datatype='str', interactive=False, wrap=True, buttons=[])
        with gr.Row():
            with gr.Column():
                gr.Markdown('### Purchase orders')
                po_caption = gr.HTML()
                po_table = gr.Dataframe(headers=['Purchase-order ID', 'Supplier ID', 'SKU', 'Quantity ordered', 'Agreed unit price'], datatype='str', interactive=False, wrap=True, buttons=[])
            with gr.Column():
                gr.Markdown('### Receipts')
                receipt_caption = gr.HTML()
                receipt_table = gr.Dataframe(headers=['Receipt ID', 'Purchase-order ID', 'Supplier ID', 'SKU', 'Quantity received'], datatype='str', interactive=False, wrap=True, buttons=[])
        with gr.Accordion('Original extracted values', open=False):
            original = gr.Dataframe(headers=['Field', 'Original value'], label='Original extracted values', datatype='str', interactive=False, buttons=[])
        with gr.Accordion('Correction history', open=False):
            history_empty = gr.HTML('<p>No corrections yet</p>')
            history = gr.Dataframe(headers=['Timestamp', 'Field', 'Before', 'After', 'Reason', 'Resulting status', 'Resulting difference'], label='Correction history', datatype='str', interactive=False, wrap=True, buttons=[], visible=False)
        with gr.Accordion('Upload invoices', open=False):
            gr.Markdown('Create a new dataset from up to 10 PNG or JPEG images, at most 10 MiB each.\n\n**Text parser** reads invoice fields from the document text. **Live Gemini** requests a new extraction using your configured access. **Offline replay** reuses a verified saved extraction and makes no new request. A saved image/text association is required for replay.')
            batch_name = gr.Textbox(label='New dataset name')
            mode = gr.Radio(choices=[(value, key) for key, value in MODES.items()], value='parser', label='Extraction mode')
            uploads = gr.UploadButton('Choose invoice images', file_count='multiple', file_types=['.png', '.jpg', '.jpeg'])
            uploaded_files = gr.Dataframe(headers=['Selected filename'], datatype='str', interactive=False, buttons=[])
            process = gr.Button('Process invoices')
            batch_notice = gr.HTML()
            batch_result = gr.Dataframe(headers=['Source filename', 'Outcome', 'Duration', 'Message'], label='Upload results', datatype='str', interactive=False, wrap=True, buttons=[])

        detail_outputs = [heading, image, *fields, reason, save, comparison, findings, po_caption, po_table, receipt_caption, receipt_table, issues, note, generate_note, replay_note, original, history, history_empty]
        queue_outputs = [queue, totals, queue_notice, row_ids, selected]

        @guarded
        def datasets(current=None):
            choices = [(f"{item['name']} · {MODES[item['mode']]}", item['name']) for item in service.datasets()]
            names = {value for _, value in choices}
            return gr.Dropdown(choices=choices, value=current if current in names else (choices[0][1] if choices else None))

        @guarded
        def refresh_queue(name, filter_value, invoice_id):
            table, summary, ids = controller.queue(name, filter_value)
            value = invoice_id if invoice_id in ids else (ids[0] if ids else None)
            notice = 'Click an invoice row to open its details.' if ids else ('No invoices match this filter.' if name else 'No saved dataset selected. Upload invoices to begin.')
            return table, summary_html(summary), '<p>' + notice + '</p>', ids, value

        @guarded
        def show(invoice_id):
            if not invoice_id:
                return ['<h2>No invoice selected</h2>', '<p>Select a row in the review queue.</p>'] + [gr.Textbox(value='', interactive=False) for _ in fields] + [gr.Textbox(value='', interactive=False), gr.Button(interactive=False), [], '', '', [], '', [], '', note_html({'message': 'Select an invoice to review its note.'}), gr.Button(interactive=False), gr.Button(interactive=False), [], gr.Dataframe(value=[], visible=False), '<p>No corrections yet</p>']
            row = service.detail(invoice_id)
            view = invoice_view(row)
            try:
                data, media = service.image(invoice_id)
                display = f'<img alt="Original invoice" style="width:100%;max-height:560px;object-fit:contain" src="data:{media};base64,{base64.b64encode(data).decode()}" />'
            except ApplicationError:
                display = '<p>The original invoice image is unavailable or has changed.</p>'
            return [view['heading'], display] + [gr.Textbox(value=value, interactive=view['editable']) for value in view['values']] + [gr.Textbox(value='', interactive=view['editable']), gr.Button(interactive=view['editable']), view['comparison'], view['findings'], view['po_caption'], view['po_table'], view['receipt_caption'], view['receipt_table'], view['issues'], note_html(service.note(invoice_id)), gr.Button(interactive=row_status(row) in ('discrepant', 'duplicate', 'unresolved')), gr.Button(interactive=row_status(row) in ('discrepant', 'duplicate', 'unresolved')), view['original'], gr.Dataframe(value=view['history'], visible=view['history_visible']), view['history_empty']]

        @guarded
        def note_action(invoice_id, live, progress=gr.Progress()):
            if not invoice_id:
                raise ApplicationError(422, 'Select an invoice first.')
            progress(0, desc='Generating discrepancy draft' if live else 'Replaying saved note')
            service.draft_note(invoice_id, 'llm' if live else 'replay')
            progress(1, desc='Note action completed')

        @guarded
        def show_note(invoice_id):
            return note_html(service.note(invoice_id)) if invoice_id else note_html({'message': 'Select an invoice to review its note.'})

        @guarded
        def select_row(ids, evt: gr.SelectData):
            index = evt.index[0] if isinstance(evt.index, (tuple, list)) else evt.index
            return ids[index] if isinstance(index, int) and 0 <= index < len(ids) else None

        @guarded
        def correction(invoice_id, name, filter_value, correction_reason, *values):
            controller.save(invoice_id, values, correction_reason)
            return refresh_queue(name, filter_value, invoice_id)

        @guarded
        def upload_preview(paths):
            return [[Path(path).name] for path in (paths or [])]

        @guarded
        def process_batch(name, selected_mode, paths, progress=gr.Progress()):
            if not paths or len(paths) > MAX_FILES:
                raise ApplicationError(413, 'Choose between 1 and 10 invoice images.')
            files = []
            for path in paths:
                with Path(path).open('rb') as stream:
                    data = stream.read(MAX_BYTES + 1)
                files.append((Path(path).name, data))
            result = service.batch(name, selected_mode, files, progress=lambda done, total, state: progress((done, total), desc=f'Processed {done} of {total} invoices'))
            choices = [(f"{item['name']} · {MODES[item['mode']]}", item['name']) for item in service.datasets()]
            failures = sum(record['status'] == 'error' for record in result['files'])
            message = f'Dataset created. {len(result["files"])} invoices processed; {failures} processing failures. Review each outcome below.'
            return controller.batch_table(result), '<p>' + message + '</p>', gr.Dropdown(choices=choices, value=name), 'all'

        ui.load(datasets, outputs=dataset)
        refresh.click(datasets, dataset, dataset).then(refresh_queue, [dataset, status, selected], queue_outputs).then(show, selected, detail_outputs)
        dataset.change(refresh_queue, [dataset, status, selected], queue_outputs).then(show, selected, detail_outputs)
        status.change(refresh_queue, [dataset, status, selected], queue_outputs).then(show, selected, detail_outputs)
        queue.select(select_row, row_ids, selected).then(show, selected, detail_outputs)
        save.click(correction, [selected, dataset, status, reason, *fields], queue_outputs).then(show, selected, detail_outputs)
        generate_note.click(note_action, [selected, gr.State(True)], outputs=None).then(show_note, selected, note)
        replay_note.click(note_action, [selected, gr.State(False)], outputs=None).then(show_note, selected, note)
        uploads.upload(upload_preview, uploads, uploaded_files)
        process.click(process_batch, [batch_name, mode, uploads], [batch_result, batch_notice, dataset, status], concurrency_limit=1)
    return ui.queue(default_concurrency_limit=2, max_size=10)
