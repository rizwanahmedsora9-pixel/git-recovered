import logging
import os
import sqlite3
import threading
import time
from datetime import datetime

from sqlalchemy import case, func, text

from models import db, Account, AccountTransaction, DirectSale, DirectSaleItem, Entry, Material


_RECON_LOCK = threading.Lock()
LOG = logging.getLogger(__name__)


def _pk_now_naive():
    # Keep it simple here: main.py already normalizes PK time elsewhere,
    # but reconciliation is internal and only needs a stable timestamp.
    return datetime.now().replace(microsecond=0)


def real_app(app):
    """Return the concrete Flask app behind a ``LocalProxy``.

    ``current_app`` is a proxy: pushing ``proxy.app_context()`` from a worker
    thread raises "Working outside of application context".  Callers resolve it
    while a request/app context is still active; this is the safety net for the
    case where the proxy is passed straight into the thread.
    """
    getter = getattr(app, "_get_current_object", None)
    if callable(getter):
        try:
            return getter()
        except RuntimeError:
            return None
    return app


def _safe_copy_db(db_path: str, backup_dir: str) -> str | None:
    """Copy the live database with SQLite's online-backup API.

    ``shutil.copy2`` of a database that is being written (WAL or rollback
    journal) can capture a torn page.  The backup API produces a consistent
    copy while the shop keeps working.
    """
    try:
        if not db_path or not os.path.exists(db_path):
            return None
        os.makedirs(backup_dir, exist_ok=True)
        stamp = _pk_now_naive().strftime("%Y%m%d_%H%M%S")
        name = f"recon_backup_{stamp}.db"
        dest = os.path.join(backup_dir, name)
        source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        try:
            target = sqlite3.connect(dest, timeout=30)
            try:
                source.backup(target, pages=256, sleep=0.05)
            finally:
                target.close()
        finally:
            source.close()
        return dest
    except Exception:
        LOG.warning("Reconciliation database copy failed (continuing)", exc_info=True)
        return None


def _cleanup_reconcile_backups(backup_dir: str, *, keep: int = 10) -> int:
    try:
        keep = int(keep or 0)
    except Exception:
        keep = 10
    keep = max(0, keep)
    try:
        if not backup_dir or not os.path.isdir(backup_dir) or keep <= 0:
            return 0
        files = []
        for name in os.listdir(backup_dir):
            if not name.startswith("recon_backup_") or not name.lower().endswith(".db"):
                continue
            path = os.path.join(backup_dir, name)
            try:
                st = os.stat(path)
            except Exception:
                continue
            files.append((st.st_mtime, path))
        files.sort(reverse=True)  # newest first
        removed = 0
        for _, path in files[keep:]:
            try:
                os.remove(path)
                removed += 1
            except Exception:
                pass
        return removed
    except Exception:
        return 0


def reconcile_material_totals(*, tolerance=0.01) -> dict:
    """
    Make Material.total match net stock from Entry rows:
      net = SUM(IN qty) - SUM(OUT qty) over non-void entries.
    """
    net_rows = (
        db.session.query(
            Entry.material.label("material"),
            func.sum(
                case(
                    (func.upper(func.coalesce(Entry.type, "")) == "IN", func.coalesce(Entry.qty, 0)),
                    (func.upper(func.coalesce(Entry.type, "")) == "OUT", -func.coalesce(Entry.qty, 0)),
                    else_=0,
                )
            ).label("net"),
        )
        .filter(Entry.is_void == False)
        .group_by(Entry.material)
        .all()
    )
    net_map = {(r.material or "").strip(): float(r.net or 0) for r in net_rows if (r.material or "").strip()}

    changed = 0
    checked = 0
    for mat in Material.query.all():
        name = (mat.name or "").strip()
        if not name:
            continue
        checked += 1
        expected = float(net_map.get(name, 0.0))
        actual = float(getattr(mat, "total", 0) or 0.0)
        if abs(actual - expected) <= float(tolerance or 0):
            continue
        mat.total = expected
        changed += 1

    return {"materials_checked": checked, "materials_changed": changed}


