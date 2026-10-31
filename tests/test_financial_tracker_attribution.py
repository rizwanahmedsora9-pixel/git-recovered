"""Regression tests for the Section / User columns on ALL TRANSACTIONS.

The cash flow list is projected from ``AccountTransaction`` rows that are often
written by sync helpers without a username.  The list must still show *who*
made the entry (resolved from the source document) and *which section* of the
app it was made in (derived from the source document type).
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _login(client):
    return client.post("/login", data=dict(ADMIN), follow_redirects=True)


def _mk_account(db, Account, name="Drawer Cash", category="cash", opening=10000.0):
    acc = Account(
        name=name,
        type="CASH" if category == "cash" else "BANK",
        account_type="company",
        category=category,
        balance=opening,
        opening_balance=opening,
        opening_balance_date=datetime(2026, 1, 1),
        is_active=True,
    )
    db.session.add(acc)
    db.session.commit()
    return acc


def _mk_supplier(db, Supplier, name="Supplier 04"):
    sup = Supplier(name=name, is_active=True)
    db.session.add(sup)
    db.session.commit()
    return sup


def test_section_label_mapping():
    from app.services.cash_flow_svc import cf_section_label

    assert cf_section_label("CashFlowEntry", "Expense") == "Cash Flow"
    assert cf_section_label("MANUAL_CASH_FLOW", "Receipt") == "Cash Flow"
    assert cf_section_label("Booking", "Receipt") == "Bookings"
    assert cf_section_label("DirectSale", "Receipt") == "Direct Sales"
    assert cf_section_label("Payment", "Receipt") == "Client Payments"
    assert cf_section_label("Payment", "Refund") == "Client Refunds"
    assert cf_section_label("SupplierPayment", "Supplier Payment") == "Supplier Payments"
    assert cf_section_label("GRN", "") == "GRN"
    assert cf_section_label("MaterialReturn", "") == "Sales Returns"
    assert cf_section_label("DeliveryPersonPayment", "Driver Payment") == "Delivery & Rent"
    assert cf_section_label("AccountReconciliation", "") == "Reconciliation"
    assert cf_section_label(None, "Transfer", default="Accounts") == "Accounts"


def test_rows_resolve_creator_and_section(app):
    from models import (
        db, Account, AccountTransaction, CashFlowEntry, Supplier, SupplierPayment,
    )
    from app.services.cash_flow_svc import collect_cash_flow_rows

    today = date.today()
    with app.app_context():
        acc = _mk_account(db, Account)
        sup = _mk_supplier(db, Supplier)

        # A supplier payment entered by "ahmed" (ledger row written by a sync
        # helper, so the ledger itself carries no username).
        sp = SupplierPayment(
            supplier_id=sup.id,
            amount=70000.0,
            method="Cash",
            payment_type="Payment",
            source_type="Manual",
            date_posted=datetime(today.year, today.month, today.day, 13, 4),
            payment_account_id=acc.id,
            created_by="ahmed",
            is_void=False,
        )
        db.session.add(sp)
        db.session.flush()
        db.session.add(AccountTransaction(
            from_account_id=acc.id,
            to_account_id=None,
            amount=70000.0,
            description="Supplier payment to Supplier 04 (SB-SP-1002)",
            transaction_type="Supplier Payment",
            source_type="SupplierPayment",
            source_id=sp.id,
            date_posted=datetime(today.year, today.month, today.day, 13, 4),
        ))

        # A manual cash flow entry entered by "sara".
        from app.services.cash_flow_svc import save_manual_cash_flow_entry
        entry, created = save_manual_cash_flow_entry(
            direction="in",
            amount=5000.0,
            account_id=acc.id,
            category_name="SALE",
            description="Counter sale",
            date_posted=datetime(today.year, today.month, today.day, 14, 0),
            actor=SimpleNamespace(username="sara"),
        )
        assert created

        db.session.commit()

        rows = collect_cash_flow_rows(
            today.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")
        )

    by_ref = {r["reference"]: r for r in rows}
    supplier_rows = [r for r in rows if r["source_type"] == "SupplierPayment"]
    assert supplier_rows, rows
    assert supplier_rows[0]["section_label"] == "Supplier Payments"
    assert supplier_rows[0]["created_by"] == "ahmed"

    manual = [r for r in rows if r["entry_id"] == entry.id]
    assert manual, rows
    assert manual[0]["section_label"] == "Cash Flow"
    assert manual[0]["created_by"] == "sara"
    assert by_ref  # sanity: references are populated


def test_matrix_renders_section_and_user_columns(app, client):
    from models import db, Account
    from app.services.cash_flow_svc import save_manual_cash_flow_entry

    today = date.today()
    with app.app_context():
        acc = _mk_account(db, Account)
        save_manual_cash_flow_entry(
            direction="out",
            amount=1200.0,
            account_id=acc.id,
            category_name="EXPENSE",
            description="Tea & refreshments",
            date_posted=datetime(today.year, today.month, today.day, 9, 30),
            actor=SimpleNamespace(username="ahmed"),
        )
        db.session.commit()

    _login(client)
    rv = client.get("/cash_flow")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert "ALL TRANSACTIONS" in body
    # New headers.
    assert ">Section</th>" in body
    assert ">User</th>" in body
    # New cell values.
    assert "Cash Flow" in body
    assert "ahmed" in body
