from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _login(client):
    return client.post("/login", data=dict(ADMIN), follow_redirects=True)


def _account(db, Account, name, category="cash", opening=1000):
    account = Account(
        name=name,
        type="CASH" if category == "cash" else "BANK",
        account_type="company",
        category=category,
        balance=opening,
        opening_balance=opening,
        opening_balance_date=datetime(2026, 1, 1),
        is_active=True,
    )
    db.session.add(account)
    db.session.commit()
    return account


def test_shift_close_requires_all_accounts_counted_and_records_immutable_snapshot(app, client):
    from models import db, Account, CashDayShiftClose, CashDayShiftClosePosition

    day = date.today().isoformat()
    with app.app_context():
        cash = _account(db, Account, "Main Cash", opening=1000)
        bank = _account(db, Account, "Main Bank", category="bank", opening=5000)
        cash_id, bank_id = cash.id, bank.id

    _login(client)
    headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
    missing = client.post("/daily_reconciliation", data={
        "action": "close_shift", "day": day, "shift_name": "Morning",
    }, headers=headers)
    assert missing.status_code == 400
    assert "Count every account" in missing.get_json()["error"]

    for account_id, value in ((cash_id, "990"), (bank_id, "5,000")):
        saved = client.post("/daily_reconciliation", data={
            "action": "save_count", "day": day,
            "account_id": account_id, "counted": value,
        }, headers=headers)
        assert saved.status_code == 200

    close_response = client.post("/daily_reconciliation", data={
        "action": "close_shift", "day": day, "shift_name": "Morning",
        "shift_note": "Drawer handed to evening cashier",
    }, follow_redirects=True)
    assert close_response.status_code == 200
    assert "day remains open" in close_response.get_data(as_text=True).lower()

    with app.app_context():
        close = CashDayShiftClose.query.filter_by(close_date=date.today()).one()
        assert close.shift_name == "Morning"
        assert close.closed_by == "Admin"
        assert close.total_expected == 6000
        assert close.total_counted == 5990
        assert close.difference == -10
        saved_positions = {
            p.account_id: (p.expected_closing, p.counted, p.difference)
            for p in CashDayShiftClosePosition.query.filter_by(shift_close_id=close.id).all()
        }
        assert saved_positions[cash_id] == (1000, 990, -10)
        assert saved_positions[bank_id] == (5000, 5000, 0)

    # A subsequent shift can update today's counts, but the previous checkpoint
    # remains an immutable record of the first handover.
    changed = client.post("/daily_reconciliation", data={
        "action": "save_count", "day": day,
        "account_id": cash_id, "counted": "975",
    }, headers=headers)
    assert changed.status_code == 200
    with app.app_context():
        close = CashDayShiftClose.query.filter_by(close_date=date.today()).one()
        assert close.total_counted == 5990
        assert {p.account_id: p.counted for p in close.positions}[cash_id] == 990

    # Final day lock still works as the separate, next-day carry-forward action.
    locked = client.post("/daily_reconciliation", data={"action": "lock", "day": day}, follow_redirects=True)
    assert locked.status_code == 200
    assert "final day close" in locked.get_data(as_text=True).lower()


def test_later_shift_opens_from_previous_count_and_only_includes_new_movements(app):
    from models import db, Account, AccountTransaction, CashDayShiftClose
    from app.services import cash_day_recon as recon

    day = date.today()
    with app.app_context():
        account = _account(db, Account, "Main Cash", opening=1000)
        account.created_at = datetime.combine(day, datetime.min.time()).replace(hour=8)
        db.session.commit()
        account_id = account.id
        db.session.add(AccountTransaction(
            to_account_id=account_id, amount=100, amount_minor=10000,
            transaction_type="Receipt", date_posted=datetime.combine(day, datetime.min.time()).replace(hour=10),
        ))
        db.session.commit()

        recon.save_count(day, account_id, "1090")
        with patch("app.services.cash_day_recon.pk_now", return_value=datetime.combine(day, datetime.min.time()).replace(hour=11)):
            morning = recon.close_shift(day, "Morning", actor="Cashier A")
        assert morning.positions[0].opening == 1000
        assert morning.positions[0].amount_in == 100
        assert morning.positions[0].expected_closing == 1100
        assert morning.positions[0].difference == -10

        db.session.add(AccountTransaction(
            to_account_id=account_id, amount=50, amount_minor=5000,
            transaction_type="Receipt", date_posted=datetime.combine(day, datetime.min.time()).replace(hour=12),
        ))
        db.session.commit()
        recon.save_count(day, account_id, "1135")
        with patch("app.services.cash_day_recon.pk_now", return_value=datetime.combine(day, datetime.min.time()).replace(hour=13)):
            evening = recon.close_shift(day, "Evening", actor="Cashier B")

        assert evening.positions[0].opening == 1090
        assert evening.positions[0].amount_in == 50
        assert evening.positions[0].expected_closing == 1140
        assert evening.positions[0].counted == 1135
        assert evening.positions[0].difference == -5
        assert CashDayShiftClose.query.filter_by(close_date=day).count() == 2


def test_shift_close_rejects_day_locked_and_is_available_in_cash_flow_tab(app, client):
    from models import db, Account, CashDayShiftClose

    with app.app_context():
        account = _account(db, Account, "Main Cash", opening=1000)
        account_id = account.id

    _login(client)
    day = date.today().isoformat()
    assert client.post("/daily_reconciliation", data={
        "action": "save_count", "day": day, "account_id": account_id, "counted": "1000",
    }).status_code in (200, 302)

    standalone = client.get(f"/daily_reconciliation?day={day}")
    assert standalone.status_code == 200
    assert "SHIFT CLOSE &amp; HANDOVER" in standalone.get_data(as_text=True)
    embedded = client.get(f"/cash_flow?recon_day={day}")
    assert embedded.status_code == 200
    assert "Record Shift Close" in embedded.get_data(as_text=True)

    client.post("/daily_reconciliation", data={"action": "lock", "day": day}, follow_redirects=True)
    locked_close = client.post("/daily_reconciliation", data={
        "action": "close_shift", "day": day, "shift_name": "Late",
    }, headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
    assert locked_close.status_code == 400
    assert "day-locked" in locked_close.get_json()["error"]
    with app.app_context():
        assert CashDayShiftClose.query.filter_by(close_date=date.today()).count() == 0
