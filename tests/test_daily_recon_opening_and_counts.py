"""Regression tests for the Daily Cash & Bank Reconciliation board.

Covers the three reported defects:

1. Opening balances that did not tie out with Accounts (accounts created before
   opening-balance metadata existed showed an opening of zero + movements).
2. The counted ("Actual Counted") cell could not be written: ``type=number``
   inputs silently discard thousands separators ("160,000"), so the browser
   blocked the submit and nothing was ever saved.
3. The Net Difference tile compared the counted total of *some* accounts with
   the expected closing of *all* accounts, producing a huge meaningless figure.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _login(client):
    return client.post("/login", data=dict(ADMIN), follow_redirects=True)


def _mk_account(db, Account, name, category, opening, **kwargs):
    acc = Account(
        name=name,
        type="CASH" if category == "cash" else "BANK",
        account_type="company",
        category=category,
        balance=kwargs.pop("balance", opening),
        opening_balance=kwargs.pop("opening_balance", opening),
        opening_balance_date=kwargs.pop("opening_balance_date", datetime(2026, 1, 1)),
        is_active=True,
        **kwargs,
    )
    db.session.add(acc)
    db.session.commit()
    return acc


def _record(direction, amount, account, day, dest=None, actor="Admin"):
    from types import SimpleNamespace
    from app.services.cash_flow_svc import save_manual_cash_flow_entry

    entry, created = save_manual_cash_flow_entry(
        direction=direction,
        amount=amount,
        account_id=account.id,
        destination_account_id=dest.id if dest else None,
        category_name=(
            "SALE" if direction == "in" else ("TRANSFER" if direction == "transfer" else "EXPENSE")
        ),
        party_name="TEST PARTY",
        party_type="person",
        date_posted=datetime(day.year, day.month, day.day, 12, 0),
        actor=SimpleNamespace(username=actor),
        create_missing=True,
    )
    assert created
    return entry


def test_opening_matches_ledger_for_legacy_accounts(app):
    """Accounts without opening metadata must infer their baseline, not zero."""
    from models import db, Account, AccountTransaction
    from app.services import cash_day_recon as recon
    from app.services.accounting import _apply_account_tx_effect
    from app.services.payments_crud import ledger_balance

    today = date.today()
    yesterday = today - timedelta(days=1)
    with app.app_context():
        legacy = _mk_account(
            db, Account, "Legacy Cash", "cash", 5000.0,
            opening_balance=None, opening_balance_date=None,
        )
        tx = AccountTransaction(
            to_account_id=legacy.id,
            amount=1000.0,
            amount_minor=100000,
            transaction_type="Receipt",
            date_posted=datetime.combine(today - timedelta(days=5), time.min),
        )
        db.session.add(tx)
        db.session.flush()
        _apply_account_tx_effect(tx)
        db.session.commit()

        expected_opening = ledger_balance(
            legacy.id, as_of=datetime.combine(yesterday, time.max)
        )
        assert expected_opening == 6000.0

        pos = [p for p in recon.account_positions_for_date(today)
               if p["account_id"] == legacy.id][0]
        assert pos["opening"] == expected_opening
        assert pos["expected_closing"] == 6000.0


def test_opening_respects_future_effective_date(app):
    """An account whose opening starts tomorrow has no balance today."""
    from models import db, Account
    from app.services import cash_day_recon as recon

    today = date.today()
    with app.app_context():
        acc = _mk_account(
            db, Account, "Tomorrow Cash", "cash", 9000.0,
            opening_balance_date=datetime.combine(today + timedelta(days=1), time.min),
        )
        pos = [p for p in recon.account_positions_for_date(today)
               if p["account_id"] == acc.id][0]
        assert pos["opening"] == 0.0
        assert pos["expected_closing"] == 0.0


def test_net_difference_only_compares_counted_accounts(app, client):
    from models import db, Account
    from app.services import cash_day_recon as recon

    today = date.today()
    with app.app_context():
        cash = _mk_account(db, Account, "Drawer Cash", "cash", 100000.0)
        _mk_account(db, Account, "Meezan Bank", "bank", 500000.0)
        _record("out", 4000, cash, today)
        db.session.commit()
        cash_id = cash.id

    _login(client)
    day = today.isoformat()
    rv = client.post(
        "/daily_reconciliation",
        data={"action": "save_count", "day": day, "account_id": cash_id, "counted": "95,000"},
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert rv.status_code == 200
    payload = rv.get_json()
    assert payload["ok"] is True
    # Cash: expected 96,000, counted 95,000 -> short 1,000.  The bank account is
    # not counted, so it must not drag the difference to -500,000.
    assert payload["expected_counted"] == 96000.0
    assert payload["total_counted"] == 95000.0
    assert payload["net_difference"] == -1000.0
    assert payload["counted_accounts"] == 1
    assert payload["accounts_total"] == 2

    with app.app_context():
        ctx = recon.account_positions_for_date(day)
    cash_pos = [p for p in ctx if p["account_id"] == cash_id][0]
    assert cash_pos["counted"] == 95000.0
    assert cash_pos["difference"] == -1000.0


def test_count_accepts_formatted_amounts(app, client):
    from models import db, Account

    today = date.today()
    with app.app_context():
        acc = _mk_account(db, Account, "Drawer Cash", "cash", 250000.0)
        acc_id = acc.id

    _login(client)
    day = today.isoformat()
    for typed, expected in (
        ("250,000", 250000.0),
        ("Rs. 249,999.50", 249999.5),
        (" 250 000 ", 250000.0),
    ):
        rv = client.post(
            "/daily_reconciliation",
            data={"action": "save_count", "day": day, "account_id": acc_id, "counted": typed},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        )
        assert rv.status_code == 200, rv.get_data(as_text=True)
        payload = rv.get_json()
        assert payload["ok"] is True
        cash = [p for p in payload["positions"] if p["account_id"] == acc_id][0]
        assert cash["counted"] == expected

    # A non-numeric value is rejected with an actionable message, not a vague
    # "Unable to save" while the row silently keeps the old value.
    rv = client.post(
        "/daily_reconciliation",
        data={"action": "save_count", "day": day, "account_id": acc_id, "counted": "abc"},
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert rv.status_code == 400
    assert "must be a number" in rv.get_json()["error"]


def test_count_cell_renders_as_text_input(app, client):
    from models import db, Account

    with app.app_context():
        _mk_account(db, Account, "Drawer Cash", "cash", 250000.0)

    _login(client)
    body = client.get("/daily_reconciliation").get_data(as_text=True)
    assert 'name="counted"' in body
    # A number input discards "160,000" and then blocks the submit.
    assert 'type="number"' not in body.split('name="counted"')[0].rsplit("<input", 1)[-1]
    assert 'inputmode="decimal"' in body


def test_locked_day_rejects_count_changes(app, client):
    from models import db, Account

    today = date.today()
    with app.app_context():
        acc = _mk_account(db, Account, "Drawer Cash", "cash", 10000.0)
        acc_id = acc.id

    _login(client)
    day = today.isoformat()
    rv = client.post("/daily_reconciliation", data={"action": "lock", "day": day},
                     follow_redirects=True)
    assert rv.status_code == 200

    rv = client.post(
        "/daily_reconciliation",
        data={"action": "save_count", "day": day, "account_id": acc_id, "counted": "9000"},
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert rv.status_code == 400
    payload = rv.get_json()
    assert payload["ok"] is False
    assert "locked" in payload["error"].lower()

    rv = client.post(
        "/daily_reconciliation",
        data={"action": "clear_count", "day": day, "account_id": acc_id},
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert rv.status_code == 400

    client.post("/daily_reconciliation", data={"action": "unlock", "day": day},
                follow_redirects=True)
