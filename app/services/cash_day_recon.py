"""Daily Cash & Bank Reconciliation service.

Implements the per-account "Account Positions" board shown on the reference
layout (references-images/): for a financial day each company money account
shows OPENING / IN / OUT / TRANSFER IN / TRANSFER OUT / EXPECTED CLOSING plus
the user's ACTUAL COUNTED figure and the DIFFERENCE.

Opening balances are ledger-derived: an unlocked day starts from the same
figure ``ledger_balance()`` reports in Accounts (explicit opening baseline, or
the inferred baseline for accounts created before opening metadata existed),
plus every active movement up to the end of the previous day.

Locking a day freezes the per-account counted figures; the locked counted
closing for an account becomes that account's opening on the next day, which
is exactly the "locked amount shows as next day opening balances" behaviour.

Counted amounts are typed by hand, so parsing lives here (``parse_counted_amount``)
and accepts "160,000"/"Rs. 160000.50"; locked days refuse count changes until
they are unlocked.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta

from sqlalchemy import case, func, or_

from models import (
    db, Account, AccountTransaction, CashDayLock, CashDayAccountPosition,
)
from utils.money import from_minor, to_minor
from app.services.time_money import _money_round, pk_now, pk_today
from app.services.cash_flow_svc import (
    CF_DIR_IN, CF_DIR_OUT, CF_DIR_TRANSFER,
    collect_cash_flow_rows, _cf_company_accounts, _cf_account_label,
)

def _to_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _naive_dt(value):
    """Normalise a stored timestamp to a naive ``datetime`` (PKT wall clock)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip())
        except (TypeError, ValueError):
            return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _position_written_at(pos):
    """When the board row was last written (lock stamp wins over edit stamp)."""
    if pos is None:
        return None
    return (
        _naive_dt(getattr(pos, "locked_at", None))
        or _naive_dt(getattr(pos, "updated_at", None))
    )


def position_belongs_to_account(pos, account) -> bool:
    """Whether a stored board row was really written for *account*.

    Daily reconciliation rows are keyed by the numeric ``account_id`` only.
    SQLite (no ``AUTOINCREMENT`` on these tables) hands the highest rowid back
    out after a delete, so an account created after a wipe can land on the id
    of a removed one and inherit its counts and locked openings.  The board then
    shows figures nobody typed for the new account — they look hard-coded.

    A row can only belong to the account currently occupying that id when the
    row was written after the account record itself was created.  Rows with no
    usable timestamps are left alone (nothing proves they are stale).
    """
    if pos is None or account is None:
        return False
    created = _naive_dt(getattr(account, "created_at", None))
    if created is None:
        return True
    written = _position_written_at(pos)
    if written is None:
        return True
    return written >= created


def _refresh_lock_totals(lock) -> None:
    """Recompute a day lock's totals from its surviving account positions."""
    rows = CashDayAccountPosition.query.filter_by(position_date=lock.lock_date).all()
    expected = sum(float(r.expected_closing or 0) for r in rows)
    counted = sum(
        float(r.counted if r.counted is not None else (r.expected_closing or 0))
        for r in rows
    )
    lock.total_expected = _money_round(expected)
    lock.total_counted = _money_round(counted)
    lock.difference = _money_round(counted - expected)


