"""Reproduction for: deleted/void entries still showing in Cash Flow transactions.

Covers both deletion entry points:
- voiding a manual Cash Flow entry from the /cash_flow screen
- deleting an Account Transaction linked to a Cash Flow entry from /accounts
"""
from __future__ import annotations

from datetime import datetime

import pytest

from models import (
    Account,
    AccountTransaction,
    CashFlowCategory,
    CashFlowEntry,
    CashFlowParty,
    db,
)
from app.services.cash_flow_svc import collect_cash_flow_rows


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
        category = CashFlowCategory(name="Office Expenses", direction="out", is_active=True)
        party = CashFlowParty(name="Landlord", party_type="person", is_active=True)
        db.session.add_all([account, category, party])
        db.session.commit()
        return {
            "account_id": account.id,
            "category_id": category.id,
            "party_id": party.id,
        }


def _active_cf_rows(app, from_date="2026-11-09", to_date="2026-11-09"):
    # Mirrors the /cash_flow page default: only active (non-voided) rows.
    with app.app_context():
        return collect_cash_flow_rows(from_date, to_date, include_voided=False)


def test_manual_cash_flow_entry_void_disappears_from_cash_flow(app, client, cf_setup):
    ids = cf_setup
    # Create a manual cash flow entry (spent) via the /cash_flow screen.
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
        assert entry.account_tx_id == tx.id
        assert entry.is_void is False
        assert tx.is_void is False

    rows_before = _active_cf_rows(app)
    assert len(rows_before) == 1, f"expected 1 active row before void, got {len(rows_before)}"

    # Void it from the Cash Flow screen.
    response = client.post(
        "/cash_flow",
        data={
            "action": "void_entry",
            "entry_id": str(entry.id),
            "void_reason": "Duplicate entry",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with app.app_context():
        entry = db.session.get(CashFlowEntry, entry.id)
        tx = db.session.get(AccountTransaction, tx.id)
        account = db.session.get(Account, ids["account_id"])
        assert entry.is_void is True, "CashFlowEntry should be voided"
        assert tx.is_void is True, "linked AccountTransaction should be voided"
        # Financial effect must be reversed: balance back to opening 10,000.
        assert account.balance_minor == 1_000_000, f"balance not restored: {account.balance_minor}"

    rows_after = _active_cf_rows(app)
    assert len(rows_after) == 0, f"voided entry still shows in cash flow: {rows_after}"


def test_account_expense_delete_removes_from_cash_flow(app, client, cf_setup):
    ids = cf_setup
    # Create an account expense (posts both AccountTransaction + CashFlowEntry).
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
        assert entry.is_void is False

    rows_before = _active_cf_rows(app)
    assert len(rows_before) == 1, f"expected 1 active row before delete, got {len(rows_before)}"

    # Delete from the Account ledger.
    response = client.post(
        f"/accounts/transactions/{tx_id}/delete",
        data={"void_reason": "Duplicate voucher"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with app.app_context():
        tx = db.session.get(AccountTransaction, tx_id)
        entry = db.session.get(CashFlowEntry, entry_id)
        account = db.session.get(Account, ids["account_id"])
        assert tx.is_void is True
        assert entry.is_void is True
        assert account.balance_minor == 1_000_000, f"balance not restored: {account.balance_minor}"

    rows_after = _active_cf_rows(app)
    assert len(rows_after) == 0, f"deleted entry still shows in cash flow: {rows_after}"
