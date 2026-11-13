"""Verify that deleting any entry from Accounts or Cash Flow cleans it from Cash Flow and All Transactions."""
from __future__ import annotations

from datetime import datetime

import pytest

from models import (
    Account,
    AccountTransaction,
    CashFlowCategory,
    CashFlowEntry,
    CashFlowParty,
    Client,
    DeliveryPerson,
    DeliveryPersonPayment,
    GRN,
    GRNItem,
    Material,
    Payment,
    Supplier,
    SupplierPayment,
    db,
)
from app.services.cash_flow_svc import (
    collect_cash_flow_rows,
    delete_cf_category,
    delete_cf_party,
)


def _login(client):
    response = client.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)


@pytest.fixture()
def cf_setup(app, client):
    _login(client)
    with app.app_context():
        account = Account(
            name="CF Repro Cash",
            type="company",
            category="cash",
            source_category="Company",
            account_type="company",
            balance=10_000.0,
            balance_minor=1_000_000,
            opening_balance=10_000.0,
            opening_balance_minor=1_000_000,
            opening_balance_date=datetime(2026, 1, 1),
            is_active=True,
        )
        bank = Account(
            name="CF Repro Bank",
            type="company",
            category="bank",
            source_category="Company",
            account_type="company",
            balance=5_000.0,
            balance_minor=500_000,
            opening_balance=5_000.0,
            opening_balance_minor=500_000,
            opening_balance_date=datetime(2026, 1, 1),
            is_active=True,
        )
        category = CashFlowCategory(name="Office Expenses", direction="out", is_active=True)
        party = CashFlowParty(name="Landlord", party_type="person", is_active=True)
        db.session.add_all([account, bank, category, party])
        db.session.commit()
        return {
            "account_id": account.id,
            "bank_id": bank.id,
            "category_id": category.id,
            "party_id": party.id,
        }