def purge_stale_reconciliation_state(*, commit=True):
    """Delete Daily Reconciliation rows that cannot belong to today's accounts.

    This is the self-healing counterpart to a wipe: it removes

    * positions whose account no longer exists, and
    * positions written before the account now holding that id was created
      (leftovers from a deleted account whose id was reused),
    * day locks that have no surviving position at all, and
    * stale day totals on locks that lost some of their accounts.

    It is safe to call on every board render — nothing is written unless
    something stale is found.  Returns ``(positions_removed, locks_removed)``.
    """
    accounts = {a.id: a for a in Account.query.all()}
    positions_removed = 0
    touched_dates = set()

    for pos in CashDayAccountPosition.query.all():
        account = accounts.get(pos.account_id)
        if position_belongs_to_account(pos, account):
            continue
        touched_dates.add(_to_date(pos.position_date))
        db.session.delete(pos)
        positions_removed += 1

    if positions_removed:
        db.session.flush()

    locks_removed = 0
    existing_locks = CashDayLock.query.all()
    if existing_locks:
        live_dates = {
            _to_date(row[0])
            for row in db.session.query(
                CashDayAccountPosition.position_date
            ).distinct().all()
        }
        # A day lock only means something while it still has per-account
        # positions to freeze; otherwise it is residue from a previous dataset
        # (and would keep showing a verified/locked banner with old totals).
        for lock in existing_locks:
            lock_day = _to_date(lock.lock_date)
            if lock_day not in live_dates:
                db.session.delete(lock)
                locks_removed += 1
            elif lock_day in touched_dates:
                _refresh_lock_totals(lock)

    if positions_removed or locks_removed:
        if commit:
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
    return positions_removed, locks_removed


def clear_account_reconciliation_state(account_id, *, commit=True) -> int:
    """Remove every Daily Reconciliation row belonging to one account id.

    Called when an account is deleted so the id it frees up cannot hand its
    old counts/locks to whichever account is created next.
    """
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        return 0
    dates = {
        _to_date(row[0])
        for row in db.session.query(CashDayAccountPosition.position_date)
        .filter(CashDayAccountPosition.account_id == account_id)
        .distinct()
        .all()
    }
    removed = int(
        CashDayAccountPosition.query.filter_by(account_id=account_id).delete(
            synchronize_session=False
        )
        or 0
    )
    if removed:
        live_dates = {
            _to_date(row[0])
            for row in db.session.query(
                CashDayAccountPosition.position_date
            ).distinct().all()
        }
        for lock in CashDayLock.query.filter(
            CashDayLock.lock_date.in_(sorted(dates))
        ).all():
            lock_day = _to_date(lock.lock_date)
            if lock_day in live_dates:
                _refresh_lock_totals(lock)
            else:
                db.session.delete(lock)
    if removed and commit:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
    return removed


def _opening_effective_day(account):
    ob_date = getattr(account, "opening_balance_date", None)
    if ob_date is None:
        return None
    return ob_date.date() if hasattr(ob_date, "date") else ob_date


def _movement_minor(account_id, *, after=None, through=None):
    """(incoming, outgoing) minor units for one account — voided rows excluded."""
    amount_expr = func.coalesce(
        AccountTransaction.amount_minor,
        func.round(AccountTransaction.amount * 100.0),
    )
    incoming_expr = func.coalesce(func.sum(case(
        (AccountTransaction.to_account_id == account_id, amount_expr), else_=0
    )), 0)
    outgoing_expr = func.coalesce(func.sum(case(
        (AccountTransaction.from_account_id == account_id, amount_expr), else_=0
    )), 0)
    q = db.session.query(incoming_expr, outgoing_expr).filter(
        AccountTransaction.is_void == False,  # noqa: E712
        or_(
            AccountTransaction.from_account_id == account_id,
            AccountTransaction.to_account_id == account_id,
        ),
    )
    if after is not None:
        q = q.filter(AccountTransaction.date_posted > after)
    if through is not None:
        q = q.filter(AccountTransaction.date_posted <= through)
    incoming, outgoing = q.one()
    return int(round(float(incoming or 0))), int(round(float(outgoing or 0)))


