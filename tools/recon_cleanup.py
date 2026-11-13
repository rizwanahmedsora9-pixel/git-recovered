#!/usr/bin/env python3
"""One-off repair for invented balance-reconciliation rows.

Older ``reconcile_account_balances()`` runs could post an ``Adjustment`` row
whose note carries the ``[RECON:ACCOUNT:<id>]`` marker even when the account's
opening balance already funded it.  Those rows inflate the ledger and the
stored balance by the same amount, so the money looks consistent while the
ledger no longer reflects real movements.

This tool only ever touches rows it can prove were written by that routine:

* ``transaction_type = 'Adjustment'``
* ``note`` starts with ``[RECON:ACCOUNT:``
* the account id in the marker matches the row's account

Usage::

    python tools/maintenance.py recon-audit                 # report only
    python tools/maintenance.py recon-audit --apply         # delete + resync

``--apply`` deletes the provable rows and then re-derives each affected
account's stored balance from its opening balance plus real ledger movements
(``create_adjustment=False`` semantics: overwrite, never post).  No new row is
ever created, so the operation is safe to repeat and safe to abort.
"""
from __future__ import annotations

import re

MARKER = re.compile(r"^\[RECON:ACCOUNT:(\d+)\]")


def _provable_rows():
    """Return (row, account_id) pairs that the old reconciler provably wrote."""
    from models import AccountTransaction, db

    rows = (
        AccountTransaction.query.filter(
            AccountTransaction.transaction_type == "Adjustment",
            AccountTransaction.is_void == False,  # noqa: E712
        )
        .order_by(AccountTransaction.id.asc())
        .all()
    )
    provable = []
    for row in rows:
        match = MARKER.match((getattr(row, "note", "") or "").strip())
        if not match:
            continue
        account_id = int(match.group(1))
        if account_id not in (row.to_account_id, row.from_account_id):
            continue
        provable.append((row, account_id))
    return provable


def audit(apply=False):
    """List (and with *apply*, delete) the provable reconciliation rows."""
    from models import AccountTransaction, db
    from utils.money import to_minor
    from utils.reconciliation import reconcile_account_balances

    rows = _provable_rows()
    by_account = {}
    for row, account_id in rows:
        amount_minor = getattr(row, "amount_minor", None)
        if amount_minor is None:
            amount_minor = to_minor(getattr(row, "amount", 0) or 0)
        signed = int(amount_minor)
        if row.from_account_id:
            signed = -signed
        by_account[account_id] = by_account.get(account_id, 0) + signed

    print(f"provable [RECON:ACCOUNT:*] adjustment rows: {len(rows)}")
    print(f"accounts affected                         : {len(by_account)}")
    for account_id, delta in sorted(by_account.items()):
        print(f"  account {account_id:>6}: ledger effect {delta / 100:>14,.2f}")

    if not rows:
        return {"rows": 0, "accounts": 0, "applied": False}

    if not apply:
        print("\nReport only. Re-run with --apply to delete these rows and")
        print("re-derive each affected account balance from real movements.")
        return {"rows": len(rows), "accounts": len(by_account), "applied": False}

    for row, _account_id in rows:
        # Hard delete: this row is a tool-written artefact, never a movement a
        # user recorded, so it leaves no audit value behind.
        db.session.delete(row)
    db.session.flush()

    # Re-derive stored balances from opening + real movements.  This overwrites
    # (never posts), so no replacement adjustment can be invented.
    result = reconcile_account_balances(create_adjustment=False)
    db.session.commit()
    print(f"\nDeleted {len(rows)} row(s).")
    print(
        "Accounts resynced from opening + movements: "
        f"{result['accounts_changed']} of {result['accounts_checked']}"
    )
    return {
        "rows": len(rows),
        "accounts": len(by_account),
        "resynced": result["accounts_changed"],
        "applied": True,
    }
