import pdfplumber
import re
from datetime import datetime

COL_STARTS = [
    ('sno', 15),
    ('item_code', 38),
    ('item_desc', 84),
    ('hsn_code', 140),
    ('qty', 180),
    ('mrp', 217),
    ('unit_base_cost', 280),
    ('taxable_value', 358),
    ('cgst_rate', 424),
    ('cgst_amt', 460),
    ('sgst_rate', 498),
    ('sgst_amt', 534),
    ('igst_rate', 571),
    ('igst_amt', 607),
    ('cess_rate', 645),
    ('cess_amt', 681),
    ('add_cess', 718),
    ('total', 777),
]

DESC_GAP_THRESHOLD = 9.5  # pt gap that signals a new item description block


def _col_for_x(x0):
    col = COL_STARTS[0][0]
    for name, start in COL_STARTS:
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
        return datetime.strptime(s.strip(), '%b %d, %Y').date().isoformat()
    except ValueError:
        return s


def parse_po_pdf(file_path):
    with pdfplumber.open(file_path) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        header = {}
        patterns = {
            'po_number': r'PO No\s*:\s*(\S+)',
            'po_date': r'PO Date\s*:\s*([A-Za-z]+ \d{1,2},\s*\d{4})',
            'po_release_date': r'PO Release Date\s*:\s*([A-Za-z]+ \d{1,2},\s*\d{4})',
            'payment_terms': r'Payment Terms\s*:\s*(.+)',
            'expected_delivery_date': r'Expected Delivery Date:\s*([A-Za-z]+ \d{1,2},\s*\d{4})',
            'po_expiry_date': r'PO Expiry Date:\s*([A-Za-z]+ \d{1,2},\s*\d{4})',
            'vendor_gstin': r'GSTIN\s*:(\w{15})',
            'grand_total': r'Grand Total \(INR\)\s*([\d.,]+)',
        }
        for key, pat in patterns.items():
            m = re.search(pat, full_text)
            header[key] = m.group(1).strip() if m else None

        for k in ['po_date', 'po_release_date', 'expected_delivery_date', 'po_expiry_date']:
            header[k] = _parse_date(header.get(k))
        header['grand_total'] = _num(header.get('grand_total'))

        # vendor name + shipping facility, via word positions (left column vs header labels)
        first_page = pdf.pages[0]
        words = first_page.extract_words(use_text_flow=False, keep_blank_chars=False)
        vendor_label_top = next((w['top'] for w in words if w['text'] == 'Vendor' and w['x0'] < 100), None)
        header['vendor_name'] = None
        if vendor_label_top is not None:
            candidate_tops = sorted({w['top'] for w in words if w['x0'] < 400 and w['top'] > vendor_label_top})
            if candidate_tops:
                name_top = candidate_tops[0]
                name_words = sorted(
                    (w for w in words if w['x0'] < 400 and abs(w['top'] - name_top) < 1),
                    key=lambda w: w['x0'],
                )
                header['vendor_name'] = ' '.join(w['text'] for w in name_words)

        # Facility name (Scootsy's receiving warehouse code, e.g. "DEMO FACILITY A")
        # -- printed as the first part of the "Shipping Address" block,
        # right column. Layout: label row ("Billing Address" | "Shipping
        # Address"), then a company-name row (always "SCOOTSY LOGISTICS
        # PRIVATE LIMITED"), then the facility code + street address row
        # ("DEMO FACILITY A, B-400, ..."). We only want the code before the first
        # comma, not the full address. The "Billing"/"Shipping" labels sit
        # roughly centered over their column and start well to the right
        # of the column's actual left-aligned content below, so the split
        # between the two columns is the midpoint between the two labels'
        # x-positions, not either label's own x0.
        billing_label = next((w for w in words if w['text'] == 'Billing' and w['top'] < 200), None)
        shipping_label = next((w for w in words if w['text'] == 'Shipping' and w['top'] < 200), None)
        header['facility_name'] = None
        if shipping_label is not None:
            col_split = ((billing_label['x0'] if billing_label else 0) + shipping_label['x0']) / 2
            label_top = shipping_label['top']
            candidate_tops = sorted({
                w['top'] for w in words
                if w['x0'] > col_split and label_top < w['top'] <= label_top + 60
            })
            if len(candidate_tops) >= 2:
                addr_top = candidate_tops[1]  # skip the company-name row
                addr_words = sorted(
                    (w for w in words if w['x0'] > col_split and abs(w['top'] - addr_top) < 1),
                    key=lambda w: w['x0'],
                )
                addr_line = ' '.join(w['text'] for w in addr_words)
                header['facility_name'] = addr_line.split(',')[0].strip() or None

        # line items, row-span driven by the (multi-line) item_desc column
        items = []
        for page in pdf.pages[:1]:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            table_words = [w for w in words if w['top'] > 230]

            desc_words = sorted(
                (w for w in table_words if 84 <= w['x0'] < 140),
                key=lambda w: (w['top'], w['x0']),
            )
            if not desc_words:
                continue

            # group desc words into per-item blocks using vertical gaps
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

                cols = {}
                for w in row_words:
                    col = _col_for_x(w['x0'])
                    cols.setdefault(col, []).append((w['top'], w['x0'], w['text']))

                def joined(col):
                    vals = sorted(cols.get(col, []), key=lambda v: (v[0], v[1]))
                    return ' '.join(v[2] for v in vals).strip()

                def single_num(col):
                    vals = sorted(cols.get(col, []), key=lambda v: (v[0], v[1]))
                    for v in vals:
                        n = _num(v[2])
                        if n is not None:
                            return n
                    return None

                item = {
                    'sno': joined('sno'),
                    'item_code': joined('item_code'),
                    'item_desc': joined('item_desc'),
                    'hsn_code': joined('hsn_code'),
                    'qty': single_num('qty'),
                    'mrp': single_num('mrp'),
                    'unit_base_cost': single_num('unit_base_cost'),
                    'taxable_value': single_num('taxable_value'),
                    'cgst_rate': single_num('cgst_rate'),
                    'cgst_amt': single_num('cgst_amt'),
                    'sgst_rate': single_num('sgst_rate'),
                    'sgst_amt': single_num('sgst_amt'),
                    'igst_rate': single_num('igst_rate'),
                    'igst_amt': single_num('igst_amt'),
                    'cess_rate': single_num('cess_rate'),
                    'cess_amt': single_num('cess_amt'),
                    'add_cess': single_num('add_cess'),
                    'total': single_num('total'),
                }
                sku_clean = item['item_code'].replace(' ', '')
                if re.fullmatch(r'\d{4,7}', sku_clean) and item['qty'] is not None:
                    item['item_code'] = sku_clean
                    items.append(item)

        header['line_items'] = items
        return header


if __name__ == '__main__':
    import json
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else 'demo_purchase_order.pdf'
    result = parse_po_pdf(path)
    print(json.dumps(result, indent=2))