def _account_baseline_minor(account) -> int:
    """Explicit opening baseline, or the inferred one for legacy accounts.

    Mirrors ``payments_crud.ledger_balance`` so the Daily Reconciliation board
    and the Accounts module always show the same starting figure.  Accounts
    created before opening-balance metadata existed store no baseline at all;
    for those the opening is inferred from the cached balance minus every
    ledger movement, instead of silently falling back to zero.
    """
    if getattr(account, "opening_balance_minor", None) is not None:
        return int(account.opening_balance_minor)
    if getattr(account, "opening_balance", None) is not None:
        return int(to_minor(account.opening_balance))
    incoming_all, outgoing_all = _movement_minor(account.id)
    return int(to_minor(account.balance or 0)) - incoming_all + outgoing_all


def ledger_opening_minor(account, through=None) -> int:
    """Opening baseline plus every active movement through ``through``."""
    opening = _account_baseline_minor(account)
    incoming, outgoing = _movement_minor(account.id, through=through)
    return opening + incoming - outgoing


def _account_opening(account, day: date, lock=None) -> float:
    """Opening balance for *account* at the start of *day*.

    * A previously locked day is authoritative: the locked counted closing is
      carried forward plus every movement posted after that day.
    * Otherwise the opening is the ledger balance at the end of the previous
      day, computed exactly like ``ledger_balance()`` so the board ties out
      with Accounts.
    * An account whose opening balance is effective in the future has not
      started yet on this day, so its opening is zero.
    """
    if lock is not None:
        base = int(to_minor(lock.counted or 0))
        lock_end = datetime.combine(lock.position_date, time.max)
        prev_end = datetime.combine(day - timedelta(days=1), time.max)
        incoming, outgoing = _movement_minor(
            account.id, after=lock_end, through=prev_end
        )
        return float(from_minor(base + incoming - outgoing))

    ob_day = _opening_effective_day(account)
    if ob_day is not None and ob_day > day:
        return 0.0
    return float(from_minor(ledger_opening_minor(
        account, through=datetime.combine(day - timedelta(days=1), time.max)
    )))


def parse_counted_amount(value, *, field="Counted amount"):
    """Parse a user-typed physical count ("160,000", "Rs. 160000.50", "160 000").

    The board used ``type=number`` inputs, which silently discard thousands
    separators and then block the submit, so a count typed the way people
    write money never reached the server.  Parsing is explicit here instead.
    """
    raw = str(value if value is not None else "").strip()
    cleaned = re.sub(r"(?i)rs\.?|pkr|,|\s|_", "", raw)
    if cleaned in ("", "-", "--", "—"):
        raise ValueError(f"{field} is required.")
    try:
        amount = float(cleaned)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number (for example 160000 or 160,000).")
    if not math.isfinite(amount):
        raise ValueError(f"{field} must be a finite number.")
    if amount < 0:
        raise ValueError(f"{field} cannot be negative.")
    return _money_round(amount)


def _activity_between(from_date: str, to_date: str) -> dict:
    """Per-account movement totals for a date window (inclusive)."""
    act = {}
    if from_date > to_date:
        return act
    rows = collect_cash_flow_rows(from_date, to_date)
    for r in rows:
        if (r.get("status") or "active") != "active":
            continue
        rtype = r.get("type")
        aid = r.get("account_id")
        if aid:
            a = act.setdefault(aid, {"in": 0.0, "out": 0.0, "tin": 0.0, "tout": 0.0})
            if rtype == CF_DIR_IN:
                a["in"] += float(r.get("cash_in") or 0)
            elif rtype == CF_DIR_OUT:
                a["out"] += float(r.get("cash_out") or 0)
            elif rtype == CF_DIR_TRANSFER:
                a["tout"] += float(r.get("transfer_amount") or 0)
        aid2 = r.get("account_to_id")
        if aid2 and rtype == CF_DIR_TRANSFER:
            b = act.setdefault(aid2, {"in": 0.0, "out": 0.0, "tin": 0.0, "tout": 0.0})
            b["tin"] += float(r.get("transfer_amount") or 0)
    return act


