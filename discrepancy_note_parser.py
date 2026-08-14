import pdfplumber
import re
from datetime import datetime

TEXT_COL_STARTS = [
    ('sno', 8),
    ('sku_code_raw', 28),   # holds "106-DEMO-SKU-006" then "HSN: 22029090" on the next line
    ('sku_desc', 100),
    ('reason', 190),
    ('remarks', 265),
]

NUMERIC_FIELDS = [
    'exp_qty', 'dn_qty', 'lot_mrp', 'unit_price', 'taxable_value', 'tax_amount',
    'cgst_rate', 'cgst_amt', 'sgst_rate', 'sgst_amt',
    'igst_rate', 'igst_amt', 'cess_rate', 'cess_amt', 'total',
]
NUMERIC_ZONE_X0 = 330
DESC_GAP_THRESHOLD = 11


def _col_for_x(x0):
    col = TEXT_COL_STARTS[0][0]
    for name, start in TEXT_COL_STARTS:
        if x0 >= start - 6:
            col = name
        else:
            break
    return col


def _num(s):
    if s is None:
        return None
    s = s.replace(',', '').strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), '%d-%m-%Y').date().isoformat()
    except ValueError:
        return s


def parse_discrepancy_note_pdf(file_path):
    with pdfplumber.open(file_path) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        header = {}
        patterns = {
            'dn_number': r'DN No\s*:-\s*(\S+)',
            'dn_date': r'DN Date\s*:-\s*([\d\-]+)',
            'inbound_no': r'Inbound No\s*:-\s*(\S+)',
            'grn_number': r'GRN No\s*:-\s*(\S+)',
            'grn_date': r'GRN date\s*:-\s*([\d\-]+)',
            'invoice_number': r'Invoice No\s*:-\s*(\S+)',
            'invoice_date': r'Invoice Date:-\s*([\d\-]+)',
            'po_number': r'PO No\s*:-\s*(\S+)',
            'po_date': r'PO Date\s*:-\s*([\d\-]+)',
            'grn_qty': r'GRN Qty\s*-\s*([\d.]+)',
            'grn_amt': r'GRN Amt\s*-\s*([\d.,]+)',
            'total_dn_qty': r'Total DN Qty\s*([\d.]+)',
            'dn_amt': r'DN Amt\s*-\s*([\d.,]+)',
            'invoice_amt': r'Invoice Amt\s*-\s*([\d.,]+)',
        }
        for key, pat in patterns.items():
            m = re.search(pat, full_text)
            header[key] = m.group(1).strip() if m else None

        for key in ['grn_qty', 'grn_amt', 'total_dn_qty', 'dn_amt', 'invoice_amt']:
            header[key] = _num(header.get(key))
        for key in ['dn_date', 'grn_date', 'invoice_date', 'po_date']:
            header[key] = _parse_date(header.get(key))

        m = re.search(r'Vendor Name\s*:-\s*(.+?)\s*DN No', full_text)
        header['vendor_name'] = m.group(1).strip() if m else None

        items = []
        for page in pdf.pages[:1]:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            table_words = [w for w in words if w['top'] > 330]

            desc_words = sorted(
                (w for w in table_words if 100 <= w['x0'] < 190),
                key=lambda w: (w['top'], w['x0']),
            )
            if not desc_words:
                continue

            blocks = []
            current = [desc_words[0]]
            for w in desc_words[1:]:
                if w['top'] - current[-1]['top'] > DESC_GAP_THRESHOLD:
                    blocks.append(current)
                    current = [w]
                else:
                    current.append(w)
            blocks.append(current)

            for block in blocks:
                top_start = min(w['top'] for w in block) - 2
                top_end = max(w['top'] for w in block) + 2
                row_words = [w for w in table_words if top_start <= w['top'] <= top_end]

                text_words = [w for w in row_words if w['x0'] < NUMERIC_ZONE_X0]
                cols = {}
                for w in text_words:
                    col = _col_for_x(w['x0'])
                    cols.setdefault(col, []).append((w['top'], w['x0'], w['text']))

                def joined(col):
                    vals = sorted(cols.get(col, []), key=lambda v: (v[0], v[1]))
                    return ' '.join(v[2] for v in vals).strip()

                sku_code_raw = joined('sku_code_raw')
                sku_code, hsn_code = sku_code_raw, None
                if 'HSN:' in sku_code_raw:
                    sku_code, hsn_part = sku_code_raw.split('HSN:', 1)
                    sku_code = sku_code.strip()
                    hsn_code = hsn_part.strip()

                numeric_words = sorted(
                    (w for w in row_words if w['x0'] >= NUMERIC_ZONE_X0),
                    key=lambda w: w['x0'],
                )
                numeric_vals = [_num(w['text']) for w in numeric_words]

                item = {
                    'sno': joined('sno'),
                    'sku_code': sku_code,
                    'hsn_code': hsn_code,
                    'sku_desc': joined('sku_desc'),
                    'reason': joined('reason'),
                    'remarks': joined('remarks').replace('- ', '-'),
                }
                for i, field in enumerate(NUMERIC_FIELDS):
                    item[field] = numeric_vals[i] if i < len(numeric_vals) else None

                sku_clean = (item['sku_code'] or '').replace(' ', '')
                if re.search(r'\d{4,7}', sku_clean) and item['dn_qty'] is not None:
                    item['sku_code'] = sku_clean
                    items.append(item)

        header['line_items'] = items
        return header


if __name__ == '__main__':
    import json
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else 'demo_discrepancy_note.pdf'
    print(json.dumps(parse_discrepancy_note_pdf(path), indent=2))
