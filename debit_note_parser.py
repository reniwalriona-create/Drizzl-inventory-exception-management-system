import pdfplumber
import re
from datetime import datetime

DESC_GAP_THRESHOLD = 11
NUMERIC_ZONE_X0 = 370


def _num(s):
    if s is None:
        return None
    s = s.replace(',', '').replace('₹', '').strip()
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


def parse_debit_note_pdf(file_path):
    with pdfplumber.open(file_path) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        header = {}
        patterns = {
            'note_number': r'Note#\s*(\S+)',
            'reference_number': r'Reference number\s*:\s*(\S+)',
            'discrepancy_type': r'Discrepancy Type\s*:\s*(\S+)',
            'note_date': r'Date\s*:\s*([\d\-]+)',
            'sub_total': r'Sub Total\s*([\d,\.]+)',
            'total_amount': r'Total\s*₹\s*([\d,\.]+)',
            'credits_remaining': r'Credits Remaining\D*([\d,\.]+)',
            'po_number': r'Po No\s*:\s*(\S+)',
            'invoice_number': r'Invoice No\s*:\s*(\S+)',
        }
        for key, pat in patterns.items():
            m = re.search(pat, full_text)
            header[key] = m.group(1).strip() if m else None

        for key in ['sub_total', 'total_amount', 'credits_remaining']:
            header[key] = _num(header.get(key))
        header['note_date'] = _parse_date(header.get('note_date'))
        header['tax_amount'] = (
            round(header['total_amount'] - header['sub_total'], 2)
            if header['total_amount'] is not None and header['sub_total'] is not None
            else None
        )

        items = []
        for page in pdf.pages[:1]:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            table_words = [w for w in words if w['top'] > 300]

            desc_words = sorted(
                (w for w in table_words if 90 <= w['x0'] < 370),
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

                sno_words = sorted(
                    (w for w in row_words if w['x0'] < 90),
                    key=lambda w: (w['top'], w['x0']),
                )
                desc_row_words = sorted(
                    (w for w in row_words if 90 <= w['x0'] < NUMERIC_ZONE_X0),
                    key=lambda w: (w['top'], w['x0']),
                )
                numeric_words = sorted(
                    (w for w in row_words if w['x0'] >= NUMERIC_ZONE_X0),
                    key=lambda w: w['x0'],
                )
                numeric_vals = [_num(w['text']) for w in numeric_words]

                # Rows without their own qty/rate/amount (e.g. a stray
                # heading line) aren't real line items -- skip them.
                if len(numeric_vals) < 3:
                    continue

                item = {
                    'sno': ' '.join(w['text'] for w in sno_words).strip(),
                    'description': ' '.join(w['text'] for w in desc_row_words).strip(),
                    'qty': numeric_vals[0],
                    'rate': numeric_vals[1],
                    'amount': numeric_vals[2],
                }
                # Stray rows (e.g. the "Credits Applied Bills" section on
                # updated snapshots) land in this same x-range but have no
                # real qty/rate/amount -- only keep genuine line items.
                if item['amount'] is not None:
                    items.append(item)

        header['line_items'] = items
        return header


if __name__ == '__main__':
    import json
    import sys
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python debit_note_parser.py <synthetic-debit-note.pdf>")
    path = sys.argv[1]
    print(json.dumps(parse_debit_note_pdf(path), indent=2))
