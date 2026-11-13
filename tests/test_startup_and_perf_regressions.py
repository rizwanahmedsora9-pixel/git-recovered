"""Regression guards for the startup / reporting performance work.

Every assertion here pins a behaviour that a "make it faster" change could
silently break:

1. An account whose opening balance already funds it must never be reconciled
   into an invented ``Adjustment``.
2. Creating the app must not leave background threads behind in a WSGI process.
3. A second ``create_app()`` (a pre-fork worker, a test session) must reuse the
   warm caches instead of paying the cold boot again.
4. ``save_count`` posts the counted difference exactly once, whatever the
   amount (shortage, excess, unchanged, cleared).
5. The performance indexes are created idempotently by both the runtime helper
   and migration ``0013``.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime

import pytest

from app.services import recon_counter
from app.services import cash_day_recon as recon
from app.services.payments_crud import ledger_balance
from models import Account, Settings, db
from utils.money import to_minor
from utils.reconciliation import reconcile_account_balances


def _login(client):
    # The production app enforces session-bound CSRF, exactly like a browser:
    # seed a token into the session before the form post.
    with client.session_transaction() as session:
        session["_csrf_token"] = "test-csrf-token"
    resp = client.post(
        "/login",
        data={
            "username": "Admin",
            "password": "Admin@fbm12345",
            "_csrf_token": "test-csrf-token",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


def _cash_account(name, opening):
    account = Account(
        name=name,
        type="company",
        account_type="company",
        category="cash",
        source_category="Company",
        balance=float(opening),
        balance_minor=to_minor(opening),
        opening_balance=float(opening),
        opening_balance_minor=to_minor(opening),
        opening_balance_date=datetime(2026, 1, 1),
        is_active=True,
    )
    db.session.add(account)
    db.session.flush()
    return account


# ---------------------------------------------------------------------------
# 1. Opening-funded accounts are never "reconciled" into an adjustment
# ---------------------------------------------------------------------------
def test_opening_funded_account_never_gets_an_adjustment(app):
    with app.app_context():
        db.session.add(Settings())
        cash = _cash_account("Opening Funded Cash", 45_000)
        db.session.commit()
        account_id = cash.id

        result = reconcile_account_balances()
        db.session.commit()

        assert result["accounts_adjusted"] == 0
        assert reconcile_account_balances()["accounts_adjusted"] == 0

        rows = db.session.execute(
            db.text(
                "select transaction_type, note from account_transaction "
                "where to_account_id = :acc or from_account_id = :acc"
            ),
            {"acc": account_id},
        ).all()
        assert rows == []
        assert db.session.get(Account, account_id).balance_minor == to_minor(45_000)


# ---------------------------------------------------------------------------
# 2. No background threads are started in a WSGI-style process
# ---------------------------------------------------------------------------
def test_create_app_leaves_no_background_threads(app_factory):
    before = {t.ident for t in threading.enumerate()}
    app = app_factory()
    client = app.test_client()
    _login(client)
    assert client.get("/").status_code == 200
    after = [t for t in threading.enumerate() if t.ident not in before]
    assert after == [], f"worker threads leaked: {[t.name for t in after]}"


# ---------------------------------------------------------------------------
# 3. A warm second boot is fast (caches are reused, not rebuilt)
# ---------------------------------------------------------------------------
def test_second_create_app_reuses_the_warm_caches(app_factory):
    import time

    app_factory()
    started = time.perf_counter()
    app_factory()
    second = time.perf_counter() - started
    assert second < 3.0, f"second create_app() took {second:.2f}s"


# ---------------------------------------------------------------------------
# 4. save_count is idempotent per (day, account) and exact in paisa
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "counted, expected_balance, expected_counter",
    [
        ("900", 900.0, 100.0),        # shortage posts once
        ("1000", 1000.0, 0.0),        # exact match posts nothing
        ("1250.55", 1250.55, -250.55),  # excess, paisa precision
        ("0", 0.0, 1000.0),           # whole drawer counted empty
    ],
)
def test_save_count_posts_the_difference_exactly_once(app, counted, expected_balance, expected_counter):
    day = date(2026, 11, 10)
    with app.app_context():
        db.session.add(Settings())
        cash = _cash_account(f"Drawer {counted}", 1000)
        db.session.commit()
        account_id = cash.id

        recon.save_count(day, account_id, counted, actor="Admin")

        account = db.session.get(Account, account_id)
        assert account.balance_minor == to_minor(expected_balance)
        assert account.balance == pytest.approx(expected_balance, abs=1e-9)
        assert ledger_balance(account_id) == pytest.approx(expected_balance, abs=1e-9)
        assert recon_counter.counter_balance_total() == pytest.approx(expected_counter, abs=1e-9)

        # Re-saving the same count must not post a second time.
        recon.save_count(day, account_id, counted, actor="Admin")
        assert db.session.get(Account, account_id).balance_minor == to_minor(expected_balance)
        assert ledger_balance(account_id) == pytest.approx(expected_balance, abs=1e-9)
        assert recon_counter.counter_invariants() == []


# ---------------------------------------------------------------------------
# 5. Performance indexes are additive and idempotent
# ---------------------------------------------------------------------------
def test_performance_indexes_are_idempotent(app):
    from pathlib import Path

    from app.services.schema import _PERFORMANCE_INDEXES, _ensure_performance_indexes

    names = [name for name, _ddl in _PERFORMANCE_INDEXES]
    assert len(names) == len(set(names)), "duplicate index names in _PERFORMANCE_INDEXES"

    db_path = app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")

    def existing_indexes():
        raw = sqlite3.connect(db_path)
        try:
            return {row[0] for row in raw.execute(
                "select name from sqlite_master where type='index'")}
        finally:
            raw.close()

    with app.app_context():
        _ensure_performance_indexes()
        first = existing_indexes()
        _ensure_performance_indexes()
        assert existing_indexes() == first
    assert set(names) <= first

    # The shipped migration says the same thing; running it twice on a database
    # that already has the indexes must be a no-op, not an error.
    sql_file = (
        Path(__file__).resolve().parents[1]
        / "app" / "migrations" / "0013_add_missing_indexes.sql"
    )
    ddl = sql_file.read_text(encoding="utf-8")
    raw = sqlite3.connect(db_path)
    try:
        with raw:
            raw.executescript(ddl)
            raw.executescript(ddl)
    finally:
        raw.close()
    assert set(names) <= existing_indexes()