def _account_expected_minor(account, ledger_minor: int) -> int:
    """The ledger-based balance of *account*, opening baseline included.

    ``ledger_balance()`` (the figure the Accounts list, the dashboard and the
    daily-reconciliation board display) is *opening + active ledger*.  Comparing
    the stored balance with the ledger movements alone made every account that
    was funded through its opening look short by exactly that opening, so this
    pass used to insert a bogus "Adjustment" for the opening amount and the
    opening was then counted twice everywhere the ledger is displayed.

    Resolution order matches ``payments_crud.ledger_balance`` exactly.
    """
    from utils.money import to_minor

    opening_minor = getattr(account, "opening_balance_minor", None)
    if opening_minor is None:
        opening = getattr(account, "opening_balance", None)
        if opening is not None:
            opening_minor = to_minor(opening)
        else:
            # Legacy row without an opening baseline: infer one that leaves the
            # stored balance unchanged (the same rule schema bootstrap uses).
            stored = getattr(account, "balance_minor", None)
            if stored is None:
                stored = to_minor(getattr(account, "balance", 0) or 0)
            opening_minor = int(stored) - int(ledger_minor)
    return int(opening_minor) + int(ledger_minor)


def reconcile_account_balances(*, tolerance=0.01, create_adjustment=True) -> dict:
    """Keep Account.balance consistent with its opening baseline + ledger.

    Works in integer paisa (``utils.money``) so no float rounding can invent or
    hide a difference.

    - ``expected`` = ``_account_expected_minor(account, ledger)`` (opening + movements).
    - If the stored balance differs by more than *tolerance*:
        - ``create_adjustment=True``: post one deterministic 'Adjustment' that
          bridges the gap, so the ledger ties out to the stored balance.
        - ``create_adjustment=False``: overwrite the stored balance.
    """
    from utils.money import to_minor

    tolerance_minor = max(0, to_minor(tolerance or 0))

    ledger = {}
    tx_rows = AccountTransaction.query.filter(AccountTransaction.is_void == False).all()
    for tx in tx_rows:
        amount_minor = getattr(tx, "amount_minor", None)
        if amount_minor is None:
            amount_minor = to_minor(getattr(tx, "amount", 0) or 0)
        amount_minor = int(amount_minor)
        to_id = getattr(tx, "to_account_id", None)
        from_id = getattr(tx, "from_account_id", None)
        if to_id:
            ledger[int(to_id)] = ledger.get(int(to_id), 0) + amount_minor
        if from_id:
            ledger[int(from_id)] = ledger.get(int(from_id), 0) - amount_minor

    changed = 0
    adjustments = 0
    checked = 0
    for acc in Account.query.all():
        acc_id = int(acc.id)
        checked += 1
        movements_minor = int(ledger.get(acc_id, 0))
        expected_minor = _account_expected_minor(acc, movements_minor)

        stored_minor = getattr(acc, "balance_minor", None)
        if stored_minor is None:
            stored_minor = to_minor(getattr(acc, "balance", 0) or 0)
        stored_minor = int(stored_minor)

        diff_minor = stored_minor - expected_minor
        if abs(diff_minor) <= tolerance_minor:
            continue

        if not create_adjustment:
            acc.balance_minor = expected_minor
            acc.balance = float(expected_minor) / 100.0
            changed += 1
            continue

        # Bridge the gap with a reconciliation adjustment, so the ledger matches
        # the stored balance.  A second pass is naturally idempotent: the
        # adjustment is part of the ledger it just measured, so the difference
        # reads as zero next time.
        marker = f"[RECON:ACCOUNT:{acc_id}]"
        description = "Balance reconciliation (auto)"
        opening_minor = expected_minor - movements_minor
        note = (
            f"{marker} Stored balance={stored_minor / 100:.2f}, "
            f"opening={opening_minor / 100:.2f}, "
            f"ledger={movements_minor / 100:.2f}, diff={diff_minor / 100:.2f}"
        )

        amount = abs(diff_minor) / 100.0
        if diff_minor > 0:
            tx = AccountTransaction(
                from_account_id=None,
                to_account_id=acc_id,
                amount=amount,
                amount_minor=abs(diff_minor),
                description=description,
                note=note,
                transaction_type="Adjustment",
                date_posted=_pk_now_naive(),
                is_void=False,
            )
        else:
            tx = AccountTransaction(
                from_account_id=acc_id,
                to_account_id=None,
                amount=amount,
                amount_minor=abs(diff_minor),
                description=description,
                note=note,
                transaction_type="Adjustment",
                date_posted=_pk_now_naive(),
                is_void=False,
            )
        db.session.add(tx)
        adjustments += 1
        changed += 1

    return {"accounts_checked": checked, "accounts_adjusted": adjustments, "accounts_changed": changed}


