"""Regression tests for Daily Reconciliation state surviving a wipe.

Reported symptom: after "wipe" the Daily Cash & Bank Reconciliation board kept
showing the old figures ("hard-coded" numbers) even though every account had
been re-entered from scratch.

Two independent causes, both fixed here:

* ``cash_day_account_position`` / ``cash_day_lock`` were not part of any wipe
  path (granular target, full wipe, accounts-domain wipe), so typed counts and
  verified day locks survived the reset.
* those tables are keyed by the numeric account id and carry no FK.  SQLite
  (no ``AUTOINCREMENT``) reuses the highest id after a delete, so an account
  created after the wipe inherited the removed account's row.

The board now (a) purges rows that cannot belong to the current account set on
every render and (b) wipes them together with the financial accounts domain.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _login(client):
    return client.post("/login", data=dict(ADMIN), follow_redirects=True)


def _mk_account(db, Account, name, category="cash", opening=1000.0):
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


def _count_and_lock(day, account_id, counted):
    from app.services import cash_day_recon as recon

    recon.save_count(day, account_id, counted, actor="tester")
    recon.lock_day(day, actor="tester")


def _recon_rows():
    from models import CashDayAccountPosition, CashDayLock

    return CashDayAccountPosition.query.count(), CashDayLock.query.count()


def test_board_drops_rows_that_belong_to_a_deleted_account(client, app):
    """A count typed for an account that no longer exists must never render."""
    from models import db, Account

    with app.app_context():
        acc = _mk_account(db, Account, "OLD CASH", opening=1000.0)
        old_id = acc.id
        _count_and_lock(date(2026, 6, 1), old_id, 7777.0)
        assert _recon_rows() != (0, 0)

        # Simulate the pre-fix hard delete that left the board rows behind.
        db.session.delete(acc)
        db.session.commit()

        # SQLite reuses the freed id for the next account.
        fresh = _mk_account(db, Account, "NEW CASH", opening=250.0)
        assert fresh.id == old_id

    _login(client)
    rv = client.get("/daily_reconciliation?day=2026-06-01")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert "NEW CASH" in body
    assert "7,777" not in body and "7777" not in body
    assert "OPEN FOR RECONCILIATION" in body  # lock residue must not survive either

    with app.app_context():
        assert _recon_rows() == (0, 0)


def test_board_keeps_counts_for_the_account_they_were_typed_on(client, app):
    """Self-healing must not throw away legitimate counts."""
    from models import db, Account

    day = date(2026, 6, 2)
    with app.app_context():
        acc = _mk_account(db, Account, "LIVE CASH", opening=1000.0)
        _count_and_lock(day, acc.id, 1234.0)

    _login(client)
    rv = client.get(f"/daily_reconciliation?day={day.isoformat()}")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert "1,234" in body
    assert "VERIFIED &amp; LOCKED" in body or "LOCKED" in body

    with app.app_context():
        positions, locks = _recon_rows()
        assert positions == 1
        assert locks == 1


def test_account_hard_delete_clears_its_board_rows(client, app):
    from models import db, Account, CashDayAccountPosition

    with app.app_context():
        acc = _mk_account(db, Account, "TEMP CASH", opening=500.0)
        acc_id = acc.id
        _count_and_lock(date(2026, 6, 3), acc_id, 450.0)
        assert CashDayAccountPosition.query.filter_by(account_id=acc_id).count() == 1

    _login(client)
    resp = client.post(f"/accounts/{acc_id}/delete", follow_redirects=False)
    assert resp.status_code in (302, 303)

    with app.app_context():
        assert Account.query.filter_by(id=acc_id).first() is None
        assert CashDayAccountPosition.query.filter_by(account_id=acc_id).count() == 0


def test_accounts_wipe_clears_daily_reconciliation_state(client, app):
    """Financial-accounts reset must remove typed counts and day locks."""
    from models import db, Account, CashDayAccountPosition, CashDayLock

    with app.app_context():
        acc = _mk_account(db, Account, "WIPE CASH", opening=900.0)
        _count_and_lock(date(2026, 6, 4), acc.id, 875.0)
        assert CashDayAccountPosition.query.count() == 1
        assert CashDayLock.query.count() == 1

    _login(client)
    resp = client.post(
        "/delete_selected_data",
        data={
            "confirm_text": "DELETE ALL DATA",
            "hard_delete_override": "1",
            "delete_targets": ["accounts"],
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    with app.app_context():
        assert CashDayAccountPosition.query.count() == 0
        assert CashDayLock.query.count() == 0


def test_explicit_daily_reconciliation_target_wipes_board_state(client, app):
    from models import db, Account, CashDayAccountPosition, CashDayLock

    with app.app_context():
        acc = _mk_account(db, Account, "TARGET CASH", opening=700.0)
        _count_and_lock(date(2026, 6, 5), acc.id, 690.0)

    _login(client)
    resp = client.post(
        "/delete_selected_data",
        data={
            "confirm_text": "DELETE SELECTED",
            "delete_targets": ["daily_reconciliation"],
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    with app.app_context():
        assert CashDayAccountPosition.query.count() == 0
        assert CashDayLock.query.count() == 0
        # The account itself is untouched by this narrow target.
        assert Account.query.count() == 1


def test_accounts_domain_wipe_clears_board_state(client, app):
    """The admin accounts-domain wipe uses the domain registry."""
    from models import (
        db, Account, AccountReconciliation, AccountTransaction,
        CashDayAccountPosition, CashDayLock, CashFlowEntry, CashFlowEntryAudit,
    )
    from app.services.wipe import (
        accounts_domain_post_reset,
        execute_domain_wipe,
        verify_accounts_domain_wipe_integrity,
    )

    with app.app_context():
        acc = _mk_account(db, Account, "DOMAIN CASH", opening=800.0)
        _count_and_lock(date(2026, 6, 6), acc.id, 780.0)
        # Ledger + reconciliation chain + cash-flow rows: the domain wipe must
        # remove all of them without tripping the FK gates.
        rec = AccountReconciliation(
            account_id=acc.id, reconciliation_date=date(2026, 6, 6),
            expected_balance=800.0, actual_balance=780.0, difference=-20.0,
        )
        db.session.add(rec)
        db.session.flush()
        db.session.add(AccountTransaction(
            to_account_id=acc.id, amount=800.0, reconciliation_id=rec.id,
        ))
        entry = CashFlowEntry(direction="in", amount=100.0, account_id=acc.id)
        db.session.add(entry)
        db.session.flush()
        db.session.add(CashFlowEntryAudit(entry_id=entry.id, action="create"))
        db.session.commit()

        execute_domain_wipe("accounts_domain", db.session)
        accounts_domain_post_reset(db.session)
        db.session.commit()
        ok, report = verify_accounts_domain_wipe_integrity()
        assert ok, report
        assert CashDayAccountPosition.query.count() == 0
        assert CashDayLock.query.count() == 0


def test_preview_counts_board_rows_under_accounts(app):
    from models import db, Account
    from app.services.wipe import _wipe_preview_for_targets

    with app.app_context():
        acc = _mk_account(db, Account, "PREVIEW CASH", opening=100.0)
        _count_and_lock(date(2026, 6, 7), acc.id, 100.0)
        preview = _wipe_preview_for_targets(["accounts"])
        tables = set(preview["affected_tables"])
        assert "cash_day_account_position" in tables
        assert "cash_day_lock" in tables
        by_key = {row["key"]: row for row in preview["selected_datasets"]}
        assert by_key["accounts"]["estimated_rows"] >= 3  # account + position + lock

        daily = _wipe_preview_for_targets(["daily_reconciliation"])
        daily_tables = set(daily["affected_tables"])
        assert daily_tables == {
            "cash_day_account_position", "cash_day_lock",
            "cash_day_shift_close", "cash_day_shift_close_position",
        }


def test_ledger_wipe_rebuilds_cached_account_balances(client, app):
    """Deleting ledger rows must not leave the old balances on screen."""
    from models import db, Account, AccountTransaction

    with app.app_context():
        acc = _mk_account(db, Account, "LEDGER CASH", opening=1000.0)
        acc_id = acc.id
        db.session.add(AccountTransaction(to_account_id=acc_id, amount=500.0))
        db.session.commit()
        acc = db.session.get(Account, acc_id)
        acc.balance = 1500.0
        acc.balance_minor = 150000
        db.session.commit()

    _login(client)
    resp = client.post(
        "/delete_selected_data",
        data={
            "confirm_text": "DELETE SELECTED",
            "delete_targets": ["account_transactions"],
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    with app.app_context():
        assert AccountTransaction.query.count() == 0
        acc = db.session.get(Account, acc_id)
        assert float(acc.balance) == 1000.0
        assert int(acc.balance_minor) == 100000

    rv = client.get("/daily_reconciliation")
    assert rv.status_code == 200
    assert "1,000" in rv.get_data(as_text=True)


def test_startup_purges_stale_rows_left_in_an_existing_database(app_factory, tmp_path):
    """An already-corrupted database is repaired on the next boot."""
    from models import db, Account, CashDayAccountPosition, CashDayLock

    db_file = tmp_path / "stale_recon.db"
    app1 = app_factory(db_file)
    with app1.app_context():
        acc = _mk_account(db, Account, "OLD CASH", opening=100.0)
        old_id = acc.id
        _count_and_lock(date(2026, 6, 8), old_id, 7777.0)
        db.session.delete(acc)  # pre-fix hard delete: board rows left behind
        db.session.commit()
        fresh = _mk_account(db, Account, "NEW CASH", opening=200.0)
        assert fresh.id == old_id
        assert CashDayAccountPosition.query.count() == 1
        assert CashDayLock.query.count() == 1

    app2 = app_factory(db_file)  # simulate a restart of the same database
    with app2.app_context():
        assert CashDayAccountPosition.query.count() == 0
        assert CashDayLock.query.count() == 0
        assert Account.query.count() == 1
