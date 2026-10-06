"""Render exactly two fictional demo invoices; writes stay beside this script.

Adapted from the supplied renderer. Exact signed integer-cent formatting.
"""
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent


def dollars(cents):
    if type(cents) is not int:
        raise ValueError('Monetary fixture values must be integer cents')
    whole, fraction = divmod(abs(cents), 100)
    return f"{'-' if cents < 0 else ''}{whole}.{fraction:02d}"


def render():
    invoices = json.loads((ROOT/'seed.json').read_text(encoding='utf-8'))['invoices']
    if {invoice['file_id'] for invoice in invoices} != {'quantity-mismatch','sku-mismatch'} or len(invoices) != 2:
        raise ValueError('Renderer supports only the two fixed additional demo cases')
    font, title = ImageFont.load_default(size=22), ImageFont.load_default(size=32)
    output = ROOT/'images'; output.mkdir(exist_ok=True)
    for inv in invoices:
        im = Image.new('RGB',(950,650),'#ffffff'); d=ImageDraw.Draw(im)
        b=inv['layout']=='b'
        d.rectangle((0,0,950,100),fill='#e4edf5' if b else '#edf0e6')
        d.text((40,30),'FICTIONAL INVOICE - '+inv['invoice_number'],font=title,fill='black')
        fields=[('Supplier',inv['supplier_id']),('Purchase order',inv['po_id'] or '(missing)'),('Item / SKU',inv['sku']),('Quantity',str(inv['quantity'])),('Unit price (USD)',dollars(inv['unit_cents'])),('Total (USD)',dollars(inv['total_cents']))]
        for i,(key,value) in enumerate(fields):
            x,y=(60,145+i*66) if not b else (480 if i%2 else 40,155+(i//2)*125)
            d.text((x,y),f'{key}: {value}',font=font,fill='black')
        im.save(output/(inv['file_id']+'.png'))
        print(output.relative_to(ROOT).as_posix()+'/'+inv['file_id']+'.png')


if __name__=='__main__':
    render()
