"""The Bill No column must show the bill that was actually saved.

Regression for the reported problem: a sale posted against a booking item
(category ``Booking Delivery``) was saved with a system bill number
(``SB-SL-1005``) but the Direct Sales list showed ``CASH-SALE`` in the
**Bill No** column, pushing the real number into **System Ref**.  Operators
read that as "my bill number was thrown away" and could not find the bill by
the number they typed.

The list must therefore:

1. show the manual number in **Bill No** (already worked),
2. otherwise show the auto/system number in **Bill No** — never a
   ``CASH-SALE`` placeholder when a real number exists,
3. keep the system reference visible without duplicating the same string in
   both columns.
"""
from datetime import datetime

from models import (
    Booking, BookingItem, Client, DirectSale, DirectSaleItem, Material,
    DeliveryPerson, db,
)

MATERIAL = "20mm steel"


def _login(client, username="Admin", password="Admin@fbm12345"):
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code in (302, 303)


def _seed():
    """One client, one material with stock, one open booking for the client."""
    client_row = Client(code="FBMCL-T1", name="FAISAL", category="General", is_active=True)
    material = Material(code="MAT-STEEL", name=MATERIAL, unit="Kg", unit_price=100,
                        total=100, is_active=True)
    driver = DeliveryPerson(name="Test Driver", is_active=True)
    db.session.add_all([client_row, material, driver])
    db.session.commit()

    booking = Booking(client_name=client_row.name, amount=1000, paid_amount=1000,
                      manual_bill_no="BK-1", auto_bill_no="SB-BK-1001",
                      date_posted=datetime(2026, 11, 1, 9, 0), is_void=False)
    db.session.add(booking)
    db.session.flush()
    db.session.add(BookingItem(booking_id=booking.id, material_name=MATERIAL,
                               qty=10, price_at_time=100))
    db.session.commit()
    return client_row.code, material.name


def _post_booking_delivery(app, client, *, manual_bill_no=""):
    with app.app_context():
        code, material_name = _seed()
        material_id = Material.query.filter_by(name=material_name).one().id
        driver_id = DeliveryPerson.query.filter_by(name="Test Driver").one().id

    _login(client)
    data = {
        "client_code": code,
        "client_name": "FAISAL",
        "category": "Booking Delivery",
        "manual_bill_no": manual_bill_no,
        "driver_name": "Test Driver",
        "delivery_person_id[]": [str(driver_id)],
        "paid_amount": "0",
        "payment_method": "Cash",
        "product_name[]": [material_name],
        "material_id[]": [str(material_id)],
        "qty[]": ["1"],
        "unit_rate[]": ["100"],
        "create_invoice": "0",
        "has_bill": "1",
    }
    response = client.post("/add_direct_sale", data=data, follow_redirects=True)
    assert response.status_code == 200

    with app.app_context():
        sale = DirectSale.query.order_by(DirectSale.id.desc()).first()
        assert sale is not None, "the sale was not posted"
        return sale.id, sale.auto_bill_no, sale.manual_bill_no


def _row_cells(html, sale_id):
    """Return the ``<td>`` text of the Bill No and System Ref cells for a sale."""
    marker = f"src_id={sale_id}"
    index = html.find(marker)
    assert index != -1, "the sale row is not on the page"
    start = html.rfind("<tr", 0, index)
    end = html.find("</tr>", index)
    row = html[start:end]
    cells = row.split("<td")
    return cells


def test_booking_delivery_shows_its_system_bill_no_in_bill_no_column(app, client):
    sale_id, auto_bill_no, manual_bill_no = _post_booking_delivery(app, client)
    assert auto_bill_no, "a posted sale must carry a system bill number"
    assert not manual_bill_no

    response = client.get("/direct_sales?show=all")
    html = response.get_data(as_text=True)
    cells = _row_cells(html, sale_id)
    # cells[0] is the text before the first <td>; Bill No is the first cell.
    bill_no_cell = cells[1]
    assert auto_bill_no in bill_no_cell, (
        "the saved bill number must appear in the Bill No column, got: "
        f"{bill_no_cell!r}"
    )
    assert "CASH-SALE" not in bill_no_cell, (
        "a sale with a real bill number must not be labelled CASH-SALE"
    )


def test_manual_bill_no_still_leads_the_bill_no_column(app, client):
    """A typed number keeps its place; the system ref is not duplicated in it."""
    sale_id, auto_bill_no, manual_bill_no = _post_booking_delivery(
        app, client, manual_bill_no="SB-SL-2001")
    # The shop's manual numbers are stored with the ``MB NO.`` prefix.
    assert manual_bill_no.endswith("SB-SL-2001")

    response = client.get("/direct_sales?show=all")
    cells = _row_cells(response.get_data(as_text=True), sale_id)
    bill_no_cell = cells[1]
    system_ref_cell = cells[2]
    assert manual_bill_no in bill_no_cell
    assert "CASH-SALE" not in bill_no_cell
    # The auto number is already the operator's reference for this sale; it is
    # listed once, under System Ref, not twice in the same row.
    assert auto_bill_no in system_ref_cell
    assert auto_bill_no not in bill_no_cell


def test_cash_sale_without_any_number_keeps_the_cash_sale_placeholder(app, client):
    """The placeholder is still shown — but only when there is no number at all."""
    with app.app_context():
        code, material_name = _seed()
        material_id = Material.query.filter_by(name=material_name).one().id
        driver_id = DeliveryPerson.query.filter_by(name="Test Driver").one().id

    _login(client)
    # An unbilled cash sale: the form clears the bill fields for that mode.
    response = client.post("/add_direct_sale", data={
        "client_code": code,
        "client_name": "FAISAL",
        "category": "Cash",
        "manual_bill_no": "",
        "driver_name": "Test Driver",
        "delivery_person_id[]": [str(driver_id)],
        # Paid later from the ledger: a fresh test database has no cash
        # account to receive a paid amount, and that is not what this test is
        # about.
        "paid_amount": "0",
        "payment_method": "Cash",
        "product_name[]": [material_name],
        "material_id[]": [str(material_id)],
        "qty[]": ["1"],
        "unit_rate[]": ["100"],
        "create_invoice": "0",
        "has_bill": "1",
    }, follow_redirects=True)
    assert response.status_code == 200

    with app.app_context():
        sale = DirectSale.query.filter_by(category="Cash").order_by(
            DirectSale.id.desc()).first()
        assert sale is not None, "the cash sale was not posted"
        sale_id = sale.id
        # The row still carries a system number, so the placeholder is only
        # correct for a legacy row that has neither number.
        assert sale.auto_bill_no

    cells = _row_cells(client.get("/direct_sales?show=all").get_data(as_text=True), sale_id)
    assert "CASH-SALE" not in cells[1], "a numbered cash sale must show its number"

    # A genuinely numberless legacy row falls back to the placeholder.
    with app.app_context():
        legacy = DirectSale(client_name="FAISAL", category="Cash", amount=100,
                            paid_amount=0, auto_bill_no=None,
                            manual_bill_no=None, date_posted=datetime(2026, 11, 1, 9, 0),
                            is_void=False)
        db.session.add(legacy)
        db.session.commit()
        legacy_id = legacy.id
        db.session.add(DirectSaleItem(sale_id=legacy_id, product_name=MATERIAL,
                                      qty=1, price_at_time=100))
        db.session.commit()

    cells = _row_cells(client.get("/direct_sales?show=all").get_data(as_text=True), legacy_id)
    assert "CASH-SALE" in cells[1]