def account_positions_for_date(day) -> list:
    """Return the per-account position dicts for a financial day.

    Everything shown here is derived from the accounts and their ledger.  The
    only stored inputs are the physically counted figures and the verified day
    locks — and those are used only when they genuinely belong to the account
    holding that id today (see ``position_belongs_to_account``).  Residue from
    a previous dataset is purged first, so a wipe + re-entered accounts start
    from a clean board instead of old "hard-coded" looking figures.
    """
    try:
        purge_stale_reconciliation_state()
    except Exception:  # never block the board on self-healing
        db.session.rollback()

    day = _to_date(day)
    day_str = day.strftime("%Y-%m-%d")
    accounts = _cf_company_accounts(active_only=True)
    account_by_id = {a.id: a for a in accounts}

    saved = {}
    for p in CashDayAccountPosition.query.filter_by(position_date=day).all():
        if position_belongs_to_account(p, account_by_id.get(p.account_id)):
            saved[p.account_id] = p
    locks = {}
    prior_locked = CashDayAccountPosition.query.filter(
        CashDayAccountPosition.is_locked.is_(True),
        CashDayAccountPosition.position_date < day,
        CashDayAccountPosition.account_id.in_([a.id for a in accounts] or [0]),
    ).order_by(CashDayAccountPosition.position_date.asc()).all()
    for p in prior_locked:
        if not position_belongs_to_account(p, account_by_id.get(p.account_id)):
            continue
        locks[p.account_id] = p

    # Opening = locked carry-forward, else the ledger balance at the end of the
    # previous day (the same rule ledger_balance() uses on every Accounts
    # screen).  Computing it from the ledger in minor units keeps the board
    # exact instead of drifting a few paisa on float accumulation.
    opening_by_acc = {
        acc.id: _money_round(_account_opening(acc, day, locks.get(acc.id)))
        for acc in accounts
    }

    day_act = _activity_between(day_str, day_str)

    positions = []
    for acc in accounts:
        opening = opening_by_acc.get(acc.id, 0.0)
        a = day_act.get(acc.id, {"in": 0.0, "out": 0.0, "tin": 0.0, "tout": 0.0})
        amount_in = _money_round(a["in"])
        amount_out = _money_round(a["out"])
        transfer_in = _money_round(a["tin"])
        transfer_out = _money_round(a["tout"])
        expected = _money_round(
            opening + amount_in - amount_out + transfer_in - transfer_out
        )
        s = saved.get(acc.id)
        counted = float(s.counted) if s is not None and s.counted is not None else None
        is_locked = bool(s.is_locked) if s is not None else False
        eff_counted = counted if counted is not None else (expected if is_locked else None)
        difference = (
            _money_round(eff_counted - expected) if eff_counted is not None else 0.0
        )
        account_category = (acc.category or "").lower()
        # Daily reconciliation already has dedicated balance columns.  Keep the
        # account title clean here; showing the live current balance in the
        # account label makes the row noisy and can be confused with the dated
        # opening/closing figures on this board.
        positions.append({
            "account_id": acc.id,
            "account_name": acc.name or _cf_account_label(acc),
            "category": account_category,
            "category_label": account_category.upper(),
            "opening": opening,
            "in": amount_in,
            "out": amount_out,
            "transfer_in": transfer_in,
            "transfer_out": transfer_out,
            "expected_closing": expected,
            "counted": counted,
            "effective_counted": eff_counted,
            "difference": difference,
            "is_locked": is_locked,
        })
    return positions


def get_day_lock(day):
    return CashDayLock.query.filter_by(lock_date=_to_date(day)).first()


