"""Render the fictional invoices from seed.json. Requires Pillow (PIL)."""
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
root = Path(__file__).resolve().parent
font = ImageFont.load_default(size=22)
title = ImageFont.load_default(size=32)
(root / 'images').mkdir(exist_ok=True)
for inv in json.loads((root / 'seed.json').read_text())['invoices']:
    im = Image.new('RGB', (950, 650), '#ffffff')
    d = ImageDraw.Draw(im)
    b = inv['layout'] == 'b'
    d.rectangle((0, 0, 950, 100), fill='#e4edf5' if b else '#edf0e6')
    d.text((40, 30), 'FICTIONAL INVOICE - ' + inv['invoice_number'], font=title, fill='black')
    fields = [('Supplier', inv['supplier_id']), ('Purchase order', inv['po_id'] or '(missing)'), ('Item / SKU', inv['sku']), ('Quantity', str(inv['quantity'])), ('Unit price (USD)', f"{inv['unit_cents']/100:.2f}"), ('Total (USD)', f"{inv['total_cents']/100:.2f}")]
    for i, (key, value) in enumerate(fields):
        x, y = (60, 145+i*66) if not b else (480 if i%2 else 40, 155+(i//2)*125)
        d.text((x,y), f'{key}: {value}', font=font, fill='black')
    im.save(root / 'images' / (inv['file_id'] + '.png'))