def reconcile_direct_sale_item_names(*, tolerance=0.01) -> dict:
    """
    Fix mismatches where DirectSaleItem.product_name doesn't match Entry.material for the same sale bill_ref.

    Only adjusts item.product_name when it can be matched to an entry material by quantity (within tolerance).
    This helps keep reports consistent and avoids stock drift when other code resolves materials by name.
    """
    # Map invoice_id -> invoice_no (best effort, avoids importing Invoice model here).
    invoice_no_by_id = {}
    if db.engine and db.engine.dialect.name == "sqlite":
        try:
            rows = db.session.execute(text("SELECT id, invoice_no FROM invoice WHERE COALESCE(is_void,0)=0")).fetchall()
            for r in rows:
                invoice_no_by_id[int(r[0])] = (r[1] or "").strip()
        except Exception:
            invoice_no_by_id = {}

    fixed_items = 0
    checked_sales = 0
    migrated_bills = 0

    # Load both sides ONCE.  The previous version ran two queries per sale inside
    # the loop, which is what made a single pass cost minutes on a shop-sized
    # database (and that pass ran inside the web process).
    sales = DirectSale.query.filter(DirectSale.is_void == False).all()

    items_by_sale = {}
    for item in (
        DirectSaleItem.query.join(DirectSale, DirectSale.id == DirectSaleItem.sale_id)
        .filter(DirectSale.is_void == False)
        .order_by(DirectSaleItem.sale_id.asc(), DirectSaleItem.id.asc())
        .all()
    ):
        items_by_sale.setdefault(int(item.sale_id), []).append(item)

    def _direct_sale_out_entries(bill_refs):
        refs = {ref for ref in bill_refs if ref}
        if not refs:
            return {}
        rows = (
            Entry.query.filter(
                Entry.is_void == False,
                func.upper(func.coalesce(Entry.type, "")) == "OUT",
                func.trim(func.coalesce(Entry.nimbus_no, "")) == "Direct Sale",
                func.trim(func.coalesce(Entry.bill_no, "")).in_(list(refs)),
            )
            .order_by(Entry.id.asc())
            .all()
        )
        grouped = {}
        for row in rows:
            grouped.setdefault((row.bill_no or "").strip(), []).append(row)
        return grouped

    bill_ref_by_sale = {}
    for sale in sales:
        manual = (getattr(sale, "manual_bill_no", "") or "").strip()
        auto = (getattr(sale, "auto_bill_no", "") or "").strip()
        inv_no = ""
        inv_id = getattr(sale, "invoice_id", None)
        if inv_id:
            inv_no = (invoice_no_by_id.get(int(inv_id)) or "").strip()
        bill_ref_by_sale[int(sale.id)] = (
            manual or inv_no or auto or f"UNBILLED-{sale.id}"
        )

    entries_by_ref = _direct_sale_out_entries(bill_ref_by_sale.values())

    for sale in sales:
        checked_sales += 1

        auto = (getattr(sale, "auto_bill_no", "") or "").strip()
        inv_no = ""
        inv_id = getattr(sale, "invoice_id", None)
        if inv_id:
            inv_no = (invoice_no_by_id.get(int(inv_id)) or "").strip()

        # Expected ref under current logic (invoice takes precedence when present).
        bill_ref = bill_ref_by_sale[int(sale.id)]

        # Legacy fix: if sale has invoice_no but entries were posted under auto bill ref, migrate bill_no.
        if inv_no and bill_ref == inv_no and auto and inv_no not in entries_by_ref:
            legacy = _direct_sale_out_entries({auto}).get(auto, [])
            if legacy:
                for e in legacy:
                    e.bill_no = inv_no
                migrated_bills += 1
                entries_by_ref[inv_no] = legacy
                entries_by_ref.pop(auto, None)

        entry_rows = entries_by_ref.get(bill_ref) or []
        if not entry_rows:
            continue

        item_rows = items_by_sale.get(int(sale.id)) or []
        if not item_rows:
            continue

        def _qty(v):
            try:
                return float(v or 0)
            except Exception:
                return 0.0

        unmatched_items = set(int(r.id) for r in item_rows)
        items_by_id = {int(r.id): r for r in item_rows}

        for e in entry_rows:
            eq = _qty(getattr(e, "qty", 0))
            emat = (getattr(e, "material", "") or "").strip()
            if not emat:
                continue

            # Prefer exact name matches for this qty.
            chosen_id = None
            for it_id in list(unmatched_items):
                it = items_by_id[it_id]
                if abs(_qty(getattr(it, "qty", 0)) - eq) <= tolerance and (getattr(it, "product_name", "") or "").strip() == emat:
                    chosen_id = it_id
                    break

            # Otherwise match any item with same qty.
            if chosen_id is None:
                for it_id in list(unmatched_items):
                    it = items_by_id[it_id]
                    if abs(_qty(getattr(it, "qty", 0)) - eq) <= tolerance:
                        chosen_id = it_id
                        break

            if chosen_id is None:
                continue
            unmatched_items.discard(chosen_id)
            it = items_by_id[chosen_id]
            cur_name = (getattr(it, "product_name", "") or "").strip()
            if cur_name != emat and emat:
                it.product_name = emat
                fixed_items += 1

    return {
        "sales_checked": checked_sales,
        "items_renamed": fixed_items,
        "bill_refs_migrated": migrated_bills,
    }