def test_manual_cash_flow_entry_void_disappears_from_cash_flow_and_all_transactions(app, client, cf_setup):
    ids = cf_setup
    response = client.post(
        "/cash_flow",
        data={
            "action": "record_movement",
            "direction": "out",
            "amount": "250.25",
            "cash_account_id": str(ids["account_id"]),
            "category_id": str(ids["category_id"]),
            "category_name": "Office Expenses",
            "party_id": str(ids["party_id"]),
            "party_name": "Landlord",
            "party_type": "person",
            "description": "November office rent",
            "note": "Rent voucher",
            "reference": "CF-001",
            "movement_date": "2026-11-09",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with app.app_context():
        entry = CashFlowEntry.query.one()
        tx = AccountTransaction.query.one()
        entry_id, tx_id = entry.id, tx.id
        assert entry.account_tx_id == tx.id
        assert len(collect_cash_flow_rows("2026-11-09", "2026-11-09")) == 1

    response = client.post(
        "/cash_flow",
        data={
            "action": "void_entry",
            "entry_id": str(entry_id),
            "void_reason": "Duplicate entry",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with app.app_context():
        assert db.session.get(CashFlowEntry, entry_id) is None
        assert db.session.get(AccountTransaction, tx_id) is None
        account = db.session.get(Account, ids["account_id"])
        assert account.balance_minor == 1_000_000
        assert collect_cash_flow_rows("2026-11-09", "2026-11-09") == []
        assert collect_cash_flow_rows("2026-11-09", "2026-11-09", include_voided=True) == []

    cf_all = client.get("/cash_flow?from_date=2026-11-09&to_date=2026-11-09&status=all&ft_preset=all")
    assert cf_all.status_code == 200
    html = cf_all.get_data(as_text=True)
    assert "CF-001" not in html
    assert "November office rent" not in html
    assert "ALL TRANSACTIONS (0)" in html


def test_account_expense_delete_removes_from_cash_flow_and_all_transactions(app, client, cf_setup):
    ids = cf_setup
    client.post(
        "/accounts/transactions/new",
        data={
            "tx_mode": "pay",
            "pay_target": "other_expense",
            "pay_from_account_id": str(ids["account_id"]),
            "amount": "250.25",
            "method": "Cash",
            "target_label": "November office rent",
            "expense_category_id": str(ids["category_id"]),
            "expense_subcategory_id": "",
            "expense_party_id": str(ids["party_id"]),
            "reference": "EXP-001",
            "note": "Rent voucher",
            "date_posted": "2026-11-09T10:30",
            "idempotency_key": "repro-exp-delete-1",
        },
        follow_redirects=False,
    )

    with app.app_context():
        tx = AccountTransaction.query.one()
        entry = CashFlowEntry.query.one()
        tx_id, entry_id = tx.id, entry.id
        assert len(collect_cash_flow_rows("2026-11-09", "2026-11-09")) == 1

    response = client.post(
        f"/accounts/transactions/{tx_id}/delete",
        data={"void_reason": "Duplicate voucher"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with app.app_context():
        assert db.session.get(AccountTransaction, tx_id) is None
        assert db.session.get(CashFlowEntry, entry_id) is None
        account = db.session.get(Account, ids["account_id"])
        assert account.balance_minor == 1_000_000
        assert collect_cash_flow_rows("2026-11-09", "2026-11-09") == []
        assert collect_cash_flow_rows("2026-11-09", "2026-11-09", include_voided=True) == []
        # Category and party are also free to be deleted now that the entry was deleted.
        assert delete_cf_category(ids["category_id"]) is True
        assert delete_cf_party(ids["party_id"]) is True
        db.session.commit()

    cf_all = client.get("/cash_flow?from_date=2026-11-09&to_date=2026-11-09&status=all&ft_preset=all")
    assert cf_all.status_code == 200
    html = cf_all.get_data(as_text=True)
    assert "EXP-001" not in html
    assert "November office rent" not in html
    assert "ALL TRANSACTIONS (0)" in html


def test_deleting_any_accounts_entry_cleans_cash_flow_and_all_transactions(app, client, cf_setup):
    ids = cf_setup
    with app.app_context():
        cust = Client(name="Clean Test Client", code="CTC-01", is_active=True)
        sup = Supplier(name="Clean Test Supplier", is_active=True)
        drv = DeliveryPerson(name="Clean Test Driver", opening_balance=500, is_active=True)
        mat = Material(name="Clean Test Cement", code="MAT-CTC", unit="Bag", total=0)
        db.session.add_all([cust, sup, drv, mat])
        db.session.commit()
        client_id, supplier_id, driver_id = cust.id, sup.id, drv.id

    # 1. Create a GRN so we can post a GRN payment
    client.post(
        "/grn",
        data={
            "action": "add",
            "bill_no": "GRN-CLEAN-1",
            "supplier_id": str(supplier_id),
            "date": "2026-11-09",
            "mat_name[]": "Clean Test Cement",
            "qty[]": "10",
            "price[]": "100",
        },
        follow_redirects=False,
    )
    with app.app_context():
        grn = GRN.query.one()
        grn_id = grn.id

    # 2. Record a GRN payment, a Client payment, a standalone Supplier payment, a Driver payment, and a Transfer
    client.post(
        "/accounts/grn_payments/save",
        data={
            "grn_id": str(grn_id),
            "obligation_key": f"SUPPLIER:{grn_id}",
            "amount": "400",
            "mode": "SUPPLIER",
            "method": "Cash",
            "payment_account_id": str(ids["account_id"]),
            "date_posted": "2026-11-09",
            "reference": "GP-CLEAN-1",
        },
        follow_redirects=False,
    )
    client.post(
        "/accounts/payments/clients/save",
        data={
            "client_code": "CTC-01",
            "amount": "600",
            "payment_type": "Receipt",
            "method": "Cash",
            "payment_account_id": str(ids["account_id"]),
            "manual_bill_no": "CP-CLEAN-1",
            "date": "2026-11-09",
            "note": "Client receipt test",
        },
        follow_redirects=False,
    )
    client.post(
        "/accounts/transactions/new",
        data={
            "tx_mode": "pay",
            "pay_target": "supplier",
            "pay_from_account_id": str(ids["account_id"]),
            "supplier_id": str(supplier_id),
            "amount": "300",
            "method": "Cash",
            "note": "SP-CLEAN-1",
            "date_posted": "2026-11-09T11:00",
        },
        follow_redirects=False,
    )
    client.post(
        "/accounts/transactions/new",
        data={
            "tx_mode": "pay",
            "pay_target": "driver",
            "pay_from_account_id": str(ids["account_id"]),
            "delivery_person_id": str(driver_id),
            "amount": "200",
            "method": "Cash",
            "reference": "DP-CLEAN-1",
            "note": "Driver settlement test",
            "date_posted": "2026-11-09T11:15",
        },
        follow_redirects=False,
    )
    client.post(
        "/accounts/transactions/new",
        data={
            "tx_mode": "pay",
            "pay_target": "company_transfer",
            "pay_from_account_id": str(ids["account_id"]),
            "transfer_to_account_id": str(ids["bank_id"]),
            "amount": "500",
            "method": "Cash",
            "note": "TR-CLEAN-1",
            "date_posted": "2026-11-09T11:30",
        },
        follow_redirects=False,
    )

    with app.app_context():
        tx_ids = [tx.id for tx in AccountTransaction.query.order_by(AccountTransaction.id.asc()).all()]
        assert len(tx_ids) == 5
        assert len(collect_cash_flow_rows("2026-11-09", "2026-11-09")) == 5

    # Delete every single transaction from Accounts ledger
    for tid in tx_ids:
        r = client.post(f"/accounts/transactions/{tid}/delete", follow_redirects=False)
        assert r.status_code in (302, 303)

    with app.app_context():
        assert AccountTransaction.query.count() == 0
        assert collect_cash_flow_rows("2026-11-09", "2026-11-09") == []
        assert collect_cash_flow_rows("2026-11-09", "2026-11-09", include_voided=True) == []
        assert db.session.get(Account, ids["account_id"]).balance_minor == 1_000_000
        assert db.session.get(Account, ids["bank_id"]).balance_minor == 500_000
        assert Payment.query.filter_by(is_void=False).count() == 0
        assert SupplierPayment.query.filter_by(is_void=False).count() == 0
        assert DeliveryPersonPayment.query.filter_by(is_void=False).count() == 0

    cf_page = client.get("/cash_flow?from_date=2026-11-09&to_date=2026-11-09&status=all&ft_preset=all")
    assert cf_page.status_code == 200
    cf_html = cf_page.get_data(as_text=True)
    assert "ALL TRANSACTIONS (0)" in cf_html
    for marker in ("GP-CLEAN-1", "CP-CLEAN-1", "SP-CLEAN-1", "DP-CLEAN-1", "TR-CLEAN-1"):
        assert marker not in cf_html

    ft_page = client.get("/financial_tracker?preset=all")
    assert ft_page.status_code == 200
    assert "ALL TRANSACTIONS (0)" in ft_page.get_data(as_text=True)


def test_deleting_supplier_grn_payment_from_supplier_payments_route_cleans_cash_flow(app, client, cf_setup):
    ids = cf_setup
    with app.app_context():
        sup = Supplier(name="Route Delete Supplier", is_active=True)
        mat = Material(name="Route Delete Sand", code="MAT-RDS", unit="Ton", total=0)
        db.session.add_all([sup, mat])
        db.session.commit()
        supplier_id = sup.id

    client.post(
        "/grn",
        data={
            "action": "add",
            "bill_no": "GRN-RD-1",
            "supplier_id": str(supplier_id),
            "date": "2026-11-09",
            "mat_name[]": "Route Delete Sand",
            "qty[]": "5",
            "price[]": "200",
        },
        follow_redirects=False,
    )
    with app.app_context():
        grn_id = GRN.query.one().id

    client.post(
        "/accounts/payments/suppliers/save",
        data={
            "supplier_id": str(supplier_id),
            "obligation_key": f"MATERIAL:{grn_id}",
            "amount": "500",
            "method": "Cash",
            "payment_account_id": str(ids["account_id"]),
            "date": "2026-11-09",
            "manual_bill_no": "SP-RD-1",
            "note": "Settling GRN from supplier payments",
        },
        follow_redirects=False,
    )
    with app.app_context():
        sp = SupplierPayment.query.one()
        sp_id = sp.id
        assert sp.source_type == "GRN_PAYMENT"
        assert AccountTransaction.query.count() == 1
        assert len(collect_cash_flow_rows("2026-11-09", "2026-11-09")) == 1

    # Delete directly from /accounts/payments/suppliers/<id>/delete
    resp = client.post(f"/accounts/payments/suppliers/{sp_id}/delete", follow_redirects=False)
    assert resp.status_code in (302, 303)

    with app.app_context():
        assert AccountTransaction.query.count() == 0
        assert collect_cash_flow_rows("2026-11-09", "2026-11-09") == []
        assert collect_cash_flow_rows("2026-11-09", "2026-11-09", include_voided=True) == []
        assert db.session.get(Account, ids["account_id"]).balance_minor == 1_000_000
