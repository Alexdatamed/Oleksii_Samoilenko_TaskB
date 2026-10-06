"""Shared extraction schema; values are invoice evidence, not business decisions."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InvoiceFields(BaseModel):
    """Unknown fields remain None; money is integer USD cents.

    The public field is unit_price_cents. Use to_reconciliation_dict() for the
    existing reconciler, whose input field is named unit_cents. No values are
    filled, calculated, or corrected by that adapter.
    """
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)

    file_id: str = Field(min_length=1)
    invoice_number: str | None = Field(default=None, min_length=1)
    supplier_id: str | None = Field(default=None, min_length=1)
    po_id: str | None = Field(default=None, min_length=1)
    sku: str | None = Field(default=None, min_length=1)
    quantity: int | None = None
    unit_price_cents: int | None = None
    total_cents: int | None = None

    @field_validator('file_id', 'invoice_number', 'supplier_id', 'po_id', 'sku')
    @classmethod
    def reject_blank_strings(cls, value):
        if value is not None and not value.strip():
            raise ValueError('Blank strings are not known identifiers; use None')
        return value

    def to_reconciliation_dict(self):
        result = self.model_dump()
        result['unit_cents'] = result.pop('unit_price_cents')
        return result


class ExtractionIssue(BaseModel):
    """An evidence problem independent of invoice reconciliation status."""
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)
    field: Literal['invoice_number', 'supplier_id', 'po_id', 'sku', 'quantity',
                   'unit_price_cents', 'total_cents']
    code: Literal['missing', 'invalid', 'conflicting']
    message: str
    raw_values: list[str] = Field(default_factory=list)