def run_auto_reconcile(app, *, interval_seconds=600, tolerance=0.01, fix=True) -> None:
    """
    Background loop. Uses a global lock so it won't overlap with itself.

    ``app`` must be the concrete Flask application (see :func:`real_app`); a
    ``current_app`` proxy can only be used from inside a context, which this
    thread never has.
    """
    interval_seconds = max(10, int(interval_seconds or 600))
    flask_app = real_app(app)
    if flask_app is None:
        LOG.warning(
            "Auto-reconcile worker started without a usable Flask app; "
            "it will not run. Set AUTO_RECONCILE_ENABLED=0 to silence this."
        )
        return
    failures = 0
    while True:
        if not fix:
            time.sleep(interval_seconds)
            continue
        # An admin wipe rebuilds the whole ledger; reconciling it concurrently
        # only competes for the SQLite write lock and can abort the reset.
        try:
            from app.services import state as _state
            wipe_running = bool(getattr(_state, 'WIPE_IN_PROGRESS', False))
        except Exception:  # noqa: BLE001
            wipe_running = False
        if wipe_running:
            time.sleep(min(5, interval_seconds))
            continue
        if not _RECON_LOCK.acquire(blocking=False):
            time.sleep(interval_seconds)
            continue
        try:
            with flask_app.app_context():
                # Optional DB file backup.  Kept small on purpose: the copies
                # live inside instance/, and a shop with a large database
                # should not quietly fill its disk quota.
                enable_backup = os.environ.get("AUTO_RECONCILE_DB_BACKUP", "1").strip() != "0"
                keep_backups = os.environ.get("AUTO_RECONCILE_BACKUP_KEEP", "3").strip()
                db_uri = flask_app.config.get("SQLALCHEMY_DATABASE_URI") or ""
                db_path = ""
                if db_uri.startswith("sqlite:///"):
                    db_path = db_uri.replace("sqlite:///", "", 1)
                if enable_backup and db_path:
                    backup_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "instance", "reconcile_backups"))
                    _safe_copy_db(db_path, backup_dir)
                    _cleanup_reconcile_backups(backup_dir, keep=int(keep_backups or 3))

                reconcile_direct_sale_item_names(tolerance=tolerance)
                reconcile_material_totals(tolerance=tolerance)
                reconcile_account_balances(tolerance=tolerance, create_adjustment=True)
                db.session.commit()
            failures = 0
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            failures += 1
            # Log the first failure of a streak, then stay quiet: this loop
            # runs every few minutes and must not flood the error log.
            if failures == 1:
                LOG.exception(
                    "Auto-reconcile pass failed; it will retry on the next interval."
                )
        finally:
            _RECON_LOCK.release()
        time.sleep(interval_seconds)
