"""The Urdu bill is its own sheet — not the English receipt with translated labels.

The sheet is defined by ``app/services/urdu_receipt.py`` and measured off the
approved reference PDF ("New Receipt.pdf"): an A5 page whose header carries a
QR block, the Nastaleeq shop name, the proprietor and the phones over a thick
navy rule and a light dashed companion; the shop note above a four-cell meta
strip; two pale party panels; a roof-ruled item table; the summary box on the
left with the signature rule on the right; and the terms in two numbered
columns.

These tests pin that document, so a later change to the English receipt (or to
the label dictionary) cannot quietly reshape it, and so "Urdu Print" and
"Urdu PDF" stay the same page.  The default is the single approved A5 sheet;
the optional carbon copy places two identical A5 copies on one A4 landscape
leaf.
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from app.services import bill_urdu
from app.services import urdu_receipt as R
from app.services.urdu_text import harfbuzz_available

# HarfBuzz is an optional native wheel (see requirements-pdf.txt).  A host
# without it still renders a legible sheet through the fontTools fallback, but
# the shaping metrics differ, so the reference-geometry assertions below only
# apply where the real shaper is installed.
requires_harfbuzz = pytest.mark.skipif(
    not harfbuzz_available(),
    reason="reference layouts are measured with HarfBuzz shaping (uharfbuzz missing)",
)


ITEMS = [
    {'name_ur': 'ڈی جی', 'qty': 1, 'price_at_time': 2000},
    {'name_ur': 'سیمنٹ ٹھیکہ', 'qty': 50, 'price_at_time': 1290},
    {'name_ur': '12 ملی میٹر سریا', 'qty': 1964, 'price_at_time': 224},
    {'name_ur': 'چناب بریٹ ٹرالی', 'qty': 2, 'price_at_time': 12500},
    {'name_ur': 'بجری ٹرالی', 'qty': 2, 'price_at_time': 14000},
    {'name_ur': 'اینٹ 1000', 'qty': 3, 'price_at_time': 18000},
    {'name_ur': 'کرایہ سیمنٹ', 'qty': 50, 'price_at_time': 30},
]

NOTE = ('سیمنٹ، سریا، اینٹ، ٹائل، چوگھٹ پیٹی، اینگل، بجری، '
        'چناب بریٹ وغیرہ تعمیراتی سامان دستیاب ہے')

TERMS = [
    'بکنگ پر خریدار کو دیا گیا ریٹ صرف اسی صورت میں لاگو ہوگا جب وہ اس بکنگ کی پوری رقم ادا کرے۔',
    'اگر بکنگ کی رقم جزوی ادا کی جائے تو بکنگ کا ریٹ صرف ادا شدہ رقم کے مطابق مال پر لاگو ہوگا۔',
    'بقیہ رقم/بکنگ کے بقیہ مال پر مارکیٹ کا نیا ریٹ لاگو ہوگا۔',
    'اگر کسی قسم کا کوئی فرق پایا جائے تو 10 دن کے اندر رابطہ کریں۔',
    'مال واپس یا تبدیل نہیں ہوگا، سوائے ڈیلیوری کے وقت ثابت شدہ نقص کے۔',
    'اتروائی کے بعد مال کی ٹوٹ پھوٹ، نقصان یا چوری کی ذمہ داری کمپنی پر نہیں۔',
    'ادھار کی صورت میں بقایا رقم باہمی طے شدہ مدت میں ادا کرنا لازم ہے۔',
]


# The complete item list and figures visible in the supplied New Receipt.pdf.
# Keep this separate from the smaller everyday fixture above: these values are
# a layout reference, never defaults injected into a user's real bill.
REFERENCE_ITEMS = [
    {'name_ur': 'ڈی جی', 'qty': 1, 'price_at_time': 2000},
    {'name_ur': 'سیمنٹ ٹھیکہ', 'qty': 50, 'price_at_time': 1290},
    {'name_ur': '12 ملی میٹر سریا', 'qty': 1964, 'price_at_time': 224},
    {'name_ur': 'چناب بریٹ ٹرالی', 'qty': 2, 'price_at_time': 12500},
    {'name_ur': 'بجری ٹرالی', 'qty': 2, 'price_at_time': 14000},
    {'name_ur': 'اینٹ 1000', 'qty': 3, 'price_at_time': 18000},
    {'name_ur': 'کرایہ سیمنٹ', 'qty': 50, 'price_at_time': 30},
    {'name_ur': 'اٹنی', 'qty': 5000, 'price_at_time': 25},
    {'name_ur': 'ٹائل 12×18', 'qty': 40, 'price_at_time': 1950},
    {'name_ur': 'اینگل 2 انچ', 'qty': 12, 'price_at_time': 6500},
    {'name_ur': 'چوگھٹ پٹی', 'qty': 60, 'price_at_time': 1150},
    {'name_ur': '10 ملی میٹر سریا', 'qty': 500, 'price_at_time': 226},
    {'name_ur': 'بائنڈنگ تار', 'qty': 15, 'price_at_time': 380},
    {'name_ur': 'چونا بوری', 'qty': 20, 'price_at_time': 450},
    {'name_ur': 'ماربل پاؤڈر بوری', 'qty': 25, 'price_at_time': 520},
]


def _reference_document():
    return _document(
        bill=_bill(amount=1105636, paid_amount=614936),
        items=REFERENCE_ITEMS,
        terms=[SimpleNamespace(title='', content_ur=text) for text in TERMS],
    )


def _bill(**over):
    data = dict(
        manual_bill_no='SB-SL-1008',
        amount=614936,
        paid_amount=0,
        discount=0,
        date_posted=datetime(2026, 11, 7, 12, 20),
        method='Cash',
        account_name='Drawer Cash',
        account_no='',
        bank_name='',
        payment_type='Receipt',
        note=NOTE,
        client_name='Muhammad Shafiq',
        client_code='FBMCL-00002',
        driver_name='عدنان احمد',
    )
    data.update(over)
    return SimpleNamespace(**data)


def _settings(**over):
    data = dict(
        company_name='FAZAL BUILDING MATERIALS',
        company_address='JALAL PUR SOBTIAN GUJRAT',
        company_phone='+92331-0000993 | +92340-3872722',
        proprietor_name_ur='عدنان احمد',
        company_note_ur='',
    )
    data.update(over)
    return SimpleNamespace(**data)


def _document(**over):
    options = dict(
        bill_type='DirectSale',
        bill=_bill(),
        items=ITEMS,
        previous_balance=31700,
        client_name_ur='محمد شفیق',
        delivery_people=['عدنان احمد'],
        driver_name_ur='عدنان احمد',
        settings=_settings(),
    )
    options.update(over)
    return bill_urdu.build_document(**options)


def _pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return '\n'.join(page.extract_text() or '' for page in reader.pages)


def _ops(sheet, kind):
    return [op for op in sheet.ops if op['op'] == kind]


# ---------------------------------------------------------------------------
# Paper: one A5 sheet, or the carbon-copy A4 leaf
# ---------------------------------------------------------------------------

def test_single_copy_is_one_a5_page():
    data = bill_urdu.render_pdf(_document(), copies=1)
    assert data and data.startswith(b'%PDF-')

    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 1
    box = reader.pages[0].mediabox
    assert round(float(box.width), 1) == round(R.PAGE_W, 1)
    assert round(float(box.height), 1) == round(R.PAGE_H, 1)


def test_default_is_the_single_approved_a5_sheet():
    data = bill_urdu.render_pdf(_document())          # copies default to 1
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 1
    box = reader.pages[0].mediabox
    assert round(float(box.width), 1) == round(R.PAGE_W, 1)
    assert round(float(box.height), 1) == round(R.PAGE_H, 1)


def test_carbon_copy_is_two_a5_copies_on_one_a4_leaf():
    data = bill_urdu.render_pdf(_document(), copies=2)
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 1, 'a carbon copy is one leaf, not two pages'
    box = reader.pages[0].mediabox
    assert round(float(box.width), 1) == round(R.A4_W, 1)
    assert round(float(box.height), 1) == round(R.A4_H, 1)
    # A4 landscape holds two A5 portraits side by side, with even margins
    assert R.A4_W >= 2 * R.PAGE_W
    assert R.A4_W - 2 * R.PAGE_W < 4.0


def test_both_copies_hold_the_very_same_marks():
    doc = _document()
    content = R.layout_document(doc)[0]
    page = R.compose_document(doc, copies=2)[0]

    offset = (R.A4_W - 2 * R.PAGE_W) / 2.0
    text_ops = [op for op in page.ops if op['op'] == 'text']
    for op in content.ops:
        if op['op'] != 'text':
            continue
        for dx in (offset, offset + R.PAGE_W):
            assert any(abs(t['y'] - op['y']) < 1e-6
                       and abs(t['x'] - (op['x'] + dx)) < 1e-6
                       and t['text'] == op['text'] for t in text_ops), op


def test_copies_carry_the_original_and_the_carbon_marks():
    page = R.compose_document(_document(), copies=2)[0]
    labels = [op['text'] for op in page.ops if op['op'] == 'text']
    assert R.COPY_ORIGINAL in labels
    assert R.COPY_CARBON in labels


def test_single_copy_has_no_cut_line_or_copy_marks():
    page = R.compose_document(_document(), copies=1)[0]
    labels = [op['text'] for op in page.ops if op['op'] == 'text']
    assert R.COPY_ORIGINAL not in labels
    assert not [op for op in page.ops
                if op.get('dash') and op['op'] in ('line', 'seg')
                and abs(op['x0'] - op['x1']) < 0.01], 'no cut line on one copy'


# ---------------------------------------------------------------------------
# Geometry: every block at its reference anchor
# ---------------------------------------------------------------------------

@requires_harfbuzz
def test_sheet_anchors_match_the_reference_bill():
    ops = R.layout_document(_document())[0].ops
    rects = _ops(R.layout_document(_document())[0], 'rect')
    lines = _ops(R.layout_document(_document())[0], 'line')

    # the thick navy rule and its light dashed companion under the header
    assert any(abs(op['y'] - R.Y_RULE_1) < 0.01 and abs(op['h'] - R.RULE_1_H) < 0.01
               and op['fill'] == R.NAVY for op in rects), 'header rule missing'
    assert any(abs(op['y0'] - R.Y_RULE_2) < 0.01 and op.get('dash')
               and op['color'] == R.DOT for op in lines), 'dashed header rule missing'
    # the shop note sits above the four-cell meta strip
    assert any(abs(op['y'] - R.Y_META) < 0.01 and abs(op['h'] - R.META_H) < 0.01
               for op in rects), 'meta strip missing'
    # the two pale party panels
    panels = [op for op in rects if abs(op['y'] - R.Y_PANEL) < 0.01
              and abs(op['h'] - R.PANEL_H) < 0.01 and op['fill'] == R.PANEL]
    assert len(panels) == 2, 'two party panels missing'
    assert any(abs(op['x'] - R.LEFT) < 0.01 for op in panels)
    assert any(abs(op['x'] - R.PANEL_RIGHT_X) < 0.01 for op in panels)
    # the navy item header row
    assert any(abs(op['y'] - R.Y_ITEMS_HEAD) < 0.01
               and abs(op['h'] - R.ITEMS_HEAD_H) < 0.01
               and op['fill'] == R.NAVY for op in rects), 'item header missing'
    # the summary box on the left half, closed by the navy total bar
    assert any(abs(op['x'] - (R.SUM_LEFT + 0.375)) < 0.01
               and abs(op['w'] - (R.SUM_RIGHT - R.SUM_LEFT - 0.75)) < 0.01
               and op['fill'] == R.NAVY for op in rects), 'summary bar missing'
    # the signature rule on the right
    assert any(abs(op['y0'] - op['y1']) < 0.01
               and abs(op['x0'] - R.SIG_LINE_X0) < 0.01
               and abs(op['x1'] - R.SIG_LINE_X1) < 0.01
               for op in lines), 'signature rule missing'
    assert ops, 'the sheet is empty'


@requires_harfbuzz
def test_item_table_columns_are_the_reference_widths():
    columns = R._item_columns()
    assert [c['label'] for c in columns] == [
        'رقم (روپے)', 'ریٹ', 'مقدار', 'تفصیل / آئٹم', 'نمبر']
    assert [round(c['x0'], 1) for c in columns] == [
        round(R.LEFT, 1), 114.8, 187.0, 243.9, 369.3]
    assert round(columns[-1]['x1'], 1) == round(R.RIGHT, 1)


@requires_harfbuzz
def test_fifteen_rows_end_where_the_reference_table_ends():
    items = [{'name_ur': 'اینٹ', 'qty': 1, 'price_at_time': 10}] * 15
    sheet = R.layout_document(_document(items=items))[0]
    lines = _ops(sheet, 'line')
    # the reference's ruled table closes at y = 428.79 and the summary starts
    # 6.877pt under it
    table_end = R.Y_ITEMS_ROWS + 15 * R.ROW_H
    assert any(abs(op['y0'] - table_end) < 0.01 for op in lines)
    # summary box closed by the navy total bar on the left half
    total = [op for op in _ops(sheet, 'rect')
             if op['fill'] == R.NAVY and abs(op['x'] - (R.SUM_LEFT + 0.375)) < 0.01]
    assert total
    assert abs(total[0]['y'] - (table_end + R.SUMMARY_GAP + 0.375
                                + 3 * R.SUM_ROW_H)) < 0.01


@requires_harfbuzz
def test_row_height_and_zebra_tint_are_the_reference_ones():
    sheet = R.layout_document(_document())[0]
    zebra = [op for op in _ops(sheet, 'rect') if op['fill'] == R.BAR
             and abs(op['x'] - R.LEFT) < 0.1 and abs(op['h'] - R.ROW_H) < 0.1]
    assert len(zebra) == 3, 'every second row of seven is tinted'
    tops = sorted(op['y'] for op in zebra)
    assert abs(tops[1] - tops[0] - 2 * R.ROW_H) < 0.1


@requires_harfbuzz
def test_sheet_text_holds_the_urdu_furniture_and_every_figure():
    op_text = ' '.join(op['text'] for op in R.layout_document(_document())[0].ops
                       if op['op'] == 'text')
    for label in ('بل نمبر', 'تاریخ', 'وقت', 'فضل بلڈنگ میٹیریلز',
                  'ٹرانزیکشن کی تفصیلات', 'کل فروخت', 'پچھلا بقایا',
                  'ادا شدہ رقم', 'کل بقایا رقم', 'مجاز دستخط'):
        assert label in op_text, label
    # the bill's own figures, copied verbatim
    for value in ('SB-SL-1008', '614,936', '31,700', '2,000', '1,964', '224'):
        assert value in op_text, value
    # English furniture must not survive on an Urdu sheet
    for english in ('INVOICE', 'Bill To', 'Transaction Details', 'Items',
                    'Total Sales'):
        assert english not in op_text, english


def test_panel_model_keeps_the_two_card_fields_in_reading_order():
    doc = _document()
    assert [label for label, _value in doc['fields']] == [
        'بل برائے', 'ڈیلیوری پرسن', 'کوڈ', 'اکاؤنٹ']
    values = [value for _label, value in doc['fields']]
    assert values[0] == 'محمد شفیق'
    assert values[1] == 'عدنان احمد'
    assert values[2] == 'FBMCL-00002'
    assert values[3] == 'Drawer Cash'


# ---------------------------------------------------------------------------
# QR block
# ---------------------------------------------------------------------------

def test_qr_block_is_drawn_from_the_bill_and_captioned():
    doc = _document()
    assert doc['qr_text'], 'a bill always carries its QR payload'
    sheet = R.layout_document(doc)[0]
    modules = [op for op in _ops(sheet, 'rect')
               if op['fill'] == R.NAVY and op['x'] >= R.QR_X - 0.1
               and op['x'] < R.QR_X + R.QR_SIZE]
    assert len(modules) > 40, 'the QR matrix was not drawn'
    assert all(op['y'] >= R.QR_Y - 0.1 for op in modules)
    assert any(op['op'] == 'text' and op['text'] == doc['qr_caption']
               for op in sheet.ops)


def test_no_qr_is_drawn_without_a_payload():
    doc = _document(qr_text='', qr_caption='')
    sheet = R.layout_document(doc)[0]
    modules = [op for op in _ops(sheet, 'rect')
               if op['fill'] == R.NAVY and R.QR_X <= op['x'] < R.QR_X + R.QR_SIZE
               and op['y'] < R.QR_Y + R.QR_SIZE]
    assert not modules


# ---------------------------------------------------------------------------
# Numbers and dates agree with the bill
# ---------------------------------------------------------------------------

def test_summary_figures_come_straight_from_the_bill():
    doc = _document()
    values = {row['label']: row['value'] for row in doc['summary']}
    assert values['کل فروخت'] == '614,936'
    assert values['پچھلا بقایا'] == '31,700'
    assert values['ادا شدہ رقم'] == '0'
    # outstanding = previous + this bill - paid
    assert values['کل بقایا رقم'] == '646,636'
    assert doc['summary'][-1]['total'] is True


def test_summary_without_a_previous_balance_has_no_carry_forward_row():
    doc = _document(previous_balance=0, bill=_bill(amount=5000))
    labels = [row['label'] for row in doc['summary']]
    assert 'پچھلا بقایا' not in labels
    assert labels[-1] == 'کل بقایا رقم'


def test_urdu_date_and_clock_read_like_the_sheet():
    assert R._urdu_date(datetime(2026, 11, 7)) == '07 نومبر 2026'
    assert R._urdu_time(datetime(2026, 11, 7, 12, 20)) == '12:20 دوپہر'
    assert R._urdu_time(datetime(2026, 11, 7, 9, 5)) == '9:05 صبح'


def test_phone_line_is_split_into_two_numbers():
    assert R.split_phones('+92331-0000993 | +92340-3872722') == [
        '+92 331 0000993', '+92 340 3872722']


def test_item_amounts_are_quantity_times_rate_as_posted():
    rows = bill_urdu._item_rows(ITEMS)
    assert rows[0] == {'index': 1, 'name': 'ڈی جی', 'qty': '1',
                       'rate': '2,000', 'amount': '2,000'}
    assert rows[2]['amount'] == '439,936'


def test_receipt_bill_shows_received_and_remaining_balance():
    doc = bill_urdu.build_document(bill_type='Payment', bill=_bill(),
                                   items=[], previous_balance=1000,
                                   settings=_settings())
    labels = [row['label'] for row in doc['summary']]
    assert labels[0] == 'وصول شدہ رقم'
    assert labels[-1] == 'بقایا رقم'


# ---------------------------------------------------------------------------
# Terms & conditions
# ---------------------------------------------------------------------------

def test_terms_are_two_numbered_columns():
    doc = _document(terms=[SimpleNamespace(title='', content_ur=text)
                           for text in TERMS])
    sheet = R.layout_document(doc)[0]
    numbers = [op for op in sheet.ops
               if op['op'] == 'text' and re.fullmatch(r'\d+\.', op['text'])]
    assert [op['text'] for op in numbers] == [f'{n}.' for n in range(1, 8)]
    # right column starts at x = 399.7, left column at 204.1
    assert {round(op['x'], 1) for op in numbers} == {204.1, 399.7}
    right = [op for op in numbers if op['x'] > 300]
    left = [op for op in numbers if op['x'] < 300]
    assert (len(right), len(left)) == (4, 3)


def test_a_terms_clause_is_numbered_once_however_it_wraps():
    doc = _document(terms=[SimpleNamespace(title='', content_ur=TERMS[0])])
    sheet = R.layout_document(doc)[0]
    numbers = [op for op in sheet.ops
               if op['op'] == 'text' and re.fullmatch(r'\d+\.', op['text'])]
    assert len(numbers) == 1, 'a wrapped clause keeps one number'


def test_terms_heading_sits_under_the_summary_rule():
    doc = _document(terms=[SimpleNamespace(title='', content_ur=t)
                           for t in TERMS])
    sheet = R.layout_document(doc)[0]
    heading = [op for op in sheet.ops
               if op['op'] == 'text' and op['text'] == 'شرائط و ضوابط']
    assert heading, 'terms heading missing'
    assert heading[0]['color'] == R.NAVY


# ---------------------------------------------------------------------------
# Print page
# ---------------------------------------------------------------------------

def test_print_page_is_the_same_rtl_sheet():
    html = bill_urdu.render_html(_document())
    assert 'dir="rtl"' in html
    assert '<div class="pg"' in html
    # the default print page is the single approved A5 sheet
    assert f'width:{R._fmt(R.PAGE_W)}pt' in html
    assert R.COPY_ORIGINAL not in html and R.COPY_CARBON not in html


def test_print_page_can_still_be_the_carbon_copy():
    html = bill_urdu.render_html(_document(), copies=2)
    assert f'width:{R._fmt(R.A4_W)}pt' in html
    assert R.COPY_ORIGINAL in html and R.COPY_CARBON in html


def test_print_page_escapes_user_text():
    doc = _document(bill=_bill(note='<script>alert(1)</script>'))
    html = bill_urdu.render_html(doc)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html


def test_english_text_in_an_urdu_sheet_keeps_its_word_order():
    words = R.rtl_words('FBMCL-00002 Drawer Cash')
    assert words == [('Cash', False), ('Drawer', False), ('FBMCL-00002', False)]


def test_mixed_urdu_line_puts_its_first_word_at_the_right():
    words = R.rtl_words('07 نومبر 2026')
    assert words[-1] == ('07', False)      # first word of the line, hard right
    assert words[0] == ('2026', False)     # last word of the line, hard left
    assert [w for w, _u in words] == ['2026', 'نومبر', '07']


# ---------------------------------------------------------------------------
# Real bill through the real route
# ---------------------------------------------------------------------------

def _login(client):
    resp = client.post('/login', data={'username': 'Admin',
                                       'password': 'Admin@fbm12345'},
                       follow_redirects=False)
    assert resp.status_code in (302, 303)


@pytest.fixture()
def sale(app, client):
    """A real DirectSale posted through the real route, in Urdu."""
    from app import db
    from models import Client, DirectSale, Entry, Material, Settings
    from app.services.void_rebuild import _rebuild_material_totals

    _login(client)

    with app.app_context():
        settings = Settings.query.first() or Settings()
        settings.allow_global_negative_stock = True
        db.session.add(settings)
        db.session.commit()

    names = ['ڈی جی', 'سیمنٹ ٹھیکہ']
    for name in names:
        client.post('/add_material',
                    data={'material_name': name, 'material_unit': 'Bags'},
                    follow_redirects=True)
    client.post('/add_client', data={'name': 'Muhammad Shafiq',
                                     'code': 'FBMCL-00009',
                                     'category': 'General',
                                     'opening_balance': '0'},
                follow_redirects=True)
    client.post('/accounts/accounts/add', data={
        'name': 'URDU CASH COUNTER', 'class_category': 'Assets',
        'class_subcategory': 'Cash', 'class_account_type': 'Main Cash',
        'account_status': 'active', 'opening_amount': '0',
        'opening_position': 'debit',
        'opening_effective_date': '2026-01-01'}, follow_redirects=True)

    with app.app_context():
        from models import Account
        mats = [(m.id, m.name) for m in Material.query.filter(
            Material.name.in_(names)).all()]
        client_row = Client.query.filter_by(code='FBMCL-00009').first()
        account = Account.query.filter(Account.name.like('%URDU CASH%')).first()
        client_row.name_ur = 'محمد شفیق'
        db.session.add(Entry(date='2026-06-01', time='09:00:00', type='IN',
                             material='ڈی جی', qty=5000, bill_no='MB NO.9001',
                             client_category='Stock In'))
        _rebuild_material_totals()
        db.session.commit()
        payload = {'client_code': client_row.code,
                   'client_name': client_row.name,
                   'account': account.id,
                   'materials': {name: mid for mid, name in mats}}

    rows = [('ڈی جی', 1, 2000), ('سیمنٹ ٹھیکہ', 50, 1290)]
    resp = client.post('/add_direct_sale', data={
        'client_code': payload['client_code'],
        'client_name': payload['client_name'],
        'category': 'Credit Customer',
        'manual_bill_no': 'SB-SL-2001',
        'driver_name': 'عدنان احمد',
        'paid_amount': '0',
        'note': NOTE,
        'product_name[]': [row[0] for row in rows],
        'material_id[]': [str(payload['materials'][row[0]]) for row in rows],
        'qty[]': [str(row[1]) for row in rows],
        'unit_rate[]': [str(row[2]) for row in rows],
        'payment_method': 'Cash',
        'payment_account_id': str(payload['account']),
    }, follow_redirects=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Direct sale added successfully' in body, 'the sale was rejected'

    with app.app_context():
        row = DirectSale.query.order_by(DirectSale.id.desc()).first()
        assert row is not None, 'the sale was not posted'
        return {'id': row.id, 'bill_no': row.manual_bill_no}


def test_route_serves_the_single_a5_pdf_by_default(client, sale):
    url = (f"/download_invoice/{sale['bill_no']}?src=direct_sale"
           f"&src_id={sale['id']}&lang=ur&tc=0")
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.data.startswith(b'%PDF-')
    assert '-UR.pdf' in resp.headers['Content-Disposition']
    # the sheet embeds its own two faces
    assert b'Jameel' in resp.data
    reader = PdfReader(io.BytesIO(resp.data))
    assert len(reader.pages) == 1
    box = reader.pages[0].mediabox
    assert round(float(box.width), 1) == round(R.PAGE_W, 1)
    assert round(float(box.height), 1) == round(R.PAGE_H, 1)


def test_route_serves_the_carbon_copy_when_asked(client, sale):
    url = (f"/download_invoice/{sale['bill_no']}?src=direct_sale"
           f"&src_id={sale['id']}&lang=ur&tc=0&copies=2")
    resp = client.get(url)
    assert resp.status_code == 200
    reader = PdfReader(io.BytesIO(resp.data))
    assert len(reader.pages) == 1
    box = reader.pages[0].mediabox
    assert round(float(box.width), 1) == round(R.A4_W, 1)
    assert round(float(box.height), 1) == round(R.A4_H, 1)


def test_route_print_is_the_same_urdu_sheet(client, sale):
    url = (f"/download_invoice/{sale['bill_no']}?src=direct_sale"
           f"&src_id={sale['id']}&lang=ur&action=print&tc=0")
    resp = client.get(url)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '-UR.html' in resp.headers['Content-Disposition']
    assert 'dir="rtl"' in html
    assert '<div class="pg"' in html
    assert R.COPY_ORIGINAL not in html and R.COPY_CARBON not in html
    # English receipt furniture must never leak into the Urdu sheet
    for english in ('rcpt-header', 'rcpt-table', 'English PDF'):
        assert english not in html, english
    assert 'ٹرانزیکشن کی تفصیلات' in html


def test_english_download_is_untouched(client, sale):
    url = (f"/download_invoice/{sale['bill_no']}?src=direct_sale"
           f"&src_id={sale['id']}")
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.data.startswith(b'%PDF-')
    assert '-EN.pdf' in resp.headers['Content-Disposition']
    text = _pdf_text(resp.data).upper()
    assert 'INVOICE' in text or 'RECEIPT' in text
    assert 'بل نمبر' not in text


# ---------------------------------------------------------------------------
# The "New Receipt.pdf" reference: data, fonts and mixed-script figures
# ---------------------------------------------------------------------------

@requires_harfbuzz
def test_reference_geometry_is_pinned():
    """The anchors measured off New Receipt.pdf, not the older Urdu sheet."""
    assert round(R.PAGE_W, 4) == 419.5276
    assert round(R.PAGE_H, 4) == 595.2756
    assert round(R.LEFT, 4) == 19.8425
    assert round(R.RIGHT, 4) == 399.6851
    assert round(R.Y_RULE_1, 4) == 75.0315
    assert round(R.Y_META, 4) == 94.9596
    assert round(R.META_BASELINE, 3) == 105.161
    assert round(R.Y_PANEL, 4) == 116.8289
    assert round(R.PANEL_H, 4) == 43.3293
    assert round(R.Y_ITEMS_HEAD, 4) == 183.3102
    assert round(R.ROW_H, 4) == 15.3071
    assert R.ROWS_FIRST_PAGE == 15
    assert round(R.SUM_LEFT, 4) == 17.5925
    assert round(R.SUM_RIGHT, 4) == 185.5866
    assert round(R.SIG_LINE_X0, 4) == 257.9528
    assert round(R.SIG_LINE_X1, 4) == 399.6851
    assert round(R.TERMS_STEP, 3) == 11.670


def test_signature_rule_aligns_with_right_margin_and_centers_label():
    """The signature line must end flush with RIGHT (not bleed to PAGE_W)."""
    sheet = R.layout_document(_document())[0]
    sig_lines = [op for op in _ops(sheet, 'line')
                 if abs(op['y0'] - op['y1']) < 0.01
                 and abs(op['x0'] - R.SIG_LINE_X0) < 0.01]
    assert len(sig_lines) == 1
    sig_line = sig_lines[0]
    assert abs(sig_line['x1'] - R.RIGHT) < 0.01
    assert sig_line['x1'] < R.PAGE_W - 19.0
    assert R.SUM_RIGHT < sig_line['x0'] < sig_line['x1']

    sig_labels = [op for op in _ops(sheet, 'text')
                  if op['text'] == 'مجاز دستخط']
    assert len(sig_labels) == 1
    sig_label = sig_labels[0]
    midpoint = (sig_line['x0'] + sig_line['x1']) / 2.0
    assert sig_label['align'] == 'center'
    assert abs(sig_label['x'] - midpoint) < 0.01

    # Carbon-copy A4 leaf: neither copy's signature line may touch the cut seam
    # or bleed past its copy's right content margin.
    a4 = R.compose_document(_document(), copies=2)[0]
    seam = R.A4_W / 2.0
    offset = (R.A4_W - 2 * R.PAGE_W) / 2.0
    copy_sig_lines = [op for op in _ops(a4, 'line')
                      if abs(op['y0'] - op['y1']) < 0.01
                      and abs((op['x1'] - op['x0']) - R.SIG_LINE_W) < 0.05]
    assert len(copy_sig_lines) == 2
    left_copy_line, right_copy_line = sorted(copy_sig_lines, key=lambda o: o['x0'])
    assert abs(left_copy_line['x1'] - (offset + R.RIGHT)) < 0.01
    assert left_copy_line['x1'] < seam - 19.0
    assert abs(right_copy_line['x1'] - (offset + R.PAGE_W + R.RIGHT)) < 0.01
    assert right_copy_line['x1'] < R.A4_W - 19.0

    # Single-row Payment receipt with terms keeps signing clearance above the
    # signature rule and places the terms rule cleanly below the label.
    pay_doc = bill_urdu.build_document(
        bill_type='Payment', bill=_bill(amount=25000), items=[],
        previous_balance=0, settings=_settings(),
        terms=[SimpleNamespace(title='', content_ur=t) for t in TERMS[:4]],
    )
    pay_sheet = R.layout_document(pay_doc)[0]
    pay_sig = next(op for op in _ops(pay_sheet, 'line')
                   if abs(op['x0'] - R.SIG_LINE_X0) < 0.01)
    pay_label = next(op for op in _ops(pay_sheet, 'text')
                     if op['text'] == 'مجاز دستخط')
    terms_heading = next(op for op in _ops(pay_sheet, 'text')
                         if op['text'] == 'شرائط و ضوابط')
    assert pay_sig['y0'] >= (R.Y_PANEL + R.PANEL_H) + R.SIG_MIN_CLEARANCE - 0.01
    assert terms_heading['y'] > pay_label['y'] + 10.0


def test_party_panel_carries_the_contact_placeholder_and_code():
    doc = _document()
    assert doc['party_phone'] == '____'
    ops = R.layout_document(doc)[0].ops
    text = [op['text'] for op in ops if op['op'] == 'text']
    assert 'حوالہ:' in text
    assert '____' in text
    assert 'کوڈ:' in text
    assert 'FBMCL-00002' in text
    assert 'ڈیلیوری پرسن:' in text
    assert 'اکاؤنٹ:' in text


def test_party_phone_comes_from_the_client_when_present():
    client = SimpleNamespace(code='FBMCL-00002', phone='0300-1234567')
    doc = _document(client=client)
    assert doc['party_phone'] == '0300 1234567'
    text = [op['text'] for op in R.layout_document(doc)[0].ops
            if op['op'] == 'text']
    assert '0300 1234567' in text


def test_pdf_embeds_the_reference_faces():
    data = bill_urdu.render_pdf(_document())
    assert b'Jameel' in data
    assert b'DejaVu' in data


def test_mixed_urdu_figures_keep_their_left_to_right_order():
    assert R._urdu_runs('ٹائل 12×18')[0] == ('12×18', 'ltr')
    assert R._urdu_runs('07 نومبر 2026')[0] == ('2026', 'ltr')
    assert R._urdu_runs('07 نومبر 2026')[-1] == ('07', 'ltr')
    assert R._urdu_runs('12 ملی میٹر سریا')[-1] == ('12', 'ltr')


@requires_harfbuzz
def test_print_page_wraps_embedded_figures_for_browser_bidi():
    items = [{'name_ur': 'ٹائل 12×18', 'qty': 40, 'price_at_time': 1950}]
    html = bill_urdu.render_html(_document(items=items))
    assert '<span dir="ltr">12×18</span>' in html


def test_long_bills_continue_without_the_first_page_meta_strip():
    items = [{'name_ur': 'اینٹ', 'qty': 1, 'price_at_time': 10}] * 40
    sheets = R.layout_document(_document(items=items))
    assert len(sheets) >= 2
    first_meta = [op for op in sheets[0].ops if op['op'] == 'rect'
                  and abs(op['y'] - R.Y_META) < 0.01]
    assert first_meta
    for sheet in sheets[1:]:
        assert not [op for op in sheet.ops if op['op'] == 'rect'
                    and abs(op['y'] - R.Y_META) < 0.01]
        assert any(op['op'] == 'rect' and op['fill'] == R.NAVY
                   and abs(op['y'] - R.CONT_HEAD_TOP) < 0.01 for op in sheet.ops)


def test_reference_bill_data_flows_through_the_real_adapter():
    doc = _reference_document()
    assert len(doc['items']) == 15
    assert sum(float(row['amount'].replace(',', '')) for row in doc['items']) == 1105636
    assert {row['label']: row['value'] for row in doc['summary']} == {
        'کل فروخت': '1,105,636',
        'پچھلا بقایا': '31,700',
        'ادا شدہ رقم': '614,936',
        'کل بقایا رقم': '522,400',
    }
    # Without a request there is no reachable host for a verify link, so the
    # QR falls back to the in-line bill summary - still the real bill, never a
    # fake "SAMPLE QR".
    assert doc['qr_caption'] == 'SB-SL-1008', 'the real bill, not a fake SAMPLE QR'
    assert 'SB-SL-1008' in doc['qr_text']
    assert 'FBMCL-00002' in doc['qr_text']
    assert '1,105,636' in doc['qr_text'], 'the bill total must be in the code'
    assert '522,400' in doc['qr_text'], 'the closing balance must be in the code'
    assert 'SAMPLE' not in doc['qr_text']

    sheets = R.layout_document(doc)
    assert len(sheets) == 1
    assert any(op['op'] == 'line' and abs(op['y0'] - 428.7905) < 0.001
               for op in sheets[0].ops)
    assert len(bill_urdu.render_pdf(doc)) > 10000


@requires_harfbuzz
def test_reference_terms_use_equal_columns_and_the_red_fourth_number():
    sheet = R.layout_document(_reference_document())[0]
    assert abs(R.TERMS_RIGHT_WIDTH - R.TERMS_LEFT_WIDTH) < 0.02
    assert round(R.TERMS_COLUMN_W, 2) == 184.25
    # the numbered clause text (the "N." markers are drawn a touch smaller)
    clauses = [op for op in sheet.ops if op['op'] == 'text'
               and R.TERMS_SIZE * 0.85 <= op['size'] <= R.TERMS_SIZE
               and op['y'] > 500.0
               and not re.fullmatch(r'\d+\.', op['text'])]
    assert len(clauses) == 7, 'each reference clause sets on one line'
    # The type is the largest size at which no clause wraps, on the approved
    # leading, all on one A5 sheet.
    assert [round(op['y'], 1) for op in clauses[:4]] == [
        521.2, 532.4, 543.6, 554.7]
    numbers = {op['text']: op for op in sheet.ops if op['op'] == 'text'
               and re.fullmatch(r'\d+\.', op['text'])}
    assert numbers['4.']['color'] == R.TERMS_RED
    assert numbers['4.']['font'] == R.SANS_BOLD
    assert all(numbers[f'{n}.']['color'] == R.NAVY for n in (1, 2, 3, 5, 6, 7))


def test_item_header_has_no_white_column_dividers():
    sheet = R.layout_document(_reference_document())[0]
    assert not [op for op in sheet.ops if op['op'] == 'line'
                and op['color'] == R.WHITE
                and R.Y_ITEMS_HEAD <= op['y0'] <= R.Y_ITEMS_ROWS]


def test_long_bill_keeps_every_row_and_finishes_the_footer_on_the_page():
    items = [{'name_ur': 'اینٹ', 'qty': 1, 'price_at_time': 10}] * 40
    doc = _document(items=items,
                    terms=[SimpleNamespace(title='', content_ur=t) for t in TERMS])
    sheets = R.layout_document(doc)
    indices = [int(op['text']) for sheet in sheets for op in sheet.ops
               if op['op'] == 'text' and op['font'] == R.SANS
               and op['text'].isdigit()
               and abs(op['x'] - R._item_columns()[-1]['x']) < 0.01]
    assert indices == list(range(1, 41))
    assert any(op['op'] == 'text' and op['text'] == '7.' for op in sheets[-1].ops)
    for sheet in sheets:
        assert all(op['y'] <= R.PAGE_BOTTOM for op in sheet.ops
                   if op['op'] == 'text')


def test_html_title_is_escaped():
    html = R.render_html(_document(), title='</title><script>alert(1)</script>')
    assert '<script>alert(1)</script>' not in html
    assert '&lt;/title&gt;&lt;script&gt;' in html


def test_urdu_brackets_are_mirrored_like_the_browser():
    runs = R._urdu_runs('رقم (روپے)')
    assert runs[0] == ('(', 'ltr')
    assert runs[2] == (')', 'ltr')
    assert ''.join(chunk for chunk, _direction in runs) == '(روپے) رقم'


def test_route_confirmation_defaults_to_the_single_a5_sheet(client, sale):
    url = (f"/download_invoice/{sale['bill_no']}?src=direct_sale"
           f"&src_id={sale['id']}&lang=ur")
    resp = client.get(url)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert re.search(r'id="copies1"[^>]*checked', html)
    assert not re.search(r'id="copies2"[^>]*checked', html)


def test_reference_proprietor_fallback_and_override():
    assert _document(settings=_settings(proprietor_name_ur=''))['proprietor'] == 'عدنان احمد'
    assert _document(settings=_settings(proprietor_name_ur='علی'))['proprietor'] == 'علی'
    assert _document(settings=_settings(proprietor_name_ur='M Ahmed'))['proprietor'] == 'محمد احمد'
    assert _document(settings=_settings(company_name='Other', proprietor_name_ur=''))['proprietor'] == ''


def test_kohat_uses_correct_urdu_spelling_on_receipts():
    from app.services.urdu_names import urdu_name

    for spelling in ('kohat', 'Kohat', 'KOHAT'):
        assert urdu_name(spelling) == 'کوہاٹ'
    assert urdu_name('Kohat, Pakistan') == 'کوہاٹ, پاکستان'
    assert urdu_name('Kohat', preferred='کوہاٹ شہر') == 'کوہاٹ شہر'

    doc = _document(settings=_settings(company_address='KOHAT, PAKISTAN'))
    assert doc['company_address'] == 'کوہاٹ, پاکستان'
    assert 'کوہاٹ' in bill_urdu.render_html(doc)


def test_standalone_m_is_muhammad_and_ahmed_uses_proper_urdu_spelling():
    from app.services.urdu_names import urdu_name

    # Standalone m / M (with or without abbreviation dot) stands for Muhammad.
    assert urdu_name('m') == 'محمد'
    assert urdu_name('M') == 'محمد'
    assert urdu_name('m.') == 'محمد'
    assert urdu_name('M.') == 'محمد'

    # Ahmed / Ahmad always uses ح (احمد), never گول ہ (اہمد).
    for spelling in ('ahmed', 'Ahmed', 'AHMED', 'ahmad', 'Ahmad', 'AHMAD'):
        assert urdu_name(spelling) == 'احمد', spelling

    # Real bill names combining standalone M and Ahmed / other names.
    for full in ('m ahmed', 'M Ahmed', 'M AHMED', 'm. ahmed', 'M. Ahmed',
                 'M. AHMED', 'm.ahmed', 'M.Ahmed', 'M.AHMED'):
        assert urdu_name(full) == 'محمد احمد', full
    assert urdu_name('M Shafiq') == 'محمد شفیق'
    assert urdu_name('M.Shafiq') == 'محمد شفیق'
    assert urdu_name('Ch. M. Ahmed Sb') == 'چوہدری. محمد احمد صاحب'
    assert urdu_name('Adnan Ahmed') == 'عدنان احمد'

    # Numeric units/grades and two-letter dotted/slashed abbreviations are not Muhammad.
    for non_name in ('Pipe 3m', '3M Tape', '1.5m', 'Bolt M10', 'M-10', 'M.10',
                     'M.S. Pipe', '10 A.M.'):
        assert 'محمد' not in urdu_name(non_name), non_name
    assert urdu_name('M/S Ahmed Traders') == 'م/س احمد ٹریڈرز'

    # Requested name and building-material vocabulary.
    for spelling in ('haris', 'Haris', 'HARIS'):
        assert urdu_name(spelling) == 'حارث', spelling
    assert urdu_name('M Haris') == 'محمد حارث'
    for unit in ('mm', 'MM', 'Mm'):
        assert urdu_name(unit) == 'ملی میٹر', unit
    assert urdu_name('12 mm') == '12 ملی میٹر'
    assert urdu_name('12mm') == '12 ملی میٹر'
    assert urdu_name('12MM') == '12 ملی میٹر'
    for kg_unit in ('kg', 'KG', 'Kg', 'kgs', 'KGS', 'Kgs', 'kilogram', 'Kilograms'):
        assert urdu_name(kg_unit) == 'کلو گرام', kg_unit
    assert urdu_name('10 kg') == '10 کلو گرام'
    assert urdu_name('10kg') == '10 کلو گرام'
    assert urdu_name('10KG') == '10 کلو گرام'
    assert urdu_name('2.5kg') == '2.5 کلو گرام'
    from app.services.bilingual import LABELS
    for label_key in ('kg', 'Kg', 'KG', 'kgs', 'Kgs', 'KGS'):
        assert LABELS[label_key] == 'کلو گرام', label_key
    for frame in ('chaugath', 'Chaugath', 'CHAUGATH'):
        assert urdu_name(frame) == 'چو گھاٹ', frame
    for b in ('brick', 'Brick', 'BRICK'):
        assert urdu_name(b) == 'اینٹ', b
    for s in ('steel', 'Steel', 'STEEL'):
        assert urdu_name(s) == 'سٹیل', s
    assert urdu_name('Steel 12mm') == 'سٹیل 12 ملی میٹر'
    assert urdu_name('12mm Steel') == '12 ملی میٹر سٹیل'
    assert urdu_name('Ahmed Steel') == 'احمد سٹیل'
    assert urdu_name('Steel', preferred='لوہا') == 'لوہا'

    steel_doc = _document(
        bill=_bill(client_name='Ahmed Steel'),
        client_name_ur='',
        items=[{'name': 'Steel 12mm', 'qty': 10, 'price_at_time': 220}],
    )
    assert steel_doc['party_name'] == 'احمد سٹیل'
    assert steel_doc['items'][0]['name'] == 'سٹیل 12 ملی میٹر'
    steel_html = bill_urdu.render_html(steel_doc)
    assert 'احمد سٹیل' in steel_html
    assert 'سٹیل' in steel_html
    assert 'ستیل' not in steel_html


def test_real_bill_renders_m_ahmed_as_muhammad_ahmed_on_sheet_and_verify(client, app):
    """A posted bill for 'M Ahmed' prints محمد احمد and material words in Urdu."""
    from app import db
    from app.services import bill_verify
    from models import Account, DirectSale, Entry, Material, Settings
    from app.services.void_rebuild import _rebuild_material_totals

    _login(client)
    with app.app_context():
        settings = Settings.query.first() or Settings()
        settings.allow_global_negative_stock = True
        db.session.add(settings)
        db.session.commit()

    mat_names = ['12mm Chaugath', 'Brick']
    for mat_name in mat_names:
        client.post('/add_material',
                    data={'material_name': mat_name, 'material_unit': 'Pcs'},
                    follow_redirects=True)
    client.post('/add_client', data={'name': 'M Ahmed',
                                     'code': 'FBMCL-00077',
                                     'category': 'General',
                                     'opening_balance': '0'},
                follow_redirects=True)
    client.post('/accounts/accounts/add', data={
        'name': 'M AHMED CASH', 'class_category': 'Assets',
        'class_subcategory': 'Cash', 'class_account_type': 'Main Cash',
        'account_status': 'active', 'opening_amount': '0',
        'opening_position': 'debit',
        'opening_effective_date': '2026-01-01'}, follow_redirects=True)

    with app.app_context():
        mats = {m.name: m.id for m in Material.query.filter(Material.name.in_(mat_names)).all()}
        account = Account.query.filter(Account.name.like('%M AHMED CASH%')).first()
        for mat_name in mat_names:
            db.session.add(Entry(date='2026-06-01', time='09:00:00', type='IN',
                                 material=mat_name, qty=100, bill_no='MB NO.9077',
                                 client_category='Stock In'))
        _rebuild_material_totals()
        db.session.commit()
        acc_id = account.id

    resp = client.post('/add_direct_sale', data={
        'client_code': 'FBMCL-00077',
        'client_name': 'M Ahmed',
        'category': 'Credit Customer',
        'manual_bill_no': 'SB-SL-2077',
        'driver_name': 'M. Haris',
        'paid_amount': '0',
        'product_name[]': mat_names,
        'material_id[]': [str(mats[n]) for n in mat_names],
        'qty[]': ['2', '500'],
        'unit_rate[]': ['1500', '20'],
        'payment_method': 'Cash',
        'payment_account_id': str(acc_id),
    }, follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        posted = DirectSale.query.order_by(DirectSale.id.desc()).first()
        assert posted is not None
        sale_id, bill_no = posted.id, posted.manual_bill_no
        token = bill_verify.make_token('DirectSale', sale_id, bill_no)

    html = client.get(
        f'/download_invoice/{bill_no}?src=direct_sale&src_id={sale_id}'
        '&lang=ur&action=print&tc=0'
    ).get_data(as_text=True)
    html_text = re.sub(r'<[^>]+>', '', html)
    for expected in ('محمد احمد', 'محمد حارث', 'ملی میٹر چو گھاٹ', 'اینٹ'):
        assert expected in html_text, expected
    assert 'اہمد' not in html

    # The scanned page draws the printed sheet, so a Latin figure inside an
    # Urdu item sits in its own LTR isolate: the text is the same, the markup
    # keeps the browser from reordering it.
    verify_html = client.get(f'/verify/bill/{token}').get_data(as_text=True)
    verify_text = re.sub(r'<[^>]+>', '', verify_html)
    for expected in ('محمد احمد', 'محمد حارث', '12 ملی میٹر چو گھاٹ', 'اینٹ'):
        assert expected in verify_text, expected
    assert 'اہمد' not in verify_text
    # every material line and its money figures are on the scanned page too
    for figure in ('500', '20', '1,500', '3,000', '10,000', '13,000'):
        assert figure in verify_text, figure


def test_confirmation_multiple_term_ids_are_all_used(client, sale, app):
    from app import db
    from models import TermsAndCondition
    with app.app_context():
        rows = [TermsAndCondition(title='Test A', content_ur='پہلی منفرد شرط', is_active=True),
                TermsAndCondition(title='Test B', content_ur='دوسری منفرد شرط', is_active=True)]
        db.session.add_all(rows)
        db.session.commit()
        ids = [row.id for row in rows]
    url = (f"/download_invoice/{sale['bill_no']}?src=direct_sale"
           f"&src_id={sale['id']}&lang=ur&action=print&tc=1"
           f"&tc_ids={ids[0]}&tc_ids={ids[1]}")
    html = client.get(url).get_data(as_text=True)
    assert 'پہلی منفرد شرط' in html and 'دوسری منفرد شرط' in html


# ---------------------------------------------------------------------------
# Optional sheet sections (print dialog switches)
# ---------------------------------------------------------------------------

def test_all_sections_are_on_by_default():
    doc = _document()
    assert doc['show_qr'] is True
    assert doc['show_note'] is True
    assert doc['show_signature'] is True
    labels = [row['label'] for row in doc['summary']]
    assert 'پچھلا بقایا' in labels
    left_panel = [op for op in R.layout_document(doc)[0].ops
                  if op['op'] == 'text'
                  and str(op['text']).startswith('ڈیلیوری')]
    assert left_panel, 'the delivery line is part of the approved sheet'


def test_qr_and_shop_note_can_be_hidden():
    doc = _document(sections={'qr': False, 'note': False})
    assert doc['show_qr'] is False and doc['show_note'] is False
    sheet = R.layout_document(doc)[0]
    modules = [op for op in sheet.ops if op['op'] == 'rect'
               and op['fill'] == R.NAVY
               and R.QR_X - 0.1 <= op['x'] < R.QR_X + R.QR_SIZE
               and R.QR_Y - 0.1 <= op['y'] < R.QR_Y + R.QR_SIZE]
    assert not modules, 'no QR modules when the section is switched off'
    assert not [op for op in sheet.ops
                if op['op'] == 'text' and op['text'] == NOTE]
    # The rest of the header - shop name, address, phones - is untouched.
    assert [op for op in sheet.ops
            if op['op'] == 'text' and op['text'] == doc['company_address']]


def test_previous_balance_can_be_hidden_without_recomputing_the_balance():
    full = _document()
    without = _document(sections={'prev': False})
    assert 'پچھلا بقایا' in [row['label'] for row in full['summary']]
    assert 'پچھلا بقایا' not in [row['label'] for row in without['summary']]
    # One row is hidden; the closing balance itself is still the real one.
    assert full['summary'][-1]['value'] == without['summary'][-1]['value']
    assert without['summary'][-1].get('total') is True


def test_signature_and_delivery_can_be_hidden():
    doc = _document(sections={'sig': False, 'delivery': False})
    assert doc['show_signature'] is False
    sheet = R.layout_document(doc)[0]
    assert not [op for op in sheet.ops
                if op['op'] == 'text' and op['text'] == 'مجاز دستخط']
    assert not [op for op in sheet.ops
                if op['op'] == 'line' and op['color'] == R.NAVY
                and abs(op['x0'] - R.SIG_LINE_X0) < 0.01
                and abs(op['x1'] - R.SIG_LINE_X1) < 0.01]
    assert not [op for op in sheet.ops
                if op['op'] == 'text'
                and str(op['text']).startswith('ڈیلیوری')]


def test_section_flags_default_on_and_the_last_value_wins():
    from werkzeug.datastructures import MultiDict
    from app.blueprints.sales._bills_download_invoice import _urdu_section_flags

    from app.services.urdu_receipt import SHEET_OPTIONS

    defaults = _urdu_section_flags(MultiDict())
    assert set(defaults) == set(SHEET_OPTIONS)
    assert all(defaults.values()), 'a link with no flags prints the full sheet'

    # The confirm page posts a hidden 0 then the checked 1; the last wins.
    assert _urdu_section_flags(MultiDict([('qr', '0'), ('qr', '1')]))['qr'] is True
    assert _urdu_section_flags(MultiDict([('qr', '1'), ('qr', '0')]))['qr'] is False
    assert _urdu_section_flags(MultiDict([('note', '0')]))['note'] is False


def test_confirmation_page_offers_every_item_on_the_page_map(client, sale):
    from app.services.urdu_receipt import SHEET_OPTIONS

    url = (f"/download_invoice/{sale['bill_no']}?src=direct_sale"
           f"&src_id={sale['id']}&lang=ur")
    html = client.get(url).get_data(as_text=True)
    for key in SHEET_OPTIONS:
        assert f'name="{key}" value="1"' in html, key
        assert f'name="{key}" value="0"' in html, key


def test_route_section_switches_reach_the_urdu_sheet(client, sale):
    off = (f"/download_invoice/{sale['bill_no']}?src=direct_sale"
           f"&src_id={sale['id']}&lang=ur&action=print&tc=0"
           f"&qr=0&note=0&prev=0&sig=0&delivery=0")
    html = client.get(off).get_data(as_text=True)
    for marker in ('مجاز دستخط', 'ڈیلیوری'):
        assert marker not in html, marker

    on = (f"/download_invoice/{sale['bill_no']}?src=direct_sale"
          f"&src_id={sale['id']}&lang=ur&action=print&tc=0")
    html = client.get(on).get_data(as_text=True)
    assert 'مجاز دستخط' in html
    assert 'ڈیلیوری' in html


def _texts(doc):
    return [op['text'] for op in R.layout_document(doc)[0].ops
            if op['op'] == 'text']


def test_header_marks_can_be_hidden_individually():
    doc = _document(sections={'company': False, 'address': False,
                              'proprietor': False, 'phones': False})
    texts = _texts(doc)
    assert doc['company_name'] not in texts
    assert doc['company_address'] not in texts
    assert 'پروپرائٹر:' not in texts
    assert not any(str(t).startswith('+92') for t in texts)
    # the header rule still closes the block
    assert [op for op in R.layout_document(doc)[0].ops
            if op['op'] == 'rect' and op['y'] == R.Y_RULE_1]


def test_meta_strip_cells_can_be_hidden_individually():
    doc = _document(sections={'meta_time': False, 'meta_date': False,
                              'meta_type': False, 'meta_bill': False})
    texts = _texts(doc)
    for label in ('وقت:', 'تاریخ:', 'بل نمبر:'):
        assert label not in texts, label
    assert 'Sale' not in texts, 'the type label was not switched off'


def test_client_panel_marks_can_be_hidden_individually():
    doc = _document(sections={'party': False, 'ref': False, 'code': False,
                              'delivery': False, 'account': False,
                              'method': False})
    texts = _texts(doc)
    for label in ('بل برائے:', 'حوالہ:', 'کوڈ:', 'ڈیلیوری پرسن:',
                  'ڈیلیوری پرسنز:', 'اکاؤنٹ:', 'ادائیگی:'):
        assert label not in texts, label


def test_whole_items_table_can_be_hidden():
    doc = _document(sections={'items': False})
    assert doc['show_items'] is False
    texts = _texts(doc)
    assert 'ٹرانزیکشن کی تفصیلات' not in texts
    for header in ('نمبر', 'تفصیل / آئٹم', 'مقدار', 'ریٹ', 'رقم (روپے)'):
        assert header not in texts, header


def test_items_heading_and_prices_note_can_be_hidden():
    doc = _document(sections={'items_title': False, 'items_note': False})
    texts = _texts(doc)
    assert 'ٹرانزیکشن کی تفصیلات' not in texts
    assert R.SECTION_NOTE not in texts
    # the table itself is still there
    assert 'مقدار' in texts and 'ریٹ' in texts


def test_item_columns_can_be_hidden_one_by_one():
    doc = _document(sections={'col_rate': False})
    column = next(c for c in R._item_columns() if c['key'] == 'rate')
    sheet = R.layout_document(doc)[0]
    assert 'ریٹ' not in [op['text'] for op in sheet.ops if op['op'] == 'text']
    assert not [op for op in sheet.ops
                if op['op'] == 'text' and abs(op['x'] - column['x']) < 0.01]
    assert not [op for op in sheet.ops
                if op['op'] == 'text' and abs(op['x'] - column['label_x']) < 0.01]
    # the other columns are untouched
    assert 'مقدار' in [op['text'] for op in sheet.ops if op['op'] == 'text']


def test_summary_rows_can_be_hidden_individually():
    doc = _document(sections={'sum_total_sales': False,
                              'sum_discount': False,
                              'sum_paid': False, 'prev': False})
    labels = [row['label'] for row in doc['summary']]
    assert labels == ['کل بقایا رقم'], labels
    assert doc['summary'][0]['value'] == '646,636'


def test_total_balance_row_can_be_hidden_without_promoting_another_row():
    doc = _document(sections={'sum_total': False})
    assert not any(row.get('total') for row in doc['summary'])
    sheet = R.layout_document(doc)[0]
    total_bar = [op for op in sheet.ops
                 if op['op'] == 'rect' and op['fill'] == R.NAVY
                 and abs(op['x'] - (R.SUM_LEFT + 0.375)) < 0.01
                 and abs(op['h'] - R.SUM_ROW_H) < 0.01]
    assert not total_bar, 'no other row may take the total bar'


def test_route_can_hide_every_mark_on_the_page(client, sale):
    from app.services.urdu_receipt import SHEET_OPTIONS

    query = '&'.join(f'{key}=0' for key in SHEET_OPTIONS)
    url = (f"/download_invoice/{sale['bill_no']}?src=direct_sale"
           f"&src_id={sale['id']}&lang=ur&action=print&tc=0&{query}")
    html = client.get(url).get_data(as_text=True)
    for marker in ('مجاز دستخط', 'ڈیلیوری پرسن', 'ٹرانزیکشن کی تفصیلات',
                   'کل بقایا رقم', 'کل فروخت', 'بک شدہ فروخت کا خلاصہ'):
        assert marker not in html, marker
    # the paper itself still exists
    assert '<div class="pg"' in html


def test_print_dialog_lists_every_mark_on_the_page_map(client, sale):
    from app.services.urdu_receipt import SHEET_OPTIONS

    url = (f"/view_bill/{sale['bill_no']}?src=direct_sale"
           f"&src_id={sale['id']}")
    resp = client.get(url)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for key in SHEET_OPTIONS:
        assert f'data-urdu-section="{key}"' in html, key
    # seven page-order groups and the show-all / hide-all shortcuts
    assert html.count('data-urdu-group-toggle=') == 7
    assert 'urduMapAll' in html and 'urduMapNone' in html
    assert 'Delivery Person' in html and 'ڈیلیوری پرسن' in html
    assert 'Booked sale summary' in html and 'بک شدہ فروخت کا خلاصہ' in html


def test_delivery_person_plural_and_panel_horizontal_clamping():
    """Multiple delivery persons use ڈیلیوری پرسنز and stay inside panel bounds."""
    doc = _document(
        delivery_people=['Tariq Mahmood Gujjar Very Long Driver Name Here',
                         'Bilal Hussain Qureshi Another Driver'],
    )
    fields = dict(doc['fields'])
    assert 'ڈیلیوری پرسنز' in fields
    sheet = R.layout_document(doc)[0]
    left_texts = [op for op in sheet.ops
                  if op['op'] == 'text'
                  and abs(op['y'] - R.PANEL_LEFT_TOP) < 0.1]
    assert any(op['text'] == 'ڈیلیوری پرسنز:' for op in left_texts)
    # Every character in the left panel stays to the right of LEFT + PANEL_INNER_PAD
    value_ops = [op for op in left_texts if op['text'] != 'ڈیلیوری پرسنز:']
    assert len(value_ops) == 1
    val = value_ops[0]
    w = (R._urdu_width(val['text'], val['size']) if val['font'] == 'ur'
         else R._latin_width(val['text'], val['font'], val['size']))
    assert val['x'] - w >= R.LEFT + R.PANEL_INNER_PAD - 0.5


def test_receiving_account_appears_on_payment_direct_sale_booking_and_invoice():
    """Receiving account is never dropped by bank/holder fields or [:4] slice."""
    # 1. Bank Payment: bank_name + holder account_name + linked payment_account.name
    pay = SimpleNamespace(
        id=10, manual_bill_no='PAY-10', amount=25000,
        date_posted=datetime(2026, 11, 13, 12, 0),
        client_name='Client A', client_code='FBMCL-00001',
        method='Bank', payment_type='Receipt',
        bank_name='Meezan Bank',
        account_name='Fazal Building Materials',
        account_no='PK99MEZN0001',
        payment_account=SimpleNamespace(name='Meezan Main',
                                        account_holder_name='Fazal Building Materials',
                                        category='bank'),
        note='',
    )
    pay_doc = bill_urdu.build_document(bill_type='Payment', bill=pay)
    pay_fields = dict(pay_doc['fields'])
    assert pay_fields.get('اکاؤنٹ') == 'Meezan Main'
    assert pay_fields.get('ادائیگی') == '(بینک) Bank'

    # 2. Paid DirectSale without driver shows both method and account
    ds_paid = SimpleNamespace(
        id=11, manual_bill_no='SB-SL-11', amount=10000, paid_amount=10000,
        discount=0, date_posted=datetime(2026, 11, 13, 12, 0),
        client_name='Client A', client_code='FBMCL-00001',
        payment_method='Cash',
        payment_account=SimpleNamespace(name='Main Cash Counter', category='cash'),
        bank_name='', account_name='Main Cash Counter', account_no='',
        driver_name='', note='',
    )
    ds_paid_doc = bill_urdu.build_document(bill_type='DirectSale', bill=ds_paid)
    ds_paid_fields = dict(ds_paid_doc['fields'])
    assert ds_paid_fields.get('اکاؤنٹ') == 'Main Cash Counter'
    assert ds_paid_fields.get('ادائیگی') == '(نقد) Cash'

    # 3. Unpaid DirectSale (paid_amount=0, default payment_method='Cash', no account)
    #    must NOT print bogus Cash method
    ds_unpaid = SimpleNamespace(
        id=12, manual_bill_no='SB-SL-12', amount=10000, paid_amount=0,
        discount=0, date_posted=datetime(2026, 11, 13, 12, 0),
        client_name='Client A', client_code='FBMCL-00001',
        payment_method='Cash', payment_account=None,
        bank_name='', account_name='', account_no='',
        driver_name='', note='',
    )
    ds_unpaid_doc = bill_urdu.build_document(bill_type='DirectSale', bill=ds_unpaid)
    assert 'ادائیگی' not in dict(ds_unpaid_doc['fields'])
    assert 'اکاؤنٹ' not in dict(ds_unpaid_doc['fields'])

    # 4. Booking with receive_in_account & Paid Now > 0
    bk = SimpleNamespace(
        id=13, manual_bill_no='BK-13', amount=50000, paid_amount=20000,
        discount=0, date_posted=datetime(2026, 11, 13, 12, 0),
        client_name='Client A', client_code='FBMCL-00001',
        receive_in_account_id=5,
        receive_in_account=SimpleNamespace(name='HBL Business', category='bank'),
        note='',
    )
    bk_doc = bill_urdu.build_document(bill_type='Booking', bill=bk)
    bk_fields = dict(bk_doc['fields'])
    assert bk_fields.get('اکاؤنٹ') == 'HBL Business'
    assert bk_fields.get('ادائیگی') == '(بینک) Bank'


def test_booked_summary_renders_paginates_and_toggles_cleanly():
    """Booked Sale Summary prints columns in Urdu, obeys toggle, and paginates."""
    summary_rows = [
        {'material': 'DG Cement', 'material_ur': 'سیمنٹ DG',
         'last_stock': 120, 'dispatched': 50, 'added': 0, 'remaining': 70},
    ]
    doc_on = _document(booked_summary=summary_rows)
    texts_on = _texts(doc_on)
    for expected in ('بک شدہ فروخت کا خلاصہ', 'میٹیریل', 'پچھلا اسٹاک',
                     'ڈیلیور شدہ', 'بقایا', '120', '50', '70'):
        assert expected in texts_on, expected

    doc_off = _document(booked_summary=summary_rows,
                        sections={'booked_summary': False})
    assert 'بک شدہ فروخت کا خلاصہ' not in _texts(doc_off)

    # Booking bill uses بک شدہ instead of ڈیلیور شدہ
    bk = _bill(manual_bill_no='BK-20', paid_amount=5000)
    bk_doc = bill_urdu.build_document(
        bill_type='Booking', bill=bk,
        items=[{'name': 'سیمنٹ DG', 'qty': 100, 'price_at_time': 1250}],
        booked_summary=[{'material': 'DG Cement', 'material_ur': 'سیمنٹ DG',
                         'last_stock': 20, 'dispatched': 0, 'added': 100,
                         'remaining': 120}],
    )
    bk_texts = [op['text'] for op in R.layout_document(bk_doc)[0].ops
                if op['op'] == 'text']
    assert 'بک شدہ' in bk_texts
    assert 'ڈیلیور شدہ' not in bk_texts

    # 15 items + booked_summary + 7 terms stays within PAGE_BOTTOM across 2 pages
    many_items = [{'name': f'میٹیریل {i}', 'qty': i + 1, 'price_at_time': 500}
                  for i in range(15)]
    full_doc = _document(
        items=many_items,
        booked_summary=summary_rows,
        terms=[SimpleNamespace(title='', content_ur=text) for text in TERMS],
    )
    sheets = R.layout_document(full_doc)
    assert len(sheets) >= 2
    for sheet in sheets:
        for op in sheet.ops:
            if op['op'] == 'text':
                assert op['y'] <= R.PAGE_BOTTOM, op


def test_booking_delivery_invoice_and_payment_e2e_urdu_receipt(client, app):
    """End-to-end DB flows verify receiving account, Booked Sale Summary,
    pre-bill toggle, and QR verification across Booking, Delivery, Invoice, Payment."""
    from app import db
    from app.services import bill_verify
    from models import (
        Account, Booking, DirectSale, Invoice, Material, Payment, Settings,
    )

    _login(client)
    with app.app_context():
        settings = Settings.query.first() or Settings()
        settings.allow_global_negative_stock = True
        db.session.add(settings)
        db.session.commit()

    client.post('/add_material',
                data={'material_name': 'DG Cement', 'material_unit': 'Bag'},
                follow_redirects=True)
    client.post('/add_client', data={
        'name': 'Tariq Builders', 'code': 'FBMCL-00088',
        'category': 'General', 'opening_balance': '0',
    }, follow_redirects=True)
    client.post('/accounts/accounts/add', data={
        'name': 'Meezan Main UrduTest', 'class_category': 'Assets',
        'class_subcategory': 'Bank', 'class_account_type': 'Operating Bank',
        'bank_name': 'Meezan Bank', 'account_holder_name': 'Fazal Building Materials',
        'account_number': '1122334455',
        'account_status': 'active', 'opening_amount': '0',
        'opening_position': 'debit',
        'opening_effective_date': '2026-01-01',
    }, follow_redirects=True)
    client.post('/delivery_persons/add', data={
        'name': 'Aslam Khan', 'phone': '03001112233',
    }, follow_redirects=True)

    with app.app_context():
        mat = Material.query.filter_by(name='DG Cement').first()
        acc = Account.query.filter(Account.name.like('%Meezan Main UrduTest%')).first()
        assert mat is not None and acc is not None
        mat_id, acc_id = mat.id, acc.id

    # 1. Create a Booking with Paid Now into Meezan Main UrduTest
    resp_bk = client.post('/add_booking', data={
        'client_code': 'FBMCL-00088',
        'client_name': 'Tariq Builders',
        'manual_bill_no': 'BK-UR-88',
        'amount': '120000',
        'paid_amount': '25000',
        'payment_method': 'Bank',
        'receive_in_account_id': str(acc_id),
        'payment_account_id': str(acc_id),
        'material_name[]': ['DG Cement'],
        'material_id[]': [str(mat_id)],
        'qty[]': ['100'],
        'unit_rate[]': ['1200'],
        'date': '2026-06-01',
    }, follow_redirects=True)
    assert resp_bk.status_code == 200

    # 2. Create a Booking Delivery DirectSale with delivery person + Bank payment
    resp_ds = client.post('/add_direct_sale', data={
        'client_code': 'FBMCL-00088',
        'client_name': 'Tariq Builders',
        'category': 'Booking Delivery',
        'manual_bill_no': 'DS-UR-88',
        'driver_name': 'Aslam Khan',
        'paid_amount': '5000',
        'payment_method': 'Bank',
        'payment_account_id': str(acc_id),
        'product_name[]': ['DG Cement'],
        'material_id[]': [str(mat_id)],
        'qty[]': ['40'],
        'unit_rate[]': ['1200'],
        'date': '2026-06-02',
    }, follow_redirects=True)
    assert resp_ds.status_code == 200

    # 3. Create a Payment into Meezan Main UrduTest
    resp_pay = client.post('/add_payment', data={
        'client_code': 'FBMCL-00088',
        'client_name': 'Tariq Builders',
        'amount': '10000',
        'method': 'Bank',
        'payment_type': 'Receipt',
        'payment_account_id': str(acc_id),
        'manual_bill_no': 'PAY-UR-88',
        'date': '2026-06-03',
    }, follow_redirects=True)
    assert resp_pay.status_code == 200

    with app.app_context():
        bk = Booking.query.order_by(Booking.id.desc()).first()
        bk_id, bk_no = bk.id, (bk.manual_bill_no or bk.auto_bill_no)
        ds = DirectSale.query.order_by(DirectSale.id.desc()).first()
        ds_id, ds_no, inv_id = ds.id, (ds.manual_bill_no or ds.auto_bill_no), ds.invoice_id
        inv = db.session.get(Invoice, inv_id) if inv_id else None
        if inv is None:
            inv = Invoice(
                client_code='FBMCL-00088',
                client_name='Tariq Builders',
                invoice_no='INV-UR-88',
                total_amount=float(ds.amount or 48000),
                balance=max(0.0, float(ds.amount or 48000) - float(ds.paid_amount or 0)),
                status='OPEN',
            )
            db.session.add(inv)
            db.session.flush()
            ds.invoice_id = inv.id
            db.session.commit()
            inv_id = inv.id
        inv_no = inv.invoice_no
        pay = Payment.query.order_by(Payment.id.desc()).first()
        pay_id, pay_no = pay.id, (pay.manual_bill_no or pay.auto_bill_no)
        ds_token = bill_verify.make_token('DirectSale', ds_id, ds_no)

    # Confirm page lists Delivery Person + Booked sale summary toggles
    confirm_html = client.get(
        f'/download_invoice/{ds_no}?src=direct_sale&src_id={ds_id}&lang=ur'
    ).get_data(as_text=True)
    assert 'Delivery Person' in confirm_html
    assert 'Booked sale summary' in confirm_html
    assert 'name="booked_summary" value="1"' in confirm_html

    # Booking Urdu print shows account + Booked Sale Summary (بک شدہ)
    bk_html = client.get(
        f'/download_invoice/{bk_no}?src=booking&src_id={bk_id}&lang=ur&action=print&tc=0'
    ).get_data(as_text=True)
    assert 'Meezan Main UrduTest' in bk_html
    assert 'بک شدہ فروخت کا خلاصہ' in bk_html
    assert 'بک شدہ' in bk_html

    # Booking Delivery Urdu print shows Delivery Person + account + Booked Sale Summary
    ds_html = client.get(
        f'/download_invoice/{ds_no}?src=direct_sale&src_id={ds_id}&lang=ur&action=print&tc=0&booked_summary=1'
    ).get_data(as_text=True)
    assert 'ڈیلیوری پرسن' in ds_html
    assert 'Meezan Main UrduTest' in ds_html
    assert 'بک شدہ فروخت کا خلاصہ' in ds_html
    assert '100' in ds_html and '40' in ds_html and '60' in ds_html

    # Pre-bill print option booked_summary=0 hides Booked Sale Summary
    ds_hidden_html = client.get(
        f'/download_invoice/{ds_no}?src=direct_sale&src_id={ds_id}&lang=ur&action=print&tc=0&booked_summary=0'
    ).get_data(as_text=True)
    assert 'بک شدہ فروخت کا خلاصہ' not in ds_hidden_html

    # Payment Urdu print shows receiving account (not holder name) + method
    pay_html = client.get(
        f'/download_invoice/{pay_no}?src=payment&src_id={pay_id}&lang=ur&action=print&tc=0'
    ).get_data(as_text=True)
    assert 'Meezan Main UrduTest' in pay_html
    assert 'Bank' in pay_html

    # Linked Invoice inherits receiving account + booked summary
    inv_html = client.get(
        f'/download_invoice/{inv_no}?src=invoice&src_id={inv_id}&lang=ur&action=print&tc=0'
    ).get_data(as_text=True)
    assert 'Meezan Main UrduTest' in inv_html
    assert 'بک شدہ فروخت کا خلاصہ' in inv_html

    # Carbon-copy PDF (copies=2) renders cleanly for a delivery with booked summary
    pdf_resp = client.get(
        f'/download_invoice/{ds_no}?src=direct_sale&src_id={ds_id}&lang=ur&action=download&tc=0&copies=2&booked_summary=1'
    )
    assert pdf_resp.status_code == 200
    assert pdf_resp.content_type == 'application/pdf'
    assert pdf_resp.data[:4] == b'%PDF'

    # Public QR verify page carries Booked Sale Summary + Delivery Person + Account
    verify_resp = client.get(f'/verify/bill/{ds_token}')
    assert verify_resp.status_code == 200
    verify_html = verify_resp.get_data(as_text=True)
    assert 'بک شدہ فروخت کا خلاصہ' in verify_html
    assert 'ڈیلیوری پرسن' in verify_html
    assert 'Meezan Main UrduTest' in verify_html


def test_urdu_alt_material_name_modes():
    """The print dialog picks how an alternate-material line is named."""
    from werkzeug.datastructures import MultiDict
    from app.blueprints.sales._bills_download_invoice import (
        _translate_material_ur, _urdu_alt_name_mode,
        _with_urdu_booked_summary, _with_urdu_item_names)

    names = {'steel 20mm': 'بیس ملی میٹر', 'steel 12mm': 'بارہ ملی میٹر'}
    label = 'Steel 20mm (ALT for booked Steel 12mm)'
    both = _translate_material_ur(label, names)
    booked = _translate_material_ur(label, names, 'booked')
    delivered = _translate_material_ur(label, names, 'delivered')
    assert 'بجائے' in both and delivered in both and booked in both
    assert both == _translate_material_ur(label, names, 'both')
    assert 'بجائے' not in booked and 'بجائے' not in delivered
    assert booked == _translate_material_ur('Steel 12mm', names)
    assert delivered == _translate_material_ur('Steel 20mm', names)
    # A normal (non-alternate) line is the same in every mode.
    plain = _translate_material_ur('Steel 12mm', names)
    assert _translate_material_ur('Steel 12mm', names, 'delivered') == plain

    assert _urdu_alt_name_mode(MultiDict()) == 'both'
    assert _urdu_alt_name_mode(MultiDict([('alt_name', 'junk')])) == 'both'
    assert _urdu_alt_name_mode(MultiDict([('alt_name', 'Booked')])) == 'booked'
    assert _urdu_alt_name_mode(MultiDict([('alt_name', 'delivered')])) == 'delivered'
    assert _with_urdu_item_names([], 'ur', 'booked') == []

    summary_in = [{'material': label, 'last_stock': 100, 'dispatched': 40, 'added': 0, 'remaining': 60}]
    # _with_urdu_booked_summary honours alt_mode using Material.name_ur or fallback urdu_name()
    sum_both = _with_urdu_booked_summary(summary_in, 'both')[0]['material_ur']
    sum_booked = _with_urdu_booked_summary(summary_in, 'booked')[0]['material_ur']
    sum_deliv = _with_urdu_booked_summary(summary_in, 'delivered')[0]['material_ur']
    assert 'بجائے' in sum_both
    assert 'بجائے' not in sum_booked and 'بجائے' not in sum_deliv
    assert sum_booked == _translate_material_ur('Steel 12mm', {})
    assert sum_deliv == _translate_material_ur('Steel 20mm', {})
    assert sum_booked != sum_deliv


def test_urdu_alt_material_booked_delivered_both_e2e(client, app):
    """Selecting Booked / Delivered / Both applies across Items, Booked Summary,
    linked Invoices, fallback confirm form, view_bill_detail, and rebuilds."""
    from app import db
    from app.services.void_rebuild import rebuild_direct_sale_effects
    from models import (
        BookingAllocation, DirectSale, DirectSaleItem, Entry, Invoice,
        Material, Settings,
    )

    _login(client)
    with app.app_context():
        settings = Settings.query.first() or Settings()
        settings.allow_global_negative_stock = True
        db.session.add(settings)
        db.session.commit()

    for m_name in ('Steel 12mm', 'Steel 20mm'):
        client.post('/add_material',
                    data={'material_name': m_name, 'material_unit': 'Kg'},
                    follow_redirects=True)

    with app.app_context():
        m12 = Material.query.filter_by(name='Steel 12mm').first()
        m20 = Material.query.filter_by(name='Steel 20mm').first()
        assert m12 is not None and m20 is not None
        m12.name_ur = 'بارہ ملی میٹر'
        m20.name_ur = 'بیس ملی میٹر'
        db.session.commit()
        m12_id = m12.id

    client.post('/add_client', data={
        'name': 'Alt Client', 'code': 'FBMCL-00099',
        'category': 'General', 'opening_balance': '0',
    }, follow_redirects=True)
    client.post('/delivery_persons/add', data={
        'name': 'Akram Driver', 'phone': '03009998877',
    }, follow_redirects=True)

    # Booking for 100 Kg of Steel 12mm
    resp_bk = client.post('/add_booking', data={
        'client_code': 'FBMCL-00099',
        'client_name': 'Alt Client',
        'manual_bill_no': 'BK-ALT-99',
        'amount': '20000',
        'paid_amount': '0',
        'material_name[]': ['Steel 12mm'],
        'material_id[]': [str(m12_id)],
        'qty[]': ['100'],
        'unit_rate[]': ['200'],
        'date': '2026-06-10',
    }, follow_redirects=True)
    assert resp_bk.status_code == 200

    # 1. Pure alternate Booking Delivery: booked Steel 12mm -> delivered Steel 20mm
    resp_ds = client.post('/add_direct_sale', data={
        'client_code': 'FBMCL-00099',
        'client_name': 'Alt Client',
        'category': 'Booking Delivery',
        'manual_bill_no': 'DS-ALT-99',
        'driver_name': 'Akram Driver',
        'product_name[]': ['Steel 12mm'],
        'alternate_material[]': ['Steel 20mm'],
        'qty[]': ['40'],
        'unit_rate[]': ['0'],
        'date': '2026-06-11',
    }, follow_redirects=True)
    assert resp_ds.status_code == 200

    # 2. Mixed Transaction with Create Invoice: remaining 60 Kg booked (alternate) + 20 Kg due
    resp_mix = client.post('/add_direct_sale', data={
        'client_code': 'FBMCL-00099',
        'client_name': 'Alt Client',
        'category': 'Mixed Transaction',
        'create_invoice': '1',
        'manual_bill_no': 'MIX-ALT-99',
        'driver_name': 'Akram Driver',
        'product_name[]': ['Steel 12mm'],
        'alternate_material[]': ['Steel 20mm'],
        'qty[]': ['80'],
        'unit_rate[]': ['250'],
        'date': '2026-06-12',
    }, follow_redirects=True)
    assert resp_mix.status_code == 200

    with app.app_context():
        ds = DirectSale.query.filter(DirectSale.manual_bill_no.like('%DS-ALT-99%')).first()
        mix = DirectSale.query.filter(DirectSale.manual_bill_no.like('%MIX-ALT-99%')).first()
        assert ds is not None and mix is not None
        assert mix.invoice_id is not None
        inv = db.session.get(Invoice, mix.invoice_id)
        assert inv is not None
        ds_id, ds_no = ds.id, (ds.manual_bill_no or ds.auto_bill_no)
        mix_id, inv_id, inv_no = mix.id, inv.id, inv.invoice_no

    # Confirm page preserves query params as hidden inputs for GET submission
    confirm_html = client.get(
        f'/download_invoice/{ds_no}?src=direct_sale&src_id={ds_id}&lang=ur&action=print'
    ).get_data(as_text=True)
    assert '<input type="hidden" name="lang" value="ur">' in confirm_html
    assert '<input type="hidden" name="action" value="print">' in confirm_html
    assert '<input type="hidden" name="src" value="direct_sale">' in confirm_html
    assert f'<input type="hidden" name="src_id" value="{ds_id}">' in confirm_html

    # Pure alternate Booking Delivery checks across Booked / Delivered / Both
    booked_html = client.get(
        f'/download_invoice/{ds_no}?src=direct_sale&src_id={ds_id}&lang=ur&action=print&tc=0&booked_summary=1&alt_name=booked'
    ).get_data(as_text=True)
    assert 'بارہ ملی میٹر' in booked_html
    assert 'بیس ملی میٹر' not in booked_html
    assert 'بجائے' not in booked_html

    deliv_html = client.get(
        f'/download_invoice/{ds_no}?src=direct_sale&src_id={ds_id}&lang=ur&action=print&tc=0&booked_summary=1&alt_name=delivered'
    ).get_data(as_text=True)
    assert 'بیس ملی میٹر' in deliv_html
    assert 'بارہ ملی میٹر' not in deliv_html
    assert 'بجائے' not in deliv_html

    both_html = client.get(
        f'/download_invoice/{ds_no}?src=direct_sale&src_id={ds_id}&lang=ur&action=print&tc=0&booked_summary=1&alt_name=both'
    ).get_data(as_text=True)
    assert 'بیس ملی میٹر' in both_html
    assert 'بارہ ملی میٹر' in both_html
    assert 'بجائے' in both_html

    # Linked Invoice for Mixed Transaction preserves alternate label (Items + Booked Summary)
    inv_booked_html = client.get(
        f'/download_invoice/{inv_no}?src=invoice&src_id={inv_id}&lang=ur&action=print&tc=0&booked_summary=1&alt_name=booked'
    ).get_data(as_text=True)
    assert 'بارہ ملی میٹر' in inv_booked_html
    assert 'بجائے' not in inv_booked_html

    inv_deliv_html = client.get(
        f'/download_invoice/{inv_no}?src=invoice&src_id={inv_id}&lang=ur&action=print&tc=0&booked_summary=1&alt_name=delivered'
    ).get_data(as_text=True)
    assert 'بیس ملی میٹر' in inv_deliv_html
    assert 'بارہ ملی میٹر' not in inv_deliv_html
    assert 'بجائے' not in inv_deliv_html

    inv_both_html = client.get(
        f'/download_invoice/{inv_no}?src=invoice&src_id={inv_id}&lang=ur&action=print&tc=0&booked_summary=1&alt_name=both'
    ).get_data(as_text=True)
    assert 'بیس ملی میٹر' in inv_both_html
    assert 'بارہ ملی میٹر' in inv_both_html
    assert 'بجائے' in inv_both_html

    # view_bill_detail embeds src & src_id on Urdu print/PDF links
    detail_html = client.get(f'/view_bill_detail/DirectSale/{mix_id}').get_data(as_text=True)
    assert 'src=direct_sale' in detail_html
    assert f'src_id={mix_id}' in detail_html

    # Rebuilding a DirectSale after qty edit preserves Entry.booked_material via BookingAllocation
    with app.app_context():
        ds = db.session.get(DirectSale, ds_id)
        dsi = DirectSaleItem.query.filter_by(sale_id=ds_id).first()
        assert BookingAllocation.query.filter_by(sale_item_id=dsi.id, is_void=False).first() is not None
        dsi.qty = 35
        db.session.flush()
        rebuild_direct_sale_effects(ds, rebuild_stock=False)
        db.session.commit()
        rebuilt_entry = Entry.query.filter_by(
            source_module='sales', source_table='direct_sale', source_id=ds_id, is_void=False
        ).first()
        assert rebuilt_entry is not None
        assert rebuilt_entry.booked_material == 'Steel 12mm'

    rebuilt_booked_html = client.get(
        f'/download_invoice/{ds_no}?src=direct_sale&src_id={ds_id}&lang=ur&action=print&tc=0&booked_summary=1&alt_name=booked'
    ).get_data(as_text=True)
    assert 'بارہ ملی میٹر' in rebuilt_booked_html
    assert 'بیس ملی میٹر' not in rebuilt_booked_html

