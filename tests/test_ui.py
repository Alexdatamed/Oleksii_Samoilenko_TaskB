"""Presentation tests: synthetic values, no requests or OCR downloads."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.application_service import ApplicationError
from app.ui import (ReviewController, FIELD_NAMES, build_ui, format_usd, money_text,
                    parse_usd, invoice_view, summary_html, source_name, status_badge, STATUS_CSS)


def synthetic_row():
    fields = dict(file_id='private-id', invoice_number='DEMO-1', supplier_id='SUP-A',
                  po_id='PO-X', sku='ITEM-X', quantity=2, unit_price_cents=2400, total_cents=4800)
    po = dict(po_id='PO-X', supplier_id='SUP-A', sku='ITEM-X', quantity=2, unit_cents=1000)
    receipt = dict(receipt_id='REC-X', po_id='PO-X', sku='ITEM-X', quantity=2)
    return {'id': 'hidden-uuid', 'file_id': 'private-id', 'dataset': 'synthetic-demo',
            'current_fields': fields, 'original_fields': deepcopy(fields), 'current_issues': [],
            'reconciliation': dict(status='discrepant', expected_cents=2000, billed_cents=4800, difference_cents=2800),
            'findings': ['unit_price_mismatch'], 'supporting_references': {'purchase_orders': [po], 'receipts': [receipt]},
            'processing_status': 'success', 'review_state': 'unreviewed', 'correction_history': [],
            'original_evidence': {'dataset_provenance': 'synthetic demonstration', 'record': {
                'original_filename': 'demo.png', 'mode': 'parser', 'metadata': {'capture': 'private/capture/path', 'origin': 'synthetic_test'}},
                'availability_notes': []}, 'image_error': None}


class UiTests(unittest.TestCase):
    def test_exact_money_display_zero_negative_unknown_and_large(self):
        for cents, display, text in [(2000, '$20.00', '20.00'), (1, '$0.01', '0.01'),
                                     (0, '$0.00', '0.00'), (-123, '-$1.23', '-1.23'),
                                     (9007199254740993, '$90,071,992,547,409.93', '90071992547409.93')]:
            self.assertEqual(format_usd(cents), display)
            self.assertEqual(money_text(cents), text)
            self.assertEqual(parse_usd(text), cents)
        self.assertEqual(format_usd(None), 'Unknown')
        self.assertEqual(format_usd(2.5), 'Unavailable')
        self.assertEqual(money_text(None), '')

    def test_exact_usd_parsing_and_blank(self):
        for value, expected in [('20', 2000), ('20.1', 2010), ('20.01', 2001), ('  +0.01 ', 1),
                                ('-0.01', -1), ('0', 0), ('-20.00', -2000), ('', None), (' ', None),
                                ('90071992547409.93', 9007199254740993)]:
            self.assertEqual(parse_usd(value), expected)

    def test_invalid_currency_and_excess_precision_never_rounded(self):
        for value in ('20.001', '20.000', '20.', 'NaN', 'Infinity', '1e2', '$20.00', '1,000.00', '.50', '--1', 'abc'):
            with self.subTest(value=value), self.assertRaises(ApplicationError):
                parse_usd(value)

    def test_comparison_and_actual_reference_ids(self):
        view = invoice_view(synthetic_row())
        comparison = {r[0]: r[1:] for r in view['comparison']}
        self.assertEqual(list(comparison), ['SKU', 'Quantity', 'Unit price', 'Expected amount', 'Billed amount', 'Difference'])
        self.assertEqual(comparison['SKU'], ['ITEM-X', 'ITEM-X', 'ITEM-X'])
        self.assertEqual(comparison['Quantity'], ['2', '2', '2'])
        self.assertEqual(comparison['Unit price'], ['$10.00', '—', '$24.00'])
        self.assertEqual(comparison['Expected amount'], ['—', '—', '$20.00'])
        self.assertEqual(comparison['Billed amount'], ['—', '—', '$48.00'])
        self.assertEqual(comparison['Difference'], ['—', '—', '$28.00'])
        self.assertEqual(view['po_table'][0][0], 'PO-X')
        self.assertEqual(view['receipt_table'][0][0], 'REC-X')
        self.assertEqual(view['receipt_table'][0][2], 'Unknown')
        self.assertIn('billed unit price differs', view['findings'])
        self.assertIn('Synthetic demonstration', view['heading'])
        self.assertEqual(view['values'][-2:], ['24.00', '48.00'])
        self.assertEqual(view['original'][-2:], [['Unit price (USD)', '$24.00'], ['Invoice total (USD)', '$48.00']])

    def test_missing_values_and_sku_mismatch_do_not_invent_expected_amounts(self):
        row = synthetic_row()
        row['current_fields']['po_id'] = None
        row['supporting_references'] = {'purchase_orders': [], 'receipts': []}
        row['reconciliation'].update(status='unresolved', expected_cents=None, difference_cents=None)
        row['findings'] = ['missing_po_reference']
        view = invoice_view(row)
        self.assertEqual(view['values'][2], '')
        self.assertEqual(view['comparison'][1][1], 'Unknown')
        self.assertEqual(view['comparison'][3][3], 'Unknown')
        self.assertEqual(view['comparison'][5][3], 'Unknown')
        self.assertEqual(view['po_table'], [])
        row = synthetic_row()
        row['reconciliation'].update(status='unresolved', expected_cents=None, difference_cents=None)
        row['findings'] = ['sku_mismatch']
        self.assertEqual(invoice_view(row)['comparison'][3][3], 'Unknown')

    def test_ambiguous_candidates_all_displayed_and_none_selected(self):
        row = synthetic_row()
        row['supporting_references']['purchase_orders'].append(dict(row['supporting_references']['purchase_orders'][0], quantity=9))
        row['supporting_references']['receipts'].append(dict(row['supporting_references']['receipts'][0], receipt_id='REC-Y'))
        row['findings'] = ['ambiguous_po_reference', 'ambiguous_receipt_reference']
        row['reconciliation'].update(status='unresolved', expected_cents=None, difference_cents=None)
        view = invoice_view(row)
        self.assertEqual(len(view['po_table']), 2)
        self.assertEqual([r[0] for r in view['receipt_table']], ['REC-X', 'REC-Y'])
        self.assertIn('No match is confirmed', view['po_caption'])
        self.assertIn('No match is confirmed', view['receipt_caption'])
        self.assertEqual(view['comparison'][1][1], 'Unknown')
        self.assertEqual(view['comparison'][1][2], 'Unknown')

    def test_supplier_conflict_is_not_a_confirmed_po(self):
        row = synthetic_row()
        row['supporting_references']['purchase_orders'][0]['supplier_id'] = 'OTHER'
        row['findings'] = ['supplier_reference_conflict']
        view = invoice_view(row)
        self.assertEqual(view['comparison'][1][1], 'Unknown')
        self.assertEqual(view['po_table'][0][1], 'OTHER')
        self.assertIn('different supplier', view['findings'])

    def test_html_escaped_and_no_private_metadata_or_nested_table_cells(self):
        row = synthetic_row()
        row['original_evidence']['record']['original_filename'] = '<img onerror=alert(1)>.png'
        row['original_fields']['invoice_number'] = {'malformed': 'object'}
        view = invoice_view(row)
        self.assertIn('&lt;img', view['heading'])
        self.assertNotIn('<img onerror', view['heading'])
        for key in ('comparison', 'po_table', 'receipt_table', 'original', 'history'):
            for line in view[key]:
                self.assertTrue(all(isinstance(cell, str) for cell in line))
        shown = str(view)
        for private in ('hidden-uuid', 'private/capture/path', 'unit_price_cents', 'sdk_version', 'configuration_fingerprint'):
            self.assertNotIn(private, shown)
        self.assertEqual(source_name(synthetic_row()), 'demo.png')

    def test_readable_issues_processing_failure_and_disabled_fields(self):
        row = synthetic_row()
        row.update(current_fields=None, original_fields=None, reconciliation=None, processing_status='error', supporting_references=None)
        row['original_evidence']['record'].update(error='Extraction failed (RuntimeError): /private/path sdk details', mode='replay')
        row['image_error'] = 'Original image changed'
        view = invoice_view(row)
        self.assertFalse(view['editable'])
        self.assertIn('Processing error', view['heading'])
        self.assertIn('compatible saved extraction', view['issues'])
        self.assertNotIn('/private/path', view['issues'])
        self.assertEqual(view['values'], [''] * 7)
        self.assertEqual(view['original'][0][1], 'Unavailable')
        row = synthetic_row()
        row['current_issues'] = [{'field': 'po_id', 'code': 'missing', 'message': 'technical-po_id'},
                                 {'field': 'quantity', 'code': 'conflicting', 'message': 'technical'}]
        view = invoice_view(row)
        self.assertIn('Purchase order: A value could not be found', view['issues'])
        self.assertIn('conflicting values', view['issues'])
        self.assertNotIn('po_id', view['issues'])

    def test_correction_history_scalar_rows_with_unknown_zero_and_exact_money(self):
        row = synthetic_row()
        row['correction_history'] = [{'timestamp': '2026-10-06T12:00:00+00:00', 'reason': 'Synthetic correction',
            'changes': {'unit_price_cents': {'before': 2400, 'after': 0}, 'po_id': {'before': None, 'after': 'PO-X'}},
            'outcome': {'status': 'unresolved', 'difference_cents': None}}]
        history = invoice_view(row)['history']
        self.assertEqual(history[0], ['2026-10-06 12:00:00 UTC', 'Unit price (USD)', '$24.00', '$0.00', 'Synthetic correction', 'Unresolved', 'Unknown'])
        self.assertEqual(history[1][2:4], ['Unknown', 'PO-X'])

    def test_controller_only_changed_fields_and_explicit_null(self):
        row = synthetic_row()
        service = Mock()
        service.detail.return_value = row
        service.correct.return_value = row
        controller = ReviewController(service)
        values = invoice_view(row)['values']
        values[2], values[5], values[6] = '', '-0.01', '0.00'
        controller.save('hidden-id', values, 'Synthetic reviewer changes')
        service.correct.assert_called_once_with('hidden-id', {'po_id': None, 'unit_price_cents': -1, 'total_cents': 0}, 'Synthetic reviewer changes')
        service.correct.reset_mock()
        values[5] = '1.001'
        with self.assertRaises(ApplicationError):
            controller.save('hidden-id', values, 'Reject precision')
        service.correct.assert_not_called()

    def test_queue_friendly_names_currency_and_empty_filters(self):
        service = Mock()
        row = synthetic_row()
        service.invoices.return_value = [row]
        service.summary.return_value = {'overcharge_cents': 2800}
        controller = ReviewController(service)
        table, summary, ids = controller.queue('demo')
        self.assertEqual(table[0], ['demo.png', 'DEMO-1', 'SUP-A', 'Discrepant', 'Not reviewed', '$28.00'])
        self.assertEqual(ids, ['hidden-uuid'])
        table, _, _ = controller.queue('demo', 'processing_error')
        self.assertEqual(table, [])
        self.assertEqual(controller.queue(None), ([], {}, []))
        service.invoices.return_value = []
        self.assertEqual(controller.queue('empty')[0], [])

    def test_batch_outcome_table_plain_language(self):
        controller = ReviewController(Mock())
        files = [{'file_id': 'a', 'original_filename': 'a.png', 'status': 'success', 'duration_seconds': 1, 'issues': []},
                 {'file_id': 'b', 'original_filename': 'b.png', 'status': 'issues', 'duration_seconds': 1.1,
                  'issues': [{'field': 'po_id', 'code': 'missing'}]},
                 {'file_id': 'c', 'original_filename': 'c.png', 'status': 'error', 'mode': 'parser', 'error': 'OCR failed (InternalException)'}]
        table = controller.batch_table({'files': files})
        self.assertEqual([r[1] for r in table], ['Ready for review', 'Extracted; check issues', 'Processing failed'])
        self.assertIn('Purchase order', table[1][3])
        self.assertIn('clearer image', table[2][3])
        self.assertNotIn('InternalException', str(table))

    def test_summary_exact_supported_overcharge_and_exclusion_explanation(self):
        html = summary_html({'counts_by_status': {'reconciled': 1, 'discrepant': 2, 'duplicate': 3, 'unresolved': 4},
                             'processing_errors': 5, 'overcharge_cents': 9007199254740993})
        self.assertIn('$90,071,992,547,409.93', html)
        for label in ('Reconciled', 'Discrepant', 'Duplicate', 'Unresolved', 'Processing errors'):
            self.assertIn(label, html)
        self.assertIn('excluded', html)
        self.assertIn('Unavailable', summary_html({}))

    def test_components_have_no_json_and_select_rows_without_invoice_dropdown(self):
        ui = build_ui(Mock())
        components = ui.config['components']
        self.assertFalse(any(component['type'] == 'json' for component in components))
        labels = [c['props'].get('label') for c in components]
        self.assertNotIn('Selected invoice', labels)
        self.assertIn('Original extracted values', labels)
        self.assertIn('Correction history', labels)
        for field in FIELD_NAMES:
            self.assertNotIn(field, labels)
        accordions = [c['props'] for c in components if c['type'] == 'accordion']
        self.assertEqual([(c['label'], c['open']) for c in accordions], [('Original extracted values', False), ('Correction history', False), ('Upload invoices', False)])
        field_components = [c for c in components if c['type'] == 'textbox' and c['props'].get('label') in ('Unit price (USD)', 'Invoice total (USD)', 'Correction reason')]
        self.assertTrue(all(not c['props']['interactive'] for c in field_components))
        self.assertTrue(any(any(target[1] == 'select' for target in dependency['targets']) for dependency in ui.config['dependencies']))

    def test_badges_are_scoped_escaped_and_keep_explicit_labels(self):
        for status, label in [('reconciled', 'Reconciled'), ('discrepant', 'Discrepant'), ('duplicate', 'Duplicate'), ('unresolved', 'Unresolved'), ('processing_error', 'Processing error')]:
            badge = status_badge(status)
            self.assertIn('status-' + status, badge)
            self.assertIn(label, badge)
            row = synthetic_row()
            row['reconciliation']['status'] = status
            self.assertIn(badge, invoice_view(row)['heading'])
            self.assertIn('status-' + status, summary_html({}))
        self.assertIn('&lt;script&gt;', status_badge('discrepant', '<script>'))
        for color in ('#dcfce7', '#ffedd5', '#fee2e2', '#f3f4f6'):
            self.assertIn(color, STATUS_CSS)

    def test_empty_history_and_exact_zero_negative_differences(self):
        row = synthetic_row()
        view = invoice_view(row)
        self.assertFalse(view['history_visible'])
        self.assertEqual(view['history_empty'], '<p>No corrections yet</p>')
        for cents, expected in [(0, '$0.00'), (-1, '-$0.01')]:
            row['reconciliation']['difference_cents'] = cents
            self.assertEqual(invoice_view(row)['comparison'][-1], ['Difference', '—', '—', expected])
        row['correction_history'] = [{'timestamp': '2026-10-06T12:00:00+00:00', 'reason': 'Synthetic correction', 'changes': {'quantity': {'before': 2, 'after': 3}}, 'outcome': {'status': 'discrepant', 'difference_cents': -1}}]
        view = invoice_view(row)
        self.assertTrue(view['history_visible'])
        self.assertEqual(view['history_empty'], '')
        self.assertEqual(view['history'][0][-1], '-$0.01')

    def test_component_order_history_updates_and_existing_event_chains(self):
        service = Mock()
        ui = build_ui(service)
        components = ui.config['components']
        html = [c for c in components if c['type'] == 'html']
        heading = next(c for c in html if 'Select an invoice' in (c['props'].get('value') or ''))
        image = next(c for c in html if 'No invoice selected' in (c['props'].get('value') or ''))
        # Findings, issues and discrepancy note appear between heading and image.
        self.assertEqual(len([c for c in html if heading['id'] < c['id'] < image['id']]), 3)
        self.assertEqual(heading['props']['css_template'], STATUS_CSS)
        history = next(c for c in components if c['type'] == 'dataframe' and c['props'].get('label') == 'Correction history')
        self.assertFalse(history['props']['visible'])
        show = next(fn.fn for fn in ui.fns.values() if fn.fn.__name__ == 'show')
        row = synthetic_row()
        service.detail.return_value = row
        service.image.side_effect = ApplicationError(404, 'Unavailable')
        empty = show('first')
        self.assertFalse(empty[-2].visible)
        self.assertEqual(empty[-1], '<p>No corrections yet</p>')
        row['correction_history'] = [{'timestamp': '2026-10-06T12:00:00+00:00', 'reason': 'Synthetic correction', 'changes': {'quantity': {'before': 2, 'after': 3}}, 'outcome': {'status': 'discrepant', 'difference_cents': 0}}]
        updated = show('first')
        self.assertTrue(updated[-2].visible)
        self.assertEqual(updated[-1], '')
        service.detail.return_value = synthetic_row()
        self.assertFalse(show('second')[-2].visible)
        self.assertFalse(show(None)[-2].visible)
        functions = {fn.fn.__name__: idx for idx, fn in ui.fns.items() if fn.fn}
        deps = ui.config['dependencies']
        for name in ('select_row', 'correction', 'refresh_queue'):
            children = [d for d in deps if d.get('trigger_after') == functions[name]]
            self.assertTrue(children)
            self.assertTrue(all(ui.fns[d['id']].fn.__name__ == 'show' for d in children))
