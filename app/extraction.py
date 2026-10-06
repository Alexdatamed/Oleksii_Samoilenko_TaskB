"""Deterministic parser for the two supplied single-line invoice layouts.

Only recognized labels delimit values. Markdown heading markers and whitespace
are normalized; identifiers themselves are not corrected. Unsupported formats
or intervening prose fail conservatively rather than being guessed.
"""
from decimal import Decimal, localcontext
import re

from app.schemas import ExtractionIssue, InvoiceFields


_LABELS = re.compile(
    r'\b(?:'
    r'(?P<invoice_number>invoice(?:\s+(?:number|no\.?|id))?)|'
    r'(?P<supplier_id>supplier(?:\s+id)?)|'
    r'(?P<po_id>purchase\s+order(?:\s+id)?|po\s+id)|'
    r'(?P<sku>item\s*/\s*sku|sku)|'
    r'(?P<quantity>quantity)|'
    r'(?P<unit_price_cents>unit\s+price(?:\s*\(\s*USD\s*\))?)|'
    r'(?P<total_cents>total(?:\s*\(\s*USD\s*[)}])?)'
    r')'  # A following delimiter is checked below, not part of an ID.
    r'(?=\s|:|#|$)\s*(?::|#)?\s*(?:-\s+)?',
    re.IGNORECASE,
)
_FIELDS = ('invoice_number', 'supplier_id', 'po_id', 'sku', 'quantity',
           'unit_price_cents', 'total_cents')
_IDENTIFIERS = set(_FIELDS[:4])


def _parse_value(field, raw):
    if field in _IDENTIFIERS:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', raw):
            raise ValueError('Expected one identifier containing letters, digits, or . _ / -')
        return raw
    if field == 'quantity':
        if not re.fullmatch(r'[+-]?\d+', raw, flags=re.ASCII):
            raise ValueError('Quantity must be a signed or unsigned integer')
        return int(raw)
    if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', raw, flags=re.ASCII):
        raise ValueError('Expected a plain USD decimal amount')
    if '.' in raw and len(raw.split('.')[1]) > 2:
        raise ValueError('Monetary precision exceeds two decimal places; no rounding is allowed')
    # Set enough precision even for unusually large amounts; never use float.
    with localcontext() as context:
        context.prec = max(28, len(raw) + 2)
        return int(Decimal(raw) * Decimal(100))


def extract_invoice_fields(markdown, file_id):
    """Return (InvoiceFields, list[ExtractionIssue]).

    file_id is supplied unchanged by the caller, normally from the filename.
    Missing labels/values, '(missing)', invalid values, or conflicting repeated
    values produce issues and None for the affected field. Equal repeated
    parsed values are accepted. If any occurrence is invalid or missing, no
    other occurrence overrides it. Quantities, prices, and totals are taken
    independently from text; no business validity or status is determined.
    """
    if not isinstance(markdown, str):
        raise TypeError('markdown must be a string')
    text = re.sub(r'(?m)^[ \t]*#{1,6}[ \t]+', '', markdown)
    text = re.sub(r'\s+', ' ', text).strip()
    matches = list(_LABELS.finditer(text))
    occurrences = {field: [] for field in _FIELDS}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        occurrences[match.lastgroup].append(text[match.end():end].strip())
    fields = {'file_id': file_id}
    issues = []
    for field, raw_values in occurrences.items():
        values = []
        invalid = False
        if not raw_values:
            issues.append(ExtractionIssue(field=field, code='missing',
                                          message='Field label was not found'))
        for raw in raw_values:
            if not raw or raw.lower() == '(missing)':
                invalid = True
                issues.append(ExtractionIssue(field=field, code='missing',
                                              message='Field has no known value', raw_values=[raw]))
                continue
            try:
                values.append(_parse_value(field, raw))
            except ValueError as exc:
                invalid = True
                issues.append(ExtractionIssue(field=field, code='invalid',
                                              message=str(exc), raw_values=[raw]))
        distinct = set(values)
        if len(distinct) > 1:
            issues.append(ExtractionIssue(field=field, code='conflicting',
                                          message='Repeated field has conflicting values', raw_values=raw_values))
        fields[field] = values[0] if len(distinct) == 1 and not invalid else None
    return InvoiceFields(**fields), issues
