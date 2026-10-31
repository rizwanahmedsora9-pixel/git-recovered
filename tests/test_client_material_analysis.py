"""Material Analysis tab on the Client Ledger page.

Verifies the unique "Material Analysis" tab on /ledger/<id>:
* the tab exists exactly once, only on the Client Ledger page (no duplicate),
* both report options (Category-Wise, Material-Wise) render with grand totals,
* the numbers equal an independent recomputation from the client's own
  booking/direct-sale bill lines, and both reports reconcile with each other,
* a direct sale follows its client code even when the name snapshot drifted,
* one master material is reported once even when spellings differ,
* rent materials keep their master category and still count as rental,
* rows of OTHER clients never leak into the analysis,
* a client with no bills gets a clean empty state and zero totals.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from models import (
    db, Client, Material, MaterialCategory, Service, Booking, BookingItem,
    DirectSale, DirectSaleItem,
)


def login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def csrf_token(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token")


def _money(v):
    return "{:,.2f}".format(Decimal(str(v)).quantize(Decimal("0.01")))


def _qty(v):
    return "{:,.3f}".format(Decimal(str(v))).rstrip("0").rstrip(".")


def _seed(app, client):
    """Create categories/materials/service and one client via the real endpoint."""
    login(client)
    tok = csrf_token(client)

    r = client.post("/add_client", data={
        "name": "MA CLIENT",
        "code": "MA-001",
        "category": "General",
        "opening_balance": "0",
        "_csrf_token": tok,
    }, follow_redirects=True)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]

    with app.app_context():
        cement_cat = MaterialCategory(name="Cement")
        tools_cat = MaterialCategory(name="Tools")
        db.session.add_all([cement_cat, tools_cat])
        db.session.flush()

        cement = Material(name="OPC CEMENT", code="MT-001", category_id=cement_cat.id,
                          unit_price=1200, unit="Bags")
        jack = Material(name="JACK HAMMER", code="MT-002", category_id=tools_cat.id,
                        unit_price=500, unit="Pcs")
        rent = Service(name="RENT-CEMENT", code="SVC-001", unit="Bags")
        db.session.add_all([cement, jack, rent])
        db.session.flush()

        ma_client = Client.query.filter_by(code="MA-001").first()
        assert ma_client is not None

        # --- This client's billed lines ---
        # Booking: 500 cement @1200 + 2 rent @100
        b = Booking(client_name="MA CLIENT", amount=600200, paid_amount=0,
                    manual_bill_no="MB-MA-1", date_posted=datetime(2026, 2, 1, 10, 0, 0))
        b.items.append(BookingItem(material_name="OPC CEMENT", qty=500,
                                   price_at_time=1200, discount=0))
        b.items.append(BookingItem(material_name="RENT-CEMENT", qty=2,
                                   price_at_time=100, discount=0))
        db.session.add(b)
        # Direct Sale: 10 jack hammers @500 + 5 cement @1200
        s = DirectSale(client_name="MA CLIENT", client_code="MA-001",
                       category="Cash", amount=5600, paid_amount=5600,
                       manual_bill_no="DS-MA-1", date_posted=datetime(2026, 2, 5, 11, 0, 0))
        s.items.append(DirectSaleItem(product_name="JACK HAMMER", qty=10,
                                      price_at_time=500, discount=0))
        s.items.append(DirectSaleItem(product_name="OPC CEMENT", qty=5,
                                      price_at_time=1200, discount=0))
        db.session.add(s)

        # --- Another client's booking (must NOT leak into MA CLIENT's analysis) ---
        b2 = Booking(client_name="OTHER CLIENT", amount=99900, paid_amount=0,
                     manual_bill_no="MB-OTHER-1", date_posted=datetime(2026, 2, 2, 10, 0, 0))
        b2.items.append(BookingItem(material_name="OPC CEMENT", qty=999,
                                    price_at_time=100, discount=0))
        db.session.add(b2)
        db.session.commit()

        return ma_client.id


def _section(html, marker):
    """HTML from *marker* to the first </table> after it (one report table)."""
    i = html.find(marker)
    if i < 0:
        return ""
    j = html.find("</table>", i)
    return html[i:j if j >= 0 else len(html)]


def test_material_analysis_tab_is_unique_and_present(app, client):
    client_id = _seed(app, client)
    html = client.get(f"/ledger/{client_id}").get_data(as_text=True)

    # Tab exists, exactly once (no duplicate tab).
    assert 'id="material-analysis-tab"' in html
    assert html.count('id="material-analysis-tab"') == 1
    assert html.count('id="ledger-tab"') == 1
    # Both report options are offered.
    assert "Category-Wise Report" in html
    assert "Material-Wise Report" in html


def test_material_analysis_material_wise_totals(app, client):
    client_id = _seed(app, client)
    html = client.get(f"/ledger/{client_id}").get_data(as_text=True)
    table = _section(html, 'id="maMaterialReport"')
    assert table, "material-wise report section not rendered"

    # OPC CEMENT: booking 500 + direct sale 5 = 505 @1200 -> 606,000.00
    assert _money(606000) in table
    # JACK HAMMER: 10 @500 -> 5,000.00
    assert _money(5000) in table
    # RENT-CEMENT rent: 2 @100 -> 200.00
    assert _money(200) in table
    # Material-wise grand total: qty 517, amount 611,200.00, rent 200.00.
    assert _money(611200) in table
    assert _qty(517) in table
    # The other client's 999-bag booking (99,900.00) must never appear here.
    assert "99,900.00" not in table
    assert _qty(999) not in table


def test_material_analysis_category_wise_totals(app, client):
    client_id = _seed(app, client)
    html = client.get(f"/ledger/{client_id}").get_data(as_text=True)
    table = _section(html, 'id="maCategoryReport"')
    assert table, "category-wise report section not rendered"

    # Category rows exist with the right bucket names.
    assert "Cement" in table
    assert "Tools" in table
    assert "Rental / Services" in table

    # Category-wise payment totals: Cement 606,000 + Tools 5,000 = 611,200.00
    assert _money(611200) in table
    # Grand total row rendered.
    assert "Grand Total" in table
    # The other client's data must not leak into the category report either.
    assert "99,900.00" not in table


def test_material_analysis_numbers_match_independent_recomputation(app, client):
    """The on-screen analysis equals a from-scratch ORM recomputation."""
    client_id = _seed(app, client)
    html = client.get(f"/ledger/{client_id}").get_data(as_text=True)

    with app.app_context():
        from app.services.lookups import is_service_name

        cli = db.session.get(Client, client_id)
        norm = cli.name.strip().lower()
        expected = {}

        def add(name, qty, rate, disc=0):
            amt = max(0.0, round(float(qty) * float(rate) - float(disc), 2))
            e = expected.setdefault(name, [0.0, 0.0, 0.0])
            e[0] += float(qty)
            e[1] += amt
            if is_service_name(name):
                e[2] += amt

        for b in Booking.query.filter(
            Booking.is_void == False,
            db.func.lower(db.func.trim(Booking.client_name)) == norm,
        ):
            for it in b.items:
                add(it.material_name, it.qty, it.price_at_time, it.discount)
        for s in DirectSale.query.filter(
            DirectSale.is_void == False,
            db.func.lower(db.func.trim(DirectSale.client_name)) == norm,
        ):
            for it in s.items:
                add(it.product_name, it.qty, it.price_at_time, it.discount)

        total_qty = sum(v[0] for v in expected.values())
        total_amt = sum(v[1] for v in expected.values())
        total_rent = sum(v[2] for v in expected.values())

    table = _section(html, 'id="maMaterialReport"')
    assert _money(total_amt) in table
    assert _money(total_rent) in table
    assert _qty(total_qty) in table


# ---------------------------------------------------------------------------
# Layout / scope / consistency checks
# ---------------------------------------------------------------------------

def _table_rows(section_html):
    """Plain-text cells for every <tr> in *section_html* (ignores thead)."""
    body = section_html
    rows = []
    for row in re.findall(r"<tr\b.*?</tr>", body, flags=re.S | re.I):
        if re.search(r"<th\b", row, flags=re.I):
            continue
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, flags=re.S | re.I)
        text = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip() for c in cells]
        rows.append(text)
    return rows


def test_material_analysis_tab_only_on_client_ledger_page(app, client):
    """The new tab is added to the Client Ledger page and nowhere else."""
    client_id = _seed(app, client)
    assert 'id="material-analysis-tab"' in client.get(f"/ledger/{client_id}").get_data(as_text=True)

    for url in ("/clients", "/ledger", "/materials", "/financial_ledger"):
        resp = client.get(url)
        if resp.status_code != 200:
            continue
        assert 'id="material-analysis-tab"' not in resp.get_data(as_text=True), url


def test_material_analysis_reports_have_consistent_totals(app, client):
    """Category totals + material rows must both reconcile to the grand totals."""
    client_id = _seed(app, client)
    html = client.get(f"/ledger/{client_id}").get_data(as_text=True)

    cat_rows = _table_rows(_section(html, 'id="maCategoryReport"'))
    mat_rows = _table_rows(_section(html, 'id="maMaterialReport"'))

    def money(txt):
        return Decimal(txt.replace(",", "").replace("%", "").strip() or "0")

    # Category table: rows + final "Grand Total" row.
    assert cat_rows and cat_rows[-1][0] == "Grand Total"
    body, total = cat_rows[:-1], cat_rows[-1]
    assert sum(money(r[3]) for r in body) == money(total[3])
    assert sum(money(r[4]) for r in body) == money(total[4])
    # "Materials" column of the category rows adds up to the grand total row.
    assert sum(int(r[1]) for r in body) == int(total[1])

    # Material table: rows + final "Grand Total" row (its first cell spans the
    # index/material/category columns, so the figures are the last three cells).
    assert mat_rows and mat_rows[-1][0] == "Grand Total"
    body, total = mat_rows[:-1], mat_rows[-1]
    assert sum(money(r[4]) for r in body) == money(total[-2])
    assert sum(money(r[5]) for r in body) == money(total[-1])

    # Both reports agree with each other on the grand totals.
    assert money(cat_rows[-1][3]) == money(mat_rows[-1][-2])
    assert money(cat_rows[-1][4]) == money(mat_rows[-1][-1])


def test_material_analysis_direct_sale_matched_by_client_code(app, client):
    """A DirectSale keeps belonging to its client even if the name snapshot drifted."""
    client_id = _seed(app, client)
    with app.app_context():
        sale = DirectSale(client_name="MA CLIENT (OLD SPELLING)", client_code="MA-001",
                          category="Cash", amount=7500, paid_amount=0,
                          manual_bill_no="DS-MA-2", date_posted=datetime(2026, 2, 9, 9, 0, 0))
        sale.items.append(DirectSaleItem(product_name="JACK HAMMER", qty=15,
                                         price_at_time=500, discount=0))
        db.session.add(sale)
        db.session.commit()

    html = client.get(f"/ledger/{client_id}").get_data(as_text=True)
    table = _section(html, 'id="maMaterialReport"')
    # 10 (seed) + 15 = 25 hammers -> 12,500.00 ; grand total 611,200 + 7,500.
    assert _qty(25) in table
    assert _money(12500) in table
    assert _money(618700) in table


def test_material_analysis_same_material_spellings_are_one_row(app, client):
    """Bill lines that name the same master material are reported once."""
    client_id = _seed(app, client)
    with app.app_context():
        b = Booking(client_name="MA CLIENT", amount=1200, paid_amount=0,
                    manual_bill_no="MB-MA-2", date_posted=datetime(2026, 2, 10, 10, 0, 0))
        b.items.append(BookingItem(material_name="  opc   cement ", qty=1,
                                   price_at_time=1200, discount=0))
        db.session.add(b)
        db.session.commit()

    html = client.get(f"/ledger/{client_id}").get_data(as_text=True)
    table = _section(html, 'id="maMaterialReport"')
    assert table.count('ma-mat-name') >= 1
    assert table.count("OPC CEMENT") == 1          # one row, master spelling
    assert "opc   cement" not in table             # the variant is folded in
    # 505 + 1 = 506 bags, 607,200.00 billed.
    assert _qty(506) in table
    assert _money(607200) in table


def test_material_analysis_empty_client_renders_clean_empty_state(app, client):
    """A client with no bills still gets the tab, both reports and zero totals."""
    login(client)
    tok = csrf_token(client)
    client.post("/add_client", data={
        "name": "MA EMPTY CLIENT", "code": "MA-EMPTY", "category": "General",
        "opening_balance": "0", "_csrf_token": tok,
    }, follow_redirects=True)
    with app.app_context():
        empty_id = Client.query.filter_by(code="MA-EMPTY").first().id

    resp = client.get(f"/ledger/{empty_id}")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'id="material-analysis-tab"' in html
    assert html.count("No billed material lines found for this client yet.") == 2
    assert "0.00" in html
    assert "Grand Total" in html


def test_material_analysis_rent_material_keeps_its_master_category(app, client):
    """A rent material in the master is reported under its own category and
    still counts towards rental charges (nothing is re-categorised)."""
    login(client)
    tok = csrf_token(client)
    client.post("/add_client", data={
        "name": "RENT CAT CLIENT", "code": "MA-RENT", "category": "General",
        "opening_balance": "0", "_csrf_token": tok,
    }, follow_redirects=True)

    with app.app_context():
        cat = MaterialCategory(name="RENT")
        db.session.add(cat)
        db.session.flush()
        db.session.add(Material(name="RENT-CEMENT", code="MT-R1", category_id=cat.id,
                                unit_price=100, unit="Bags"))
        db.session.flush()
        rid = Client.query.filter_by(code="MA-RENT").first().id

        b = Booking(client_name="RENT CAT CLIENT", amount=2300, paid_amount=0,
                    manual_bill_no="MB-RENT-1", date_posted=datetime(2026, 3, 1, 9, 0, 0))
        b.items.append(BookingItem(material_name="RENT-CEMENT", qty=3,
                                   price_at_time=100, discount=0))
        b.items.append(BookingItem(material_name="RENT-CEMENT", qty=20,
                                   price_at_time=100, discount=0))
        db.session.add(b)
        db.session.commit()

    html = client.get(f"/ledger/{rid}").get_data(as_text=True)
    cat_table = _section(html, 'id="maCategoryReport"')
    mat_table = _section(html, 'id="maMaterialReport"')

    # One category row: RENT, 23 bags, 2,300.00, all of it rental.
    assert "RENT" in cat_table
    assert "2,300.00" in cat_table
    assert "Rental / Services" not in cat_table
    # One material row even though the line was billed twice.
    assert mat_table.count("RENT-CEMENT") == 1
    assert _qty(23) in mat_table
    assert _money(2300) in mat_table