def save_count(day, account_id, counted, actor=None):
    """Store the user-entered physical count for one account on a day.

    ``counted`` accepts anything a person types ("160,000", "Rs 160000.50",
    "160 000") — parsing lives here so the browser never has to be the
    validator.  Locked days are immutable until they are unlocked.
    """
    try:
        purge_stale_reconciliation_state()
    except Exception:
        db.session.rollback()
    day = _to_date(day)
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        raise ValueError("Select an account to count.")
    amount = parse_counted_amount(counted)
    day_lock = get_day_lock(day)
    pos = CashDayAccountPosition.query.filter_by(
        position_date=day, account_id=account_id
    ).first()
    if day_lock is not None or (pos is not None and pos.is_locked):
        raise ValueError(
            f"{day:%Y-%m-%d} is locked. Unlock the day before changing counts."
        )
    acc = db.session.get(Account, account_id)
    if acc is None:
        raise ValueError("Account not found.")
    actor_name = getattr(actor, "username", None) or actor
    if pos is None:
        pos = CashDayAccountPosition(
            position_date=day,
            account_id=account_id,
            account_name=(acc.name if acc else ""),
        )
        db.session.add(pos)
    # Keep the stored label in step with renames; figures always come from the
    # account + ledger, never from this snapshot.
    pos.account_name = acc.name or pos.account_name
    pos.counted = amount
    pos.difference = None  # recomputed on read
    pos.updated_by = actor_name
    db.session.commit()
    return pos


def lock_day(day, actor=None, note=""):
    """Verify & lock a financial day.

    Every account's counted figure is frozen (defaulting to its expected
    closing when not manually counted) and a day-level lock row is written.
    The locked counted totals become the next day's opening positions.
    """
    day = _to_date(day)
    actor_name = getattr(actor, "username", None) or actor
    now = pk_now()
    try:
        purge_stale_reconciliation_state()
    except Exception:
        db.session.rollback()
    positions = account_positions_for_date(day)

    total_expected = 0.0
    total_counted = 0.0
    for p in positions:
        counted = p["counted"] if p["counted"] is not None else p["expected_closing"]
        total_expected += p["expected_closing"]
        total_counted += counted
        pos = CashDayAccountPosition.query.filter_by(
            position_date=day, account_id=p["account_id"]
        ).first()
        if pos is None:
            pos = CashDayAccountPosition(
                position_date=day,
                account_id=p["account_id"],
                account_name=p["account_name"],
            )
            db.session.add(pos)
        pos.opening = p["opening"]
        pos.amount_in = p["in"]
        pos.amount_out = p["out"]
        pos.transfer_in = p["transfer_in"]
        pos.transfer_out = p["transfer_out"]
        pos.expected_closing = p["expected_closing"]
        pos.counted = counted
        pos.difference = _money_round(counted - p["expected_closing"])
        pos.is_locked = True
        pos.locked_by = actor_name
        pos.locked_at = now
        pos.updated_by = actor_name

    total_expected = _money_round(total_expected)
    total_counted = _money_round(total_counted)

    lock = get_day_lock(day)
    if lock is None:
        lock = CashDayLock(lock_date=day)
        db.session.add(lock)
    lock.total_expected = total_expected
    lock.total_counted = total_counted
    lock.difference = _money_round(total_counted - total_expected)
    lock.note = note or ""
    lock.locked_by = actor_name
    lock.locked_at = now
    db.session.commit()
    return lock


def unlock_day(day):
    day = _to_date(day)
    CashDayAccountPosition.query.filter_by(position_date=day).update({"is_locked": False})
    lock = get_day_lock(day)
    if lock:
        db.session.delete(lock)
    db.session.commit()


def delete_count(day, account_id):
    try:
        purge_stale_reconciliation_state()
    except Exception:
        db.session.rollback()
    day = _to_date(day)
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        raise ValueError("Select an account to clear.")
    day_lock = get_day_lock(day)
    pos = CashDayAccountPosition.query.filter_by(
        position_date=day, account_id=account_id
    ).first()
    if day_lock is not None or (pos is not None and pos.is_locked):
        raise ValueError(
            f"{day:%Y-%m-%d} is locked. Unlock the day before clearing counts."
        )
    if pos:
        db.session.delete(pos)
        db.session.commit()
