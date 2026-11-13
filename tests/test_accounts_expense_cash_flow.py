"""Accounts-only expense entry using Cash Flow-owned classification masters."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from models import (
    Account,
    AccountTransaction,
    CashDayLock,
    CashFlowCategory,
    CashFlowEntry,
    CashFlowEntryAudit,
    CashFlowParty,
    CashFlowSubcategory,
    db,
)


def _login(client):
    response = client.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)


@pytest.fixture()
def expense_setup(app, client):
    _login(client)
    with app.app_context():
        account = Account(
            name="Expense Test Cash",
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
        db.session.add_all([account, category])
        db.session.flush()
        subcategory = CashFlowSubcategory(
            category_id=category.id, name="Rent", is_active=True
        )
        party = CashFlowParty(name="Landlord", party_type="person", is_active=True)
        db.session.add_all([subcategory, party])
        db.session.commit()
        return {
            "account_id": account.id,
            "category_id": category.id,
            "subcategory_id": subcategory.id,
            "party_id": party.id,
        }


def _expense_payload(ids, **overrides):
    payload = {
        "tx_mode": "pay",
        "pay_target": "other_expense",
        "pay_from_account_id": str(ids["account_id"]),
        "amount": "250.25",
        "method": "Cash",
        "target_label": "November office rent",
        "expense_category_id": str(ids["category_id"]),
        "expense_subcategory_id": str(ids["subcategory_id"]),
        "expense_party_id": str(ids["party_id"]),
        "reference": "EXP-001",
        "note": "Rent voucher",
        "date_posted": "2026-11-09T10:30",
        "idempotency_key": "accounts-expense-test-1",
    }
    payload.update(overrides)
    return payload


def test_accounts_dashboard_reads_cash_flow_masters(client, expense_setup):
    response = client.get("/accounts/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'name="expense_category_id"' in html
    assert 'name="expense_subcategory_id"' in html
    assert 'name="expense_party_id"' in html
    assert "Office Expenses" in html
    assert "Rent" in html
    assert "Landlord" in html
    assert "Category and party/recipient are required" in html


@pytest.mark.parametrize("missing_field", ["expense_category_id", "expense_party_id"])
def test_expense_category_and_party_are_required_on_server(
    app, client, expense_setup, missing_field
):
    payload = _expense_payload(expense_setup, **{missing_field: ""})
    response = client.post(
        "/accounts/transactions/new", data=payload, follow_redirects=True
    )
    assert response.status_code == 200
    with app.app_context():
        assert AccountTransaction.query.count() == 0
        assert CashFlowEntry.query.count() == 0
        account = db.session.get(Account, expense_setup["account_id"])
        assert account.balance_minor == 1_000_000
    message = response.get_data(as_text=True)
    if missing_field == "expense_category_id":
        assert "Category is required" in message
    else:
        assert "Party / recipient is required" in message


def test_expense_rejects_inactive_or_unknown_masters(app, client, expense_setup):
    with app.app_context():
        inactive = CashFlowParty(name="Inactive party", is_active=False)
        db.session.add(inactive)
        db.session.commit()
        inactive_id = inactive.id

    for party_id in (str(inactive_id), "999999"):
        response = client.post(
            "/accounts/transactions/new",
            data=_expense_payload(expense_setup, expense_party_id=party_id),
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Party / recipient is required" in response.get_data(as_text=True)

    with app.app_context():
        assert AccountTransaction.query.count() == 0
        assert CashFlowEntry.query.count() == 0
        assert CashFlowParty.query.filter_by(name="Inactive party").count() == 1


def test_accounts_expense_posts_once_with_classification_and_accounts_origin(
    app, client, expense_setup
):
    payload = _expense_payload(expense_setup)
    for _ in range(2):
        response = client.post(
            "/accounts/transactions/new", data=payload, follow_redirects=False
        )
        assert response.status_code in (302, 303)

    with app.app_context():
        transactions = AccountTransaction.query.all()
        entries = CashFlowEntry.query.all()
        assert len(transactions) == 1
        assert len(entries) == 1
        tx = transactions[0]
        entry = entries[0]
        account = db.session.get(Account, expense_setup["account_id"])
        assert tx.transaction_type == "Expense"
        assert tx.amount_minor == 25_025
        assert tx.from_account_id == account.id
        assert tx.to_account_id is None
        assert entry.account_tx_id == tx.id
        assert entry.direction == "out"
        assert entry.source_type == "ACCOUNT_EXPENSE"
        assert entry.category_id == expense_setup["category_id"]
        assert entry.subcategory_id == expense_setup["subcategory_id"]
        assert entry.party_id == expense_setup["party_id"]
        assert entry.reference == "EXP-001"
        assert entry.idempotency_key == "accounts-expense-test-1"
        assert account.balance_minor == 1_000_000 - 25_025
        assert account.balance == pytest.approx(9_749.75)

        with patch('blueprints.accounts.kpis.pk_today', return_value=tx.date_posted.date()):
            drilldown = client.get('/accounts/api/kpi/expenditures')
            assert drilldown.status_code == 200
            rows = drilldown.get_json()
            assert len(rows) == 1
            assert rows[0]['category'] == 'Office Expenses'
            assert rows[0]['amount'] == pytest.approx(250.25)
            assert 'Recipient: Landlord' in rows[0]['note']

        report = client.get('/accounts/expenditures?from=2026-11-09&to=2026-11-09')
        assert report.status_code == 200
        assert 'Office Expenses' in report.get_data(as_text=True)
        filtered = client.get(
            '/accounts/expenditures?from=2026-11-09&to=2026-11-09&category=Office%20Expenses'
        )
        assert filtered.status_code == 200
        assert 'Office Expenses' in filtered.get_data(as_text=True)
        assert 'Recipient: Landlord' in filtered.get_data(as_text=True)
        searched = client.get(
            '/accounts/expenditures?from=2026-11-09&to=2026-11-09&q=Landlord'
        )
        assert searched.status_code == 200
        assert 'Office Expenses' in searched.get_data(as_text=True)
        assert 'Recipient: Landlord' in searched.get_data(as_text=True)

        from app.services.cash_flow_svc import collect_cash_flow_rows

        rows = collect_cash_flow_rows("2026-11-09", "2026-11-09")
        assert len(rows) == 1
        assert rows[0]["category"] == "Office Expenses"
        assert rows[0]["subcategory"] == "Rent"
        assert rows[0]["party_name"] == "Landlord"
        assert rows[0]["section_label"] == "Accounts"
        assert rows[0]["origin_label"] == "Recorded on Accounts"

        # P&L consumes the one account movement and uses Cash Flow only for
        # metadata; the linked entry never creates a second expense.
        from app.services.profit_loss import _Projection

        projection = _Projection().build()
        expense_rows = [
            row for row in projection.rows
            if row.get("document_type") == "AccountTransaction"
            and row.get("document_id") == tx.id
        ]
        assert len(expense_rows) == 1
        assert expense_rows[0]["entry_type"] == "expense"
        assert expense_rows[0]["category"] == "Office Expenses"
        assert expense_rows[0]["subcategory"] == "Rent"
        assert expense_rows[0]["expense"] == pytest.approx(250.25)


def test_accounts_edit_and_void_update_both_sides_together(app, client, expense_setup):
    client.post(
        "/accounts/transactions/new",
        data=_expense_payload(expense_setup),
        follow_redirects=False,
    )
    with app.app_context():
        tx = AccountTransaction.query.one()
        tx_id = tx.id

    response = client.post(
        f"/accounts/transactions/{tx_id}/edit",
        data={
            "amount": "300",
            "description": "Updated office rent",
            "note": "Corrected voucher",
            "date_posted": "2026-11-09T11:00",
            "from_account_id": str(expense_setup["account_id"]),
            "edit_reason": "Amount correction",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    with app.app_context():
        tx = db.session.get(AccountTransaction, tx_id)
        entry = CashFlowEntry.query.one()
        account = db.session.get(Account, expense_setup["account_id"])
        assert tx.amount_minor == 30_000
        assert tx.description == "Updated office rent"
        assert entry.amount_minor == 30_000
        assert entry.description == "Updated office rent"
        assert entry.revision == 2
        assert account.balance_minor == 970_000

    response = client.post(
        f"/accounts/transactions/{tx_id}/delete",
        data={"void_reason": "Duplicate voucher"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    with app.app_context():
        from models import AccountingAuditLog
        from app.services.cash_flow_svc import collect_cash_flow_rows

        assert db.session.get(AccountTransaction, tx_id) is None
        assert CashFlowEntry.query.count() == 0
        account = db.session.get(Account, expense_setup["account_id"])
        assert account.balance_minor == 1_000_000
        assert collect_cash_flow_rows("2026-11-09", "2026-11-09") == []
        assert collect_cash_flow_rows("2026-11-09", "2026-11-09", include_voided=True) == []
        assert AccountingAuditLog.query.filter_by(
            entity_type="CashFlowEntry", action="Delete"
        ).count() == 1


def test_expense_cannot_be_posted_into_locked_day(app, client, expense_setup):
    with app.app_context():
        db.session.add(CashDayLock(
            lock_date=datetime(2026, 11, 9).date(),
            total_expected=0,
            total_counted=0,
            difference=0,
            locked_by="Admin",
        ))
        db.session.commit()

    response = client.post(
        "/accounts/transactions/new",
        data=_expense_payload(expense_setup),
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "finalised in the daily reconciliation" in response.get_data(as_text=True)
    with app.app_context():
        assert AccountTransaction.query.count() == 0
        assert CashFlowEntry.query.count() == 0
        assert db.session.get(Account, expense_setup["account_id"]).balance_minor == 1_000_000
