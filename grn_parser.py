import pdfplumber
import re
from datetime import datetime

# Text columns (left of the numeric section) are matched by x-position,
# same technique as po_parser.py, since sku_desc wraps across lines.
TEXT_COL_STARTS = [
    ('sno', 14),
    ('sku_code', 30),
    ('sku_desc', 66),
    ('vendor_sku', 140),
    ('bin', 178),
    ('lot_no', 224),
]

# The numeric section (Lot MRP onward) always has these 15 values, always
# present (zeros instead of blanks), always in this order -- so instead of
# fragile x-position thresholds (numbers are right-aligned, so their x0
# shifts with digit count), we just take them positionally, left to right.
NUMERIC_FIELDS = [
    'lot_mrp', 'expected_qty', 'received_qty', 'unit_price', 'taxable_value',
    'cgst_rate', 'cgst_amt', 'sgst_rate', 'sgst_amt',
    'igst_rate', 'igst_amt', 'cess_rate', 'cess_amt', 'add_cess', 'total',
]
NUMERIC_ZONE_X0 = 285

DESC_GAP_THRESHOLD = 9.5


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


def parse_grn_pdf(file_path):
    with pdfplumber.open(file_path) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        header = {}
        patterns = {
            'grn_number': r'GRN No\s*:-\s*(\S+)',
            'grn_date': r'GRN Date\s*:-\s*([\d\-]+)',
            'po_number': r'PO No\s*:-\s*(\S+)',
            'po_date': r'PO Date\s*:-\s*([\d\-]+)',
            'inbound_no': r'Inbound No\s*:-\s*(\S+)',
            'create_date': r'Create Date\s*:-\s*([\d\-]+)',
            'invoice_no': r'Invoice No\s*:-\s*(\S+)',
            'invoice_date': r'Invoice Date:-\s*([\d\-]+)',
            'challan_date': r'Challan date\s*:-\s*(\S+)',
        }
        for key, pat in patterns.items():
            m = re.search(pat, full_text)
            header[key] = m.group(1).strip() if m else None

        # Challan No is often left blank on this template, in which case the
        # regex above would grab the next label ("Challan") by mistake.
        m = re.search(r'Challan No\s*:-\s*(\S+)', full_text)
        challan_no = m.group(1).strip() if m else None
        header['challan_no'] = None if challan_no == 'Challan' else challan_no

        m = re.search(r'Vendor Name\s*:-\s*(.+?)\s*PO No', full_text)
        header['vendor_name'] = m.group(1).strip() if m else None

        for k in ['grn_date', 'po_date', 'create_date', 'invoice_date']:
            header[k] = _parse_date(header.get(k))

        # Facility name: unlike the PO PDF (which prints a short code like
        # "DEMO FACILITY A"), the GRN PDF doesn't have a compact facility code
        # anywhere -- only the receiving warehouse operator's name, on the
        # second line of the page (line 1 is always the constant "SCOOTSY
        # LOGISTICS PRIVATE LIMITED" boilerplate). This is a weaker signal
        # than the PO's -- an operator name, not a facility code -- but
        # it's the only facility-identifying text this document has.
        lines = [l.strip() for l in full_text.splitlines() if l.strip()]
        header['facility_name'] = None
        if len(lines) >= 2 and lines[0] == 'SCOOTSY LOGISTICS PRIVATE LIMITED':
            header['facility_name'] = lines[1]

        # line items, row-span driven by the (multi-line) sku_desc column
        items = []
        for page in pdf.pages[:1]:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            table_words = [w for w in words if w['top'] > 300]

            desc_words = sorted(
                (w for w in table_words if 66 <= w['x0'] < 140),
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

                numeric_words = sorted(
                    (w for w in row_words if w['x0'] >= NUMERIC_ZONE_X0),
                    key=lambda w: w['x0'],
                )
                numeric_vals = [_num(w['text']) for w in numeric_words]

                item = {
                    'sno': joined('sno'),
                    'sku_code': joined('sku_code'),
                    'sku_desc': joined('sku_desc'),
                    'vendor_sku': joined('vendor_sku'),
                    'bin': joined('bin'),
                    'lot_no': joined('lot_no'),
                }
                for i, field in enumerate(NUMERIC_FIELDS):
                    item[field] = numeric_vals[i] if i < len(numeric_vals) else None

                sku_clean = item['sku_code'].replace(' ', '')
                if re.fullmatch(r'\d{4,7}', sku_clean) and item['received_qty'] is not None:
                    item['sku_code'] = sku_clean
                    items.append(item)

        header['line_items'] = items
        return header


if __name__ == '__main__':
    import json
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else 'GRN_DEMO000001 (1).pdf'
    result = parse_grn_pdf(path)
    print(json.dumps(result, indent=2))
