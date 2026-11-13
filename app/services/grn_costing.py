"""GRN landed costing and purchase-obligation arithmetic — the SINGLE authority.

Owner rules (approved 2026-11-02):

A1  A GRN NEVER pays.  It records material received, purchase costs, charge
    details and payment obligations only.  No payment/settlement/cash row is
    created here.  All money is paid from Accounts -> Pay GRN Expenses.

A2  Supplier Ledger credit for a GRN is material
    ``sum(GRNItem.qty * GRNItem.price_at_time)`` plus only the individual
    applicable charges whose supplier-billing checkbox is selected.  An
    unselected charge belongs to its own non-supplier recipient.  Tax,
    discount and adjustment are never added.  Billing choices do not change
    inventory, COGS or expense recognition.

A3  Material and supplier-billed expenses are SEPARATE payment obligations.
    ``GRN_MATERIAL`` settles stock value only; ``GRN_EXPENSES`` settles the
    ticked GRN costs only.  Both are paid from Accounts, neither can be paid
    twice, and the supplier ledger / payables total is unchanged by how the
    operator splits the payment.  (An older single lumped obligation is still
    accepted and is always applied to material first.)

Everything else (landed FIFO cost, COGS, expense recognition, payables, cash
actually paid, GRN detail screens) is derived from this module so no screen can
compute its own version of the truth.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func

from models import (
    db, GRN, GRNItem, GRNCharge, SupplierPayment, AccountTransaction,
    CashFlowParty,
)
from utils.money import to_minor, from_minor, decimal_money
from app.services.pricing import allocate_cents

logger = logging.getLogger(__name__)

CHARGE_TYPES = ('VEHICLE_FARE', 'TRANSPORT', 'LOADING', 'UNLOADING', 'OTHER')
CHARGE_TYPE_LABELS = {
    'VEHICLE_FARE': 'Vehicle Fare',
    'TRANSPORT': 'Transportation',
    'LOADING': 'Loading',
    'UNLOADING': 'Unloading',
    'OTHER': 'Other Expenses',
}
RECIPIENT_TYPES = ('TRANSPORTER', 'LABOUR', 'OTHER', 'UNSPECIFIED')
RECIPIENT_LABELS = {
    'TRANSPORTER': 'Vehicle / Transporter',
    'LABOUR': 'Loading / Labour',
    'OTHER': 'Other Party',
    'UNSPECIFIED': 'Unassigned (legacy charge)',
}
DEFAULT_RECIPIENT_BY_CHARGE = {
    'VEHICLE_FARE': 'TRANSPORTER',
    'TRANSPORT': 'TRANSPORTER',
    'LOADING': 'LABOUR',
    'UNLOADING': 'LABOUR',
    'OTHER': 'OTHER',
}
COST_TREATMENTS = ('INVENTORY', 'EXPENSE', 'NON_MATERIAL', 'UNTRACKED_LEGACY')
DECLARED_STATUSES = ('PAID', 'UNPAID', 'NOT_APPLICABLE')
ALLOCATION_BASES = ('VALUE', 'QUANTITY', 'MANUAL')

#: Supplier payment obligations.  Material (stock value) and supplier-billed
#: GRN expenses are paid separately; GRN_CHARGE is a non-supplier recipient.
OBLIGATION_SUPPLIER = 'SUPPLIER'
OBLIGATION_MATERIAL = 'MATERIAL'
OBLIGATION_EXPENSES = 'EXPENSES'
OBLIGATION_CHARGE = 'CHARGE'

OBLIGATION_PREFIX = {
    OBLIGATION_SUPPLIER: 'SUPPLIER',
    OBLIGATION_MATERIAL: 'MATERIAL',
    OBLIGATION_EXPENSES: 'EXPENSES',
    OBLIGATION_CHARGE: 'CHARGE',
}
#: payment_kind written on the SupplierPayment row for each obligation.
PAYMENT_KIND_BY_OBLIGATION = {
    OBLIGATION_SUPPLIER: 'GRN_MATERIAL',   # legacy lumped obligation
    OBLIGATION_MATERIAL: 'GRN_MATERIAL',
    OBLIGATION_EXPENSES: 'GRN_EXPENSES',
    OBLIGATION_CHARGE: 'GRN_CHARGE',
}
OBLIGATION_LABEL = {
    OBLIGATION_SUPPLIER: 'Supplier bill',
    OBLIGATION_MATERIAL: 'Stock / material amount',
    OBLIGATION_EXPENSES: 'GRN expenses billed to supplier',
}

LEGACY_MIRRORS = (
    ('freight_cost', ('VEHICLE_FARE', 'TRANSPORT')),
    ('loading_cost', ('LOADING', 'UNLOADING')),
    ('other_expense', ('OTHER',)),
)


def _minor(value, field='Amount'):
    return to_minor(value, field=field)


def _f(minor):
    return float(from_minor(int(minor or 0)))


def _bulk_items(grn_ids):
    """{(grn_id): [GRNItem]} for many GRNs in one query.

    The single-GRN helpers below run once per GRN in a report; asking the
    database per GRN turned a 4,000-GRN page into 4,000 queries (measured:
    5,400 charge queries and 5,000 line queries on one report page).  Reports
    load the period's lines once and prime this map instead.
    """
    grouped = {}
    ids = [int(i) for i in grn_ids if i]
    if not ids:
        return {}
    rows = (
        GRNItem.query.filter(
            GRNItem.grn_id.in_(ids),
            GRNItem.is_void == False,  # noqa: E712
            GRNItem.qty > 0,
        )
        .order_by(GRNItem.grn_id.asc(), GRNItem.id.asc())
        .all()
    )
    for row in rows:
        grouped.setdefault(int(row.grn_id), []).append(row)
    return grouped


def _bulk_charges(grn_ids):
    """{(grn_id): [GRNCharge]} for many GRNs in one query (see ``_bulk_items``)."""
    grouped = {}
    ids = [int(i) for i in grn_ids if i]
    if not ids:
        return {}
    rows = (
        GRNCharge.query.filter(
            GRNCharge.grn_id.in_(ids),
            GRNCharge.is_void == False,  # noqa: E712
        )
        .order_by(GRNCharge.grn_id.asc(), GRNCharge.id.asc())
        .all()
    )
    for row in rows:
        grouped.setdefault(int(row.grn_id), []).append(row)
    return grouped


# Per-request batch maps.  Values are only read back for the GRN ids that were
# primed, so a stale or partial map can never invent a line or a charge: an
# unprimed GRN still takes the direct query path below.
#
# ``_PRIMED_DB_KEY`` records which database the maps were filled from.  Two
# apps in one process (tests, a migration tool) must never hand each other's
# rows back, so every read is scoped to the current engine; the maps are also
# dropped on commit/rollback (see ``invalidate_line_cache``).
_PRIMED_DB_KEY = [None]
_PRIMED_ITEMS = {}
_PRIMED_CHARGES = {}
# Charge id -> money paid straight to that expense's recipient (minor units),
# for charges whose recipient payments were loaded in bulk by ``prime_grn_lines``.
_PRIMED_CHARGE_RECIPIENT_PAID = {}
_PRIMED_PAYMENTS = {}
_PRIMED_CASH_PAID = {}
# ``SupplierPayment.grn_id IS NULL`` rows matched by an ``[auto_grn_pay:<id>]``
# marker.  That scan is parent-less (nothing to join on), so the only way to make
# it bounded is to load the (small) table once per connection instead of once
# per GRN.  Keyed by the SQLAlchemy engine so the cache lives exactly as long as
# the database it was read from, and cleared on commit/rollback/DDL.
_LEGACY_GRN_PAYMENTS = {}


def _primed_store():
    """Return the batch maps for the database this request is reading.

    A different engine means different data: clear everything first so a cached
    row id from another database can never be returned.
    """
    try:
        key = id(db.engine)
    except Exception:
        key = None
    if key is not None and _PRIMED_DB_KEY[0] != key:
        invalidate_line_cache()
        _PRIMED_DB_KEY[0] = key
    return {
        "items": _PRIMED_ITEMS,
        "charges": _PRIMED_CHARGES,
        "recipient_paid": _PRIMED_CHARGE_RECIPIENT_PAID,
        "payments": _PRIMED_PAYMENTS,
        "cash_paid": _PRIMED_CASH_PAID,
    }


def _legacy_grn_payments_cache_key():
    try:
        return id(db.engine)
    except Exception:
        return None


def invalidate_line_cache():
    """Drop every cached GRN line/charge/payment batch (call after a write)."""
    _PRIMED_ITEMS.clear()
    _PRIMED_CHARGES.clear()
    _PRIMED_CHARGE_RECIPIENT_PAID.clear()
    _PRIMED_PAYMENTS.clear()
    _PRIMED_CASH_PAID.clear()
    _LEGACY_GRN_PAYMENTS.clear()


def _install_line_cache_invalidation():
    """Clear the caches whenever the session ends a transaction."""
    from sqlalchemy import event

    engine = db.engine
    if getattr(engine, "_ams_line_cache_hook", False):
        return
    # Engine-level "commit"/"rollback" fire when a connection ends its
    # transaction, i.e. exactly when a write could have changed what we cached.
    for name in ("commit", "rollback"):
        event.listen(engine, name, lambda conn: invalidate_line_cache())
    engine._ams_line_cache_hook = True


def _legacy_marked_payments(grn_id):
    """``SupplierPayment`` rows that reference *grn_id* only through a note marker."""
    marker = f"[auto_grn_pay:{grn_id}]".lower()
    key = _legacy_grn_payments_cache_key()
    if key is not None:
        cached = _LEGACY_GRN_PAYMENTS.get(key)
        if cached is None:
            cached = SupplierPayment.query.filter(
                SupplierPayment.grn_id.is_(None),
                SupplierPayment.is_void == False,  # noqa: E712
                func.lower(func.coalesce(SupplierPayment.note, '')).like('%[auto_grn_pay:%'),
            ).all()
            _LEGACY_GRN_PAYMENTS.clear()
            _LEGACY_GRN_PAYMENTS[key] = cached
        return [r for r in cached if marker in (getattr(r, 'note', '') or '').lower()]
    return SupplierPayment.query.filter(
        SupplierPayment.grn_id.is_(None),
        SupplierPayment.is_void == False,  # noqa: E712
        func.lower(func.coalesce(SupplierPayment.note, '')).like(f"%{marker}%"),
    ).all()


def _bulk_grn_payments(grn_ids):
    """{grn_id: [SupplierPayment]} for many GRNs in one query, markers included."""
    ids = [int(i) for i in grn_ids if i]
    if not ids:
        return {}
    grouped = {}
    rows = SupplierPayment.query.filter(
        SupplierPayment.grn_id.in_(ids),
        SupplierPayment.is_void == False,  # noqa: E712
    ).order_by(SupplierPayment.grn_id.asc(), SupplierPayment.id.asc()).all()
    for row in rows:
        grouped.setdefault(int(row.grn_id), []).append(row)
    for grn_id in ids:
        bucket = grouped.setdefault(grn_id, [])
        seen = {r.id for r in bucket}
        for row in _legacy_marked_payments(grn_id):
            if row.id not in seen:
                bucket.append(row)
    return grouped


def _bulk_charge_recipient_paid(charge_ids):
    """{charge_id: minor} of SupplierPayment rows addressed to a GRN expense."""
    ids = [int(i) for i in charge_ids if i]
    if not ids:
        return {}
    totals = {charge_id: 0 for charge_id in ids}
    rows = SupplierPayment.query.filter(
        SupplierPayment.grn_charge_id.in_(ids),
        SupplierPayment.is_void == False,  # noqa: E712
    ).all()
    for row in rows:
        current = getattr(row, 'amount_minor', None)
        amount = int(current) if current is not None else _minor(getattr(row, 'amount', 0))
        totals[int(row.grn_charge_id)] = totals.get(int(row.grn_charge_id), 0) + amount
    return totals


def prime_grn_lines(grns):
    """Load lines and charges for *grns* in bulk (read-only helper for reports)."""
    ids = [int(i) for i in (getattr(g, 'id', None) for g in (grns or [])) if i]
    _install_line_cache_invalidation()
    items = _bulk_items(ids)
    charges = _bulk_charges(ids)
    for grn_id in ids:
        # An explicit empty list matters: most GRNs have no expense lines, and
        # without the entry each of them would still run its own query.
        _PRIMED_ITEMS[grn_id] = items.get(grn_id, [])
        _PRIMED_CHARGES[grn_id] = charges.get(grn_id, [])
    charge_ids = [c.id for charges in charges.values() for c in charges]
    _PRIMED_CHARGE_RECIPIENT_PAID.update(_bulk_charge_recipient_paid(charge_ids))
    payments = _bulk_grn_payments(ids)
    cash_paid = _bulk_cash_paid(ids)
    for grn_id in ids:
        _PRIMED_PAYMENTS[grn_id] = payments.get(grn_id, [])
        _PRIMED_CASH_PAID[grn_id] = cash_paid.get(grn_id, 0)
    return len(_PRIMED_ITEMS), len(_PRIMED_CHARGES)


def active_items(grn):
    """Query directly: a cached relationship may miss rows added in this
    same request (a GRN is saved with its lines and charges in one flush)."""
    if grn is None:
        return []
    grn_id = getattr(grn, 'id', None)
    if grn_id:
        _primed_store()
        if int(grn_id) in _PRIMED_ITEMS:
            return list(_PRIMED_ITEMS[int(grn_id)])
    if grn_id:
        try:
            return [
                i for i in GRNItem.query.filter(
                    GRNItem.grn_id == grn.id,
                    GRNItem.is_void == False,  # noqa: E712
                    GRNItem.qty > 0,
                ).order_by(GRNItem.id.asc()).all()
            ]
        except Exception:
            pass
    return [
        i for i in (getattr(grn, 'items', None) or [])
        if not bool(getattr(i, 'is_void', False)) and float(getattr(i, 'qty', 0) or 0) > 0
    ]


def active_charges(grn):
    if grn is None:
        return []
    grn_id = getattr(grn, 'id', None)
    if grn_id:
        _primed_store()
        if int(grn_id) in _PRIMED_CHARGES:
            return list(_PRIMED_CHARGES[int(grn_id)])
    if grn_id:
        try:
            return GRNCharge.query.filter(
                GRNCharge.grn_id == grn.id,
                GRNCharge.is_void == False,  # noqa: E712
            ).order_by(GRNCharge.id.asc()).all()
        except Exception:
            pass
    return [
        c for c in (getattr(grn, 'charges', None) or [])
        if not bool(getattr(c, 'is_void', False))
    ]


def lot_cost_rate(item):
    """Authoritative per-unit cost for FIFO / P&L / stock valuation.

    LANDED lots carry the allocated landed cost; legacy lots fall back to the
    supplier rate so historical frozen costs never change.
    """
    rate = float(getattr(item, 'landed_unit_cost', 0) or 0)
    if rate > 0:
        return rate
    return float(getattr(item, 'price_at_time', 0) or 0)


def charge_effective_minor(charge):
    """Cost/obligation value of a charge; a non-applicable line is zero.

    Keep the stored amount for audit, but use this value consistently in
    capitalisation, payables and expense recognition.
    """
    if bool(getattr(charge, 'is_void', False)):
        return 0
    if (getattr(charge, 'declared_status', None) or '').upper() == 'NOT_APPLICABLE':
        return 0
    return _charge_minor(charge)


def lot_cost_components(item, qty, total_amount):
    """Split an already-costed FIFO layer into supplier cost and landed costs.

    This is reporting arithmetic only: it never changes a frozen sale cost.
    The supplier component uses the immutable purchase rate.  The remainder
    is the capitalised cost actually recognised by that sale.  Charge types
    are split in whole paisa using the lot's saved allocation, so the detail
    adds back to COGS exactly.  Legacy/frozen costs without a landed lot are
    supplier/unattributed cost, never an invented freight allocation.
    """
    from app.services.pricing import money_float, to_cents, cents_to_float

    amount = money_float(total_amount)
    grn = getattr(item, 'grn', None)
    landed = item is not None and (getattr(grn, 'costing_mode', '') or '').upper() == 'LANDED'
    if not landed:
        base = money_float(float(qty or 0) * float(getattr(item, 'price_at_time', 0) or 0))
        # A legacy frozen amount may already contain old freight.  Without a
        # landed-cost record we cannot honestly label that difference supplier
        # material; leave it in the explicit unattributed residual instead.
        supplier = amount if abs(amount - base) < 0.005 else 0.0
        return {'supplier_amount': supplier, 'landed_charge_amount': 0.0, 'charges': []}
    supplier = money_float(float(qty or 0) * float(getattr(item, 'price_at_time', 0) or 0))
    extra_minor = to_cents(amount) - to_cents(supplier)
    weights, charges = [], []
    for charge in active_charges(grn):
        treatment = (getattr(charge, 'cost_treatment', None) or 'INVENTORY').upper()
        if treatment != 'INVENTORY' or charge_effective_minor(charge) <= 0:
            continue
        try:
            allocation = json.loads(getattr(charge, 'allocation_json', None) or '{}')
            weight = int(allocation.get(str(item.id), 0) or 0)
        except (TypeError, ValueError, AttributeError):
            weight = 0
        if weight > 0:
            weights.append(weight)
            charges.append(charge)
    shares = allocate_cents(extra_minor, weights) if weights else []
    if not weights and extra_minor:
        # Historical landed lots may lack the allocation JSON.  Disclose the
        # amount as capitalised, but do not guess which charge supplied it.
        details = [{'id': None, 'type': 'OTHER', 'label': 'Unattributed landed cost', 'amount': cents_to_float(extra_minor)}]
    else:
        details = [
            {'id': charge.id, 'type': charge.charge_type,
             'label': charge.label or CHARGE_TYPE_LABELS.get(charge.charge_type, charge.charge_type),
             'amount': cents_to_float(share)}
            for charge, share in zip(charges, shares) if share
        ]
    return {'supplier_amount': supplier, 'landed_charge_amount': cents_to_float(extra_minor), 'charges': details}


def landed_cost_signature(grn):
    """Snapshot cost-affecting facts before an edit (no database writes).

    The receipt date is deliberately excluded: moving a locked lot's header
    date/time must stay possible and does not rewrite the frozen sale COGS.
    Quantities, supplier rates, landed line totals and capitalised charges are
    the facts that would change material cost.
    """
    return {
        'items': [(i.id, float(i.qty or 0), float(i.price_at_time or 0),
                   int(i.line_landed_cost_minor or 0)) for i in active_items(grn)],
        'charges': [(c.id, c.charge_type, charge_effective_minor(c), c.cost_treatment,
                     c.allocation_basis, c.allocation_json) for c in active_charges(grn)
                    if (c.cost_treatment or 'INVENTORY').upper() == 'INVENTORY'],
    }


def grn_has_consumed_quantity(grn):
    """Include booking/legacy dispatch consumption, not only allocation locks."""
    if any(bool(i.is_locked) for i in active_items(grn)):
        return True
    from app.services.grn_consumption import build_grn_consumption
    view = build_grn_consumption(grn_ids=[grn.id])
    return any(float(row.get('consumed_qty') or 0) > 0.000001
               for row in (view.get('items') or {}).values())


def get_or_create_party(name, role='other', actor=None):
    """Reuse the existing CashFlowParty master for non-supplier recipients."""
    clean = (name or '').strip()
    if not clean:
        return None
    party = CashFlowParty.query.filter(
        func.lower(func.trim(CashFlowParty.name)) == clean.lower()
    ).first()
    if party is None:
        party = CashFlowParty(
            name=clean,
            party_type='person',
            party_role=role,
            is_active=True,
            is_payee_active=True,
            created_by=actor,
        )
        db.session.add(party)
        db.session.flush()
    elif not getattr(party, 'party_role', None) or party.party_role == 'other':
        if role != 'other':
            party.party_role = role
    return party


def _line_minor(item):
    return _minor(float(getattr(item, 'qty', 0) or 0) * float(getattr(item, 'price_at_time', 0) or 0),
                  field='Material line')


def supplier_material_minor(grn):
    """Gross material amount only (qty × supplier rate)."""
    return sum(_line_minor(i) for i in active_items(grn))


def charge_billed_to_supplier(charge, grn=None):
    """The billing choice on this expense row, independent of all other rows.

    NULL means a pre-0005 row whose old whole-GRN choice has not been copied
    yet.  Once migrated (and for every new row) the choice is explicit.  The
    fallback keeps balances intact during a rolling deployment; it is never
    used to override an explicit unchecked row.
    """
    choice = getattr(charge, 'include_in_supplier', None)
    if choice is not None:
        return bool(choice)
    parent = grn if grn is not None else getattr(charge, 'grn', None)
    return bool(getattr(parent, 'include_charges_in_supplier', False))


def charge_supplier_billed_minor(charge, grn=None):
    """This row's contribution to the supplier bill; never a cost calculation."""
    treatment = (getattr(charge, 'cost_treatment', None) or 'INVENTORY').upper()
    amount = charge_effective_minor(charge)
    if (not charge_billed_to_supplier(charge, grn) or amount <= 0
            or treatment in ('NON_MATERIAL', 'UNTRACKED_LEGACY')):
        return 0
    return amount


def charges_billed_to_supplier(grn):
    """Whether at least one applicable expense is selected for supplier billing."""
    return any(charge_supplier_billed_minor(c, grn) > 0 for c in active_charges(grn))


def supplier_billed_charge_minor(grn):
    """Sum of selected applicable expenses that are STILL owed to the supplier.

    An expense ticked "Add to supplier ledger" can also be settled directly with
    its own recipient (Pay GRN Expenses → one box per expense).  Whatever has
    already been paid to the recipient is no longer a supplier payable, so the
    supplier bill shrinks by exactly that amount.  Nothing is ever payable
    twice: recipient payments and supplier payments never overlap.
    """
    total = 0
    for charge in active_charges(grn):
        billed = charge_supplier_billed_minor(charge, grn)
        if billed <= 0:
            continue
        total += max(0, billed - charge_recipient_paid_minor(charge))
    return total


def charge_recipient_paid_minor(charge):
    """Money already paid straight to an expense's own recipient."""
    if charge is None or getattr(charge, 'id', None) is None:
        return 0
    _primed_store()
    primed = _PRIMED_CHARGE_RECIPIENT_PAID.get(int(charge.id))
    if primed is not None:
        return primed
    rows = SupplierPayment.query.filter(
        SupplierPayment.grn_charge_id == charge.id,
        SupplierPayment.is_void == False,  # noqa: E712
    ).all()
    total = 0
    for row in rows:
        current = getattr(row, 'amount_minor', None)
        total += int(current) if current is not None else _minor(getattr(row, 'amount', 0))
    return total


def charge_recipient_open_minor(charge, grn=None):
    """What is still payable to an expense's own recipient.

    Every applicable expense has its own payable (owner rule: each expense gets
    its own money field).  A ticked expense is owed to the supplier only up to
    the part that has not been paid to the recipient directly.
    """
    if bool(getattr(charge, 'is_void', False)):
        return 0
    if (getattr(charge, 'cost_treatment', None) or 'INVENTORY').upper() in ('NON_MATERIAL', 'UNTRACKED_LEGACY'):
        return 0
    amount = charge_effective_minor(charge)
    if amount <= 0:
        return 0
    return max(0, amount - charge_recipient_paid_minor(charge))


def _supplier_expense_paid_minor(grn):
    """Supplier-side expense money actually paid in Accounts (never capped).

    Mirrors the expenses branch of :func:`supplier_obligation_paid_minor` but
    without the clamp, so it can be used *inside* the supplier-bill calculation
    without recursion.
    """
    if grn is None or not getattr(grn, 'id', None):
        return 0
    material_paid = 0
    expense_paid = 0
    lumped = 0
    for payment in _payment_rows(grn):
        if not _is_supplier_payment(payment):
            continue
        amount = int(getattr(payment, 'amount_minor', None)
                     if getattr(payment, 'amount_minor', None) is not None
                     else _minor(getattr(payment, 'amount', 0)))
        if amount <= 0:
            continue
        kind = (getattr(payment, 'payment_kind', None) or '').strip().upper()
        if kind == 'GRN_EXPENSES':
            expense_paid += amount
        elif kind == 'ADVANCE' and getattr(payment, 'grn_charge_id', None) is None:
            material_paid += amount
        elif getattr(payment, 'grn_charge_id', None) is None and kind in ('GRN_MATERIAL', '', 'LEGACY_GRN_AUTO'):
            material_paid += amount
        else:
            lumped += amount
    leftover = max(0, lumped - max(0, supplier_material_minor(grn) - material_paid))
    return expense_paid + leftover


def expense_route_allocation(grn):
    """How each GRN expense has been settled, and what is still open.

    Returns ``{charge_id: row}`` where ``row`` carries the expense amount, the
    money already paid straight to its own recipient, the share of supplier
    payments that has settled it (for expenses ticked "Add to supplier ledger"),
    and the remaining payable.

    Supplier expense payments are allocated to the ticked expenses oldest row
    first.  That keeps one rupee in exactly one place: an expense is never both
    a supplier payable and a recipient payable, whichever screen paid it.
    """
    rows = {}
    charges = active_charges(grn)
    ticked = []
    for charge in charges:
        amount = charge_effective_minor(charge)
        treatment = (getattr(charge, 'cost_treatment', None) or 'INVENTORY').upper()
        applicable = (amount > 0 and treatment not in ('NON_MATERIAL', 'UNTRACKED_LEGACY')
                      and (getattr(charge, 'declared_status', None) or 'UNPAID').upper() != 'NOT_APPLICABLE')
        recipient_paid = charge_recipient_paid_minor(charge)
        billed = charge_supplier_billed_minor(charge, grn) > 0
        rows[charge.id] = {
            'charge_id': charge.id,
            'amount_minor': amount,
            'applicable': applicable,
            'billed_to_supplier': billed,
            'recipient_paid_minor': recipient_paid,
            'supplier_paid_minor': 0,
            'remaining_minor': max(0, amount - recipient_paid) if applicable else 0,
        }
        if applicable and billed:
            ticked.append(charge.id)
    remaining_supplier_money = _supplier_expense_paid_minor(grn)
    for charge_id in ticked:
        if remaining_supplier_money <= 0:
            break
        row = rows[charge_id]
        applied = min(remaining_supplier_money, row['remaining_minor'])
        row['supplier_paid_minor'] = applied
        row['remaining_minor'] -= applied
        remaining_supplier_money -= applied
    for row in rows.values():
        row['paid_minor'] = row['recipient_paid_minor'] + row['supplier_paid_minor']
    return rows


def supplier_ledger_credit_minor(grn):
    """Single source of truth for Supplier Ledger / Accounts payable credit."""
    return supplier_material_minor(grn) + supplier_billed_charge_minor(grn)


def parse_obligation_key(obligation_key):
    """Split ``"<HEAD>[:<id>]"`` into ``(head, id)``; never raises."""
    key = (obligation_key or '').strip()
    if not key:
        return '', None
    head, _, tail = key.partition(':')
    try:
        tail_id = int(tail) if tail.strip() else None
    except (TypeError, ValueError):
        tail_id = None
    return head.strip().upper(), tail_id


def make_obligation_key(kind, grn_id=None):
    prefix = OBLIGATION_PREFIX.get(kind, kind)
    return f'{prefix}:{grn_id}' if grn_id is not None else prefix


def supplier_obligation_paid_minor(grn, obligation_kind):
    """How much of ONE supplier obligation has actually been paid.

    Payments are allocated in a fixed order, so a single cash voucher can
    never settle two obligations and the same rupee can never pay twice:

      * ``MATERIAL``  -> material-only payments, then any older lumped
                         ``SUPPLIER`` voucher (unapplied part only);
      * ``EXPENSES``  -> expense-only payments, then any remaining lumped
                         voucher (after material has taken its share);
      * ``SUPPLIER`` (legacy lumped) -> everything paid to the supplier.

    The three figures always add up to :func:`supplier_ledger_credit_minor`.
    """
    rows = _payment_rows(grn) if getattr(grn, 'id', None) else []
    supplier_rows = [p for p in rows if _is_supplier_payment(p)]
    material_paid = 0
    expense_paid = 0
    lumped = 0
    for p in supplier_rows:
        amount = int(getattr(p, 'amount_minor', None)
                     if getattr(p, 'amount_minor', None) is not None
                     else _minor(getattr(p, 'amount', 0)))
        if amount <= 0:
            continue
        kind = (getattr(p, 'payment_kind', None) or '').strip().upper()
        if kind == 'GRN_EXPENSES':
            expense_paid += amount
        elif kind == 'ADVANCE' and getattr(p, 'grn_charge_id', None) is None:
            material_paid += amount
        elif getattr(p, 'grn_charge_id', None) is None and kind in ('GRN_MATERIAL', '', 'LEGACY_GRN_AUTO'):
            material_paid += amount
        else:
            lumped += amount
    supplier_minor = supplier_material_minor(grn)
    billed_minor = supplier_billed_charge_minor(grn)
    if obligation_kind == OBLIGATION_MATERIAL:
        return min(material_paid, supplier_minor)
    if obligation_kind == OBLIGATION_EXPENSES:
        # Older lumped vouchers paid the material part first.  The per-expense
        # allocation then spreads that money over the ticked expenses, so an
        # expense paid straight to its own recipient can never be paid twice.
        allocation = expense_route_allocation(grn)
        applied = sum(row['supplier_paid_minor'] for row in allocation.values())
        return min(applied, billed_minor)
    # The legacy lumped obligation (and any "whole supplier bill" query).
    return min(material_paid + lumped + expense_paid, supplier_minor + billed_minor)


def _charge_minor(charge):
    current = getattr(charge, 'amount_minor', None)
    if current is None:
        return _minor(getattr(charge, 'amount', 0), field='Charge amount')
    return int(current)


def _bulk_cash_paid(grn_ids):
    """{grn_id: minor} of money that left a cash/bank account, in one query."""
    ids = [int(i) for i in grn_ids if i]
    totals = {grn_id: 0 for grn_id in ids}
    if not ids:
        return totals
    rows = AccountTransaction.query.filter(
        AccountTransaction.grn_id.in_(ids),
        AccountTransaction.is_void == False,  # noqa: E712
        AccountTransaction.from_account_id.isnot(None),
    ).all()
    for tx in rows:
        current = getattr(tx, 'amount_minor', None)
        amount = int(current) if current is not None else _minor(getattr(tx, 'amount', 0))
        totals[int(tx.grn_id)] = totals.get(int(tx.grn_id), 0) + amount
    return totals


def _cash_paid_minor(grn):
    """Money that ACTUALLY left a cash/bank account for this GRN."""
    grn_id = getattr(grn, 'id', None)
    if grn_id:
        _primed_store()
        if int(grn_id) in _PRIMED_CASH_PAID:
            return _PRIMED_CASH_PAID[int(grn_id)]
    rows = AccountTransaction.query.filter(
        AccountTransaction.grn_id == grn.id,
        AccountTransaction.is_void == False,  # noqa: E712
        AccountTransaction.from_account_id.isnot(None),
    ).all()
    total = 0
    for tx in rows:
        total += int(getattr(tx, 'amount_minor', None) if getattr(tx, 'amount_minor', None) is not None
                     else _minor(getattr(tx, 'amount', 0)))
    return total


def _payment_rows(grn):
    grn_id = getattr(grn, 'id', None)
    if grn_id:
        _primed_store()
        if int(grn_id) in _PRIMED_PAYMENTS:
            return list(_PRIMED_PAYMENTS[int(grn_id)])
    q = SupplierPayment.query.filter(
        SupplierPayment.grn_id == grn.id,
        SupplierPayment.is_void == False,  # noqa: E712
    )
    rows = list(q.all())
    # Legacy GRNs used a marker-only auto payment without grn_id.  Keep those
    # attached so historical balances stay intact without rewriting them.
    legacy = _legacy_marked_payments(grn.id)
    seen = {r.id for r in rows}
    for r in legacy:
        if r.id not in seen:
            rows.append(r)
    return rows


def _is_supplier_payment(row):
    kind = (getattr(row, 'payment_kind', None) or '').strip().upper()
    recipient = (getattr(row, 'recipient_type', None) or '').strip().upper()
    if kind == 'GRN_CHARGE':
        return False
    # NULL recipient_type means a legacy/standalone supplier payment.
    return recipient in ('', 'SUPPLIER')


def payment_obligation_label(payment, grn=None):
    """Plain-language name of what one payment settled (display only)."""
    if payment is None:
        return ''
    if (getattr(payment, 'grn_charge_id', None) or getattr(payment, 'payment_kind', '') == 'GRN_CHARGE'):
        charge = getattr(payment, 'grn_charge', None)
        if charge is not None:
            base = CHARGE_TYPE_LABELS.get(getattr(charge, 'charge_type', ''), 'GRN charge')
            label = getattr(charge, 'label', '') or getattr(payment, 'recipient_name', '')
            return f'{base} — {label}'.strip(' —') if label else base
        return getattr(payment, 'recipient_name', '') or 'GRN charge'
    kind = (getattr(payment, 'payment_kind', None) or '').strip().upper()
    if kind == 'GRN_EXPENSES':
        return OBLIGATION_LABEL[OBLIGATION_EXPENSES]
    if kind == 'ADVANCE':
        return 'Supplier advance'
    if kind == 'LEGACY_GRN_AUTO':
        return 'Legacy GRN payment'
    return OBLIGATION_LABEL[OBLIGATION_MATERIAL]


def grn_breakdown(grn):
    """One immutable breakdown used by every screen, report and export."""
    grn_id = getattr(grn, 'id', None)
    items = active_items(grn)
    charges = active_charges(grn)
    payments = _payment_rows(grn) if grn_id else []

    line_minors = [_line_minor(i) for i in items]
    supplier_minor = sum(line_minors)
    billed_charge_minor = supplier_billed_charge_minor(grn)
    supplier_bill_minor = supplier_minor + billed_charge_minor
    include_charges = charges_billed_to_supplier(grn)

    charge_rows = []
    capitalised_minor = 0
    expense_minor = 0
    route_allocation = expense_route_allocation(grn) if grn_id else {}
    for c in charges:
        amount = charge_effective_minor(c)
        treatment = (getattr(c, 'cost_treatment', None) or 'INVENTORY').upper()
        status = (getattr(c, 'declared_status', None) or 'UNPAID').upper()
        if treatment == 'INVENTORY' and amount > 0:
            capitalised_minor += amount
        if treatment == 'EXPENSE' and amount > 0:
            expense_minor += amount
        paid = sum(
            int(getattr(p, 'amount_minor', None) if getattr(p, 'amount_minor', None) is not None
                else _minor(getattr(p, 'amount', 0)))
            for p in payments if getattr(p, 'grn_charge_id', None) == c.id
        )
        applicable = amount > 0 and status != 'NOT_APPLICABLE' and treatment != 'NON_MATERIAL'
        allocation = route_allocation.get(c.id) or {}
        supplier_share_paid = int(allocation.get('supplier_paid_minor', 0) or 0)
        settled = paid + supplier_share_paid
        open_minor = max(0, amount - settled) if applicable else 0
        charge_rows.append({
            'id': c.id,
            'charge_type': c.charge_type,
            'charge_label': CHARGE_TYPE_LABELS.get(c.charge_type, c.charge_type),
            'label': getattr(c, 'label', '') or '',
            'amount': _f(amount),
            'amount_minor': amount,
            'recipient_type': c.recipient_type,
            'recipient_label': RECIPIENT_LABELS.get(c.recipient_type, c.recipient_type),
            'recipient_party_id': getattr(c, 'recipient_party_id', None),
            'recipient_name': getattr(c, 'recipient_name', '') or '',
            'declared_status': status,
            'cost_treatment': treatment,
            'allocation_basis': getattr(c, 'allocation_basis', 'VALUE'),
            'allocation_json': getattr(c, 'allocation_json', None),
            'is_legacy_mirror': bool(getattr(c, 'is_legacy_mirror', False)),
            'paid': _f(paid),
            'paid_minor': paid,
            # Every expense is its own payable: what is left after BOTH its own
            # recipient payments and the supplier's expense payments.
            'remaining': _f(open_minor),
            'remaining_minor': open_minor,
            'status': ('Paid' if amount > 0 and open_minor <= 0 and settled > 0 else
                       ('Partially Paid' if settled > 0 else 'Unpaid')),
            'applicable': applicable,
            'include_in_supplier': charge_billed_to_supplier(c, grn),
            'included_in_supplier_bill': charge_supplier_billed_minor(c, grn) > 0,
            # Where the money went / is owed, so no screen has to guess.
            'recipient_paid': _f(paid),
            'recipient_paid_minor': paid,
            'supplier_paid': _f(supplier_share_paid),
            'supplier_paid_minor': supplier_share_paid,
            'supplier_open_minor': max(0, amount - settled) if charge_supplier_billed_minor(c, grn) > 0 else 0,
        })

    supplier_paid = sum(
        int(getattr(p, 'amount_minor', None) if getattr(p, 'amount_minor', None) is not None
            else _minor(getattr(p, 'amount', 0)))
        for p in payments if _is_supplier_payment(p) and getattr(p, 'grn_charge_id', None) is None
    )
    # Legacy GRNs without a canonical payment row keep their historic read-side
    # debit; LANDED GRNs never have a paid_amount (A1).
    legacy_paid_minor = 0
    if (getattr(grn, 'costing_mode', 'LEGACY') or 'LEGACY') == 'LEGACY':
        has_auto = any((getattr(p, 'payment_kind', None) or '').upper() == 'LEGACY_GRN_AUTO'
                       or (getattr(p, 'source_type', None) or '').upper() == 'GRN'
                       for p in payments)
        if not has_auto and float(getattr(grn, 'paid_amount', 0) or 0) > 0:
            legacy_paid_minor = _minor(getattr(grn, 'paid_amount', 0))
    supplier_paid += legacy_paid_minor

    charge_paid = sum(r['paid_minor'] for r in charge_rows)
    landed_minor = supplier_minor + capitalised_minor
    cash_paid_minor = _cash_paid_minor(grn) if grn_id else 0
    total_qty = sum(float(getattr(i, 'qty', 0) or 0) for i in items)
    units = {str((getattr(i, 'mat_name', '') or '')).strip() for i in items}
    # A single "average rate" only makes sense for one material/one unit.
    avg_rate = (float(from_minor(landed_minor)) / total_qty) if total_qty > 0 else 0.0

    material_paid_minor = supplier_obligation_paid_minor(grn, OBLIGATION_MATERIAL)
    expenses_paid_minor = supplier_obligation_paid_minor(grn, OBLIGATION_EXPENSES)

    def _supplier_obligation(kind, original_minor, paid_minor, label, hint):
        return {
            'key': make_obligation_key(kind, grn_id),
            'kind': kind,
            'charge_id': None,
            'label': label,
            'hint': hint,
            'recipient_type': 'SUPPLIER',
            'recipient_label': 'Supplier',
            'recipient_name': getattr(grn, 'supplier', '') or '',
            'recipient_party_id': None,
            'original': _f(original_minor),
            'original_minor': original_minor,
            'paid': _f(paid_minor),
            'paid_minor': paid_minor,
            'remaining': _f(max(0, original_minor - paid_minor)),
            'remaining_minor': max(0, original_minor - paid_minor),
            'status': ('Paid' if paid_minor >= original_minor and original_minor > 0 else
                       ('Partially Paid' if paid_minor > 0 else 'Unpaid')),
            # The two parts of this payee's total, so a lumped legacy voucher
            # can be booked against whichever part is still open.
            'material_original_minor': supplier_minor,
            'expenses_original_minor': billed_charge_minor,
        }

    # A3: stock value and supplier-billed GRN expenses are SEPARATE payables,
    # so the operator can pay the material now and the expenses later (or the
    # other way round) without either being silently settled by the other.
    obligations = [
        _supplier_obligation(
            OBLIGATION_MATERIAL, supplier_minor, material_paid_minor,
            'Stock / material amount',
            'Qty × purchase rate only — never a GRN expense.',
        ),
    ]
    if billed_charge_minor > 0:
        obligations.append(_supplier_obligation(
            OBLIGATION_EXPENSES, billed_charge_minor, expenses_paid_minor,
            'GRN expenses billed to supplier',
            'Only the charges ticked "Add to supplier ledger" on this GRN.',
        ))
    # Owner rule: EVERY GRN expense is its own payable with its own money field,
    # including the ones ticked "Add to supplier ledger".  Such an expense is
    # owed to the supplier only up to the part that has not been settled with
    # its own recipient, so the same rupee is never payable twice.
    for row in charge_rows:
        if not row['applicable'] or row['amount_minor'] <= 0:
            continue
        also_on_supplier_bill = bool(row['included_in_supplier_bill'])
        if also_on_supplier_bill:
            hint = ('Ticked "Add to supplier ledger": owed to the supplier, and payable '
                    'directly to this recipient too — whichever pays first removes it from the other.')
        else:
            hint = 'Not added to the supplier ledger: paid only to this recipient.'
        obligations.append({
            'key': make_obligation_key(OBLIGATION_CHARGE, row['id']),
            'kind': 'CHARGE',
            'charge_id': row['id'],
            'label': f"{row['charge_label']}{(' — ' + row['label']) if row['label'] else ''}",
            'hint': hint,
            'recipient_type': row['recipient_type'],
            'recipient_label': row['recipient_label'],
            'recipient_name': row['recipient_name'],
            'recipient_party_id': row['recipient_party_id'],
            'original': row['amount'],
            'original_minor': row['amount_minor'],
            'paid': row['paid'],
            'paid_minor': row['paid_minor'],
            'remaining': row['remaining'],
            'remaining_minor': row['remaining_minor'],
            'status': row['status'],
            'cost_treatment': row['cost_treatment'],
            # Which ledger this expense is ALSO owed through (read-only facts).
            'billed_to_supplier': also_on_supplier_bill,
            'supplier_paid': row['supplier_paid'],
            'supplier_paid_minor': row['supplier_paid_minor'],
            'recipient_paid': row['recipient_paid'],
            'recipient_paid_minor': row['recipient_paid_minor'],
        })

    per_line = []
    for idx, item in enumerate(items):
        per_line.append({
            'id': item.id,
            'material': item.mat_name,
            'qty': float(getattr(item, 'qty', 0) or 0),
            'supplier_rate': float(getattr(item, 'price_at_time', 0) or 0),
            'supplier_amount': _f(line_minors[idx]),
            'allocated_charges': _f(int(getattr(item, 'allocated_charges_minor', 0) or 0)),
            'landed_unit_cost': round(lot_cost_rate(item), 6),
            'line_landed_cost': _f(int(getattr(item, 'line_landed_cost_minor', 0) or 0)),
        })

    total_obligations = supplier_bill_minor + sum(
        r['amount_minor'] for r in charge_rows if r['applicable'] and not r['included_in_supplier_bill']
    )
    # One rupee, one place.  A ticked expense is counted inside the supplier
    # EXPENSES payable until it is settled with its recipient; from then on the
    # supplier payable shrinks and the expense box is what stays open.  So the
    # GRN total never counts the same expense twice.
    for ob in obligations:
        if ob['kind'] == OBLIGATION_CHARGE and ob.get('billed_to_supplier'):
            ob['counted_in_total_minor'] = 0
        else:
            ob['counted_in_total_minor'] = ob['remaining_minor']
    total_remaining = sum(o['counted_in_total_minor'] for o in obligations)

    return {
        'grn_id': grn_id,
        'bill_ref': (getattr(grn, 'manual_bill_no', None) or getattr(grn, 'auto_bill_no', None)
                     or f"GRN-{grn_id or ''}"),
        'supplier': getattr(grn, 'supplier', '') or '',
        'supplier_id': getattr(grn, 'supplier_id', None),
        'costing_mode': (getattr(grn, 'costing_mode', 'LEGACY') or 'LEGACY'),
        'supplier_material_amount': _f(supplier_minor),
        'supplier_material_amount_minor': supplier_minor,
        'include_charges_in_supplier': include_charges,
        'supplier_billed_charges': _f(billed_charge_minor),
        'supplier_billed_charges_minor': billed_charge_minor,
        'supplier_billed_charge_count': sum(r['included_in_supplier_bill'] for r in charge_rows),
        'supplier_bill_amount': _f(supplier_bill_minor),
        'supplier_bill_amount_minor': supplier_bill_minor,
        'charges': charge_rows,
        'vehicle_fare': _f(sum(r['amount_minor'] for r in charge_rows if r['charge_type'] in ('VEHICLE_FARE', 'TRANSPORT'))),
        'loading': _f(sum(r['amount_minor'] for r in charge_rows if r['charge_type'] == 'LOADING')),
        'unloading': _f(sum(r['amount_minor'] for r in charge_rows if r['charge_type'] == 'UNLOADING')),
        'other_charges': _f(sum(r['amount_minor'] for r in charge_rows if r['charge_type'] == 'OTHER')),
        'capitalised_charge_total': _f(capitalised_minor),
        'capitalised_charge_total_minor': capitalised_minor,
        'expense_charge_total': _f(expense_minor),
        'expense_charge_total_minor': expense_minor,
        'landed_cost_total': _f(landed_minor),
        'landed_cost_total_minor': landed_minor,
        'total_qty': total_qty,
        'average_actual_rate': round(avg_rate, 6),
        'single_unit': len(units) <= 1,
        'per_line': per_line,
        'supplier_paid': _f(supplier_paid),
        'supplier_paid_minor': supplier_paid,
        'supplier_remaining': _f(max(0, supplier_bill_minor - supplier_paid)),
        # A3: the two supplier payables of this GRN, kept separately.
        'material_original': _f(supplier_minor),
        'material_paid': _f(material_paid_minor),
        'material_remaining': _f(max(0, supplier_minor - material_paid_minor)),
        'expenses_original': _f(billed_charge_minor),
        'expenses_paid': _f(expenses_paid_minor),
        'expenses_remaining': _f(max(0, billed_charge_minor - expenses_paid_minor)),
        'additional_paid': _f(charge_paid),
        # Expenses the supplier ledger does not carry: unticked rows only.
        'additional_remaining': _f(sum(r['remaining_minor'] for r in charge_rows
                                       if r['applicable'] and not r['included_in_supplier_bill'])),
        # Every expense still open, whoever it is owed to (pay-box display).
        'recipient_expenses_remaining': _f(sum(r['remaining_minor'] for r in charge_rows if r['applicable'])),
        'total_obligations': _f(total_obligations),
        'total_remaining': _f(total_remaining),
        'total_cash_paid': _f(cash_paid_minor),
        'total_cash_paid_minor': cash_paid_minor,
        'obligations': obligations,
        'payment_rows': [
            {
                'id': p.id,
                'amount': float(getattr(p, 'amount', 0) or 0),
                'date_posted': getattr(p, 'date_posted', None),
                'method': getattr(p, 'method', '') or '',
                'reference': getattr(p, 'reference', None) or getattr(p, 'manual_bill_no', '') or '',
                'recipient_type': getattr(p, 'recipient_type', None) or 'SUPPLIER',
                'recipient_name': getattr(p, 'recipient_name', '') or getattr(grn, 'supplier', '') or '',
                'charge_id': getattr(p, 'grn_charge_id', None),
                'payment_kind': getattr(p, 'payment_kind', None) or '',
                # What the operator sees: material / supplier expenses / one
                # recipient charge. Never a document type code.
                'obligation_label': payment_obligation_label(p, grn),
                'cash_posted': bool(getattr(p, 'cash_posted', False)),
                'is_void': bool(getattr(p, 'is_void', False)),
                'note': getattr(p, 'note', '') or '',
            }
            for p in sorted(payments, key=lambda r: (getattr(r, 'date_posted', None) or '', r.id))
        ],
        # memo-only legacy fields (A2: never posted to a ledger or to inventory)
        'tax_amount': float(getattr(grn, 'tax_amount', 0) or 0),
        'discount': float(getattr(grn, 'discount', 0) or 0),
        'adjustment_amount': float(getattr(grn, 'adjustment_amount', 0) or 0),
    }


def grn_expense_payment_rows(grn, breakdown=None):
    """Every GRN expense as one payment row: Total, Paid and To pay.

    This is the **one-by-one expense payment system** (owner rule A3): a vehicle
    fare, a loading charge, an unloading charge or any other expense the
    operator entered on the GRN is paid on its own, to its own recipient, with
    its own amount field.  Nothing here is stock: no quantity, no rate and no
    supplier material amount ever appears on these rows.

    Two payment routes may settle one expense, and they never add up twice:

      * ``to_supplier``  — only when the row is ticked "Add to supplier ledger",
        the part the supplier bill still carries;
      * ``to_recipient`` — always, paid straight to the expense's recipient.

    ``paid`` is the money already spent on THIS expense through either route,
    so ``original = paid + remaining`` holds for every row.

    ``payment_rows`` are the unticked expenses only — the ones the supplier
    ledger does not carry at all.  ``supplier_billed_rows`` lists the ticked
    ones, and those are settled through the supplier bill instead.

    Every figure comes from :func:`grn_breakdown`; this helper only selects and
    renames, so no screen can invent its own version of an expense payable.
    """
    bd = breakdown if breakdown is not None else grn_breakdown(grn)
    rows = []
    for ob in bd.get('obligations') or []:
        if ob.get('kind') != OBLIGATION_CHARGE:
            continue
        remaining_minor = int(ob.get('remaining_minor') or 0)
        billed = bool(ob.get('billed_to_supplier'))
        rows.append({
            'key': ob.get('key'),
            'charge_id': ob.get('charge_id'),
            'label': ob.get('label') or 'GRN expense',
            'detail': ob.get('hint') or '',
            'recipient_type': ob.get('recipient_type') or 'OTHER',
            'recipient_label': ob.get('recipient_label') or '',
            'recipient_name': ob.get('recipient_name') or '',
            'cost_treatment': ob.get('cost_treatment') or 'INVENTORY',
            'total': ob.get('original') or 0.0,
            'total_minor': int(ob.get('original_minor') or 0),
            'paid': ob.get('paid') or 0.0,
            'paid_minor': int(ob.get('paid_minor') or 0),
            'remaining': ob.get('remaining') or 0.0,
            'remaining_minor': remaining_minor,
            'status': ob.get('status') or 'Unpaid',
            'billed_to_supplier': billed,
            'supplier_paid': ob.get('supplier_paid') or 0.0,
            'recipient_paid': ob.get('recipient_paid') or 0.0,
            # Where each rupee of this expense can still go.
            'to_supplier': (ob.get('remaining') or 0.0) if billed else 0.0,
            'to_recipient': ob.get('remaining') or 0.0,
        })
    payment_rows = [row for row in rows if not row['billed_to_supplier']]
    supplier_rows = [row for row in rows if row['billed_to_supplier']]
    return {
        'grn_id': bd.get('grn_id'),
        'bill_ref': bd.get('bill_ref') or '',
        'supplier': bd.get('supplier') or '',
        'date_posted': getattr(grn, 'date_posted', None),
        # The per-expense system itself: expenses the supplier ledger does NOT carry.
        'payment_rows': payment_rows,
        'payment_total': _f(sum(row['total_minor'] for row in payment_rows)),
        'payment_paid': _f(sum(row['paid_minor'] for row in payment_rows)),
        'payment_remaining': _f(sum(row['remaining_minor'] for row in payment_rows)),
        # Ticked expenses: settled by the supplier bill, listed for completeness.
        'supplier_billed_rows': supplier_rows,
        'supplier_billed_total': _f(sum(row['total_minor'] for row in supplier_rows)),
        'supplier_billed_paid': _f(sum(row['paid_minor'] for row in supplier_rows)),
        'supplier_billed_remaining': _f(sum(row['remaining_minor'] for row in supplier_rows)),
        'supplier_ledger_due': (bd.get('material_remaining', 0.0) or 0.0)
                                + (bd.get('expenses_remaining', 0.0) or 0.0),
        'material_remaining': bd.get('material_remaining', 0.0) or 0.0,
        'total_remaining': bd.get('total_remaining', 0.0) or 0.0,
    }


def supplier_bill_breakdown(grn):
    """One GRN's supplier payables, split the way the operator pays them (A3).

    Returns the four numbers the Accounts screens show side by side:

      * ``material``   stock only (Σ qty × rate);
      * ``expenses``   GRN costs ticked "Add to supplier ledger";
      * ``charges``    unticked expiry costs owed to their own recipients;
      * ``paid`` / ``remaining`` for each, from the one allocation authority.

    This is a projection: it never writes and never changes the supplier
    ledger total, it only explains it.
    """
    breakdown = grn_breakdown(grn)
    material = next((o for o in breakdown['obligations'] if o['kind'] == OBLIGATION_MATERIAL), None)
    expenses = next((o for o in breakdown['obligations'] if o['kind'] == OBLIGATION_EXPENSES), None)
    # Expenses the supplier ledger does NOT carry (unticked rows).  Every
    # expense also has its own box — see ``grn_breakdown()['obligations']``.
    recipient_charges = [o for o in breakdown['obligations']
                         if o['kind'] == OBLIGATION_CHARGE and not o.get('billed_to_supplier')]
    return {
        'grn_id': breakdown.get('grn_id'),
        'bill_ref': breakdown.get('bill_ref'),
        'supplier': breakdown.get('supplier'),
        'supplier_id': breakdown.get('supplier_id'),
        'date_posted': getattr(grn, 'date_posted', None),
        'material': material,
        'expenses': expenses,
        'recipient_charges': recipient_charges,
        # Ticked expenses: owed to the supplier, but each also has its own box
        # so the operator can pay the recipient directly (the supplier bill
        # then drops by exactly that amount).
        'supplier_expense_charges': [o for o in breakdown['obligations']
                                     if o['kind'] == OBLIGATION_CHARGE
                                     and o.get('billed_to_supplier')],
        'material_original': breakdown['material_original'],
        'material_paid': breakdown['material_paid'],
        'material_remaining': breakdown['material_remaining'],
        'expenses_original': breakdown['expenses_original'],
        'expenses_paid': breakdown['expenses_paid'],
        'expenses_remaining': breakdown['expenses_remaining'],
        'recipient_original': _f(sum(o['original_minor'] for o in recipient_charges)),
        'recipient_paid': _f(sum(o['paid_minor'] for o in recipient_charges)),
        'recipient_remaining': _f(sum(o['remaining_minor'] for o in recipient_charges)),
        'supplier_bill_amount': breakdown['supplier_bill_amount'],
        'supplier_remaining': breakdown['supplier_remaining'],
        'landed_cost_total': breakdown['landed_cost_total'],
        'total_remaining': breakdown['total_remaining'],
        'supplier_billed_charge_count': breakdown['supplier_billed_charge_count'],
        'payment_rows': breakdown['payment_rows'],
        'charged_to_supplier': breakdown['charges'],
    }


def _weights_for_charge(charge, items, line_minors):
    basis = (getattr(charge, 'allocation_basis', None) or 'VALUE').upper()
    if basis == 'QUANTITY':
        return [max(0.0, float(getattr(i, 'qty', 0) or 0)) for i in items], 'QUANTITY'
    if basis == 'MANUAL':
        try:
            raw = json.loads(getattr(charge, 'allocation_json', None) or '{}')
            weights = [int(raw.get(str(i.id), raw.get(i.id, 0)) or 0) for i in items]
            if sum(weights) == _charge_minor(charge) and all(w >= 0 for w in weights):
                return weights, 'MANUAL'
        except (TypeError, ValueError):
            pass
        logger.warning('GRN charge %s MANUAL allocation invalid; using VALUE', getattr(charge, 'id', None))
    return [max(0, m) for m in line_minors], 'VALUE'


def recompute_landed_costs(grn, *, actor=None):
    """Allocate INVENTORY charges to GRN lots and store exact landed costs.

    Charges flagged EXPENSE / NON_MATERIAL / UNTRACKED_LEGACY are never
    allocated, so they can never be counted twice (inventory AND expense).
    """
    items = active_items(grn)
    charges = active_charges(grn)
    line_minors = [_line_minor(i) for i in items]

    allocated = {i.id: 0 for i in items}
    capitalised = 0
    expense_total = 0
    for c in charges:
        amount = charge_effective_minor(c)
        treatment = (getattr(c, 'cost_treatment', None) or 'INVENTORY').upper()
        status = (getattr(c, 'declared_status', None) or 'UNPAID').upper()
        if amount <= 0 or status == 'NOT_APPLICABLE' or treatment in ('NON_MATERIAL', 'UNTRACKED_LEGACY'):
            continue
        if treatment == 'EXPENSE':
            expense_total += amount
            continue
        capitalised += amount
        weights, basis = _weights_for_charge(c, items, line_minors)
        shares = allocate_cents(amount, weights) if items else []
        c.allocation_json = json.dumps({str(i.id): int(s) for i, s in zip(items, shares)})
        c.allocation_basis = basis
        for i, share in zip(items, shares):
            allocated[i.id] += int(share)

    supplier_minor = sum(line_minors)
    landed_minor = supplier_minor + capitalised
    for idx, item in enumerate(items):
        line_total = line_minors[idx] + allocated[item.id]
        qty = float(getattr(item, 'qty', 0) or 0)
        unit = 0.0
        if qty > 0:
            unit = float((Decimal(line_total) / Decimal('100') / Decimal(str(qty))).quantize(
                Decimal('0.000001'), rounding=ROUND_HALF_UP))
        item.base_rate_minor = _minor(getattr(item, 'price_at_time', 0), field='Supplier rate')
        item.allocated_charges_minor = allocated[item.id]
        item.line_landed_cost_minor = line_total
        item.landed_unit_cost = unit
        item.landed_unit_cost_minor = _minor(unit, field='Landed unit cost')
        item.allocation_basis = (getattr(grn, 'allocation_basis_default', None) or 'VALUE')

    grn.supplier_material_amount_minor = supplier_minor
    grn.supplier_material_amount = _f(supplier_minor)
    grn.capitalised_charge_total_minor = capitalised
    grn.capitalised_charge_total = _f(capitalised)
    grn.landed_cost_total_minor = landed_minor
    grn.landed_cost_total = _f(landed_minor)
    grn.expense_charge_total_minor = expense_total
    grn.expense_charge_total = _f(expense_total)
    if actor:
        grn.updated_by = actor
    return grn_breakdown(grn)


def mirror_legacy_charge_columns(grn):
    """Keep the deprecated flat columns as a cache only (never a second truth)."""
    by_type = {}
    for c in active_charges(grn):
        if bool(getattr(c, 'is_legacy_mirror', False)):
            continue
        by_type.setdefault((c.charge_type or 'OTHER').upper(), 0)
        by_type[(c.charge_type or 'OTHER').upper()] += _charge_minor(c)
    for column, types in LEGACY_MIRRORS:
        setattr(grn, column, _f(sum(by_type.get(t, 0) for t in types)))


def sync_charges_from_form(grn, form, *, actor=None, preserve_legacy=True):
    """Create/replace LANDED charge rows from the wizard's charge grid.

    ``preserve_legacy`` keeps the old scalar columns intact for LEGACY GRNs
    when the browser does not post them (historical safety).
    """
    if not preserve_legacy:
        pass
    types = form.getlist('charge_type[]')
    labels = form.getlist('charge_label[]')
    amounts = form.getlist('charge_amount[]')
    recipients = form.getlist('charge_recipient_type[]')
    party_names = form.getlist('charge_recipient[]')
    # A charge carries cost facts only.  There is no paid/unpaid field on the
    # grid any more (owner: every paid control lives in Accounts), so the old
    # declaration is gone from the form; a stored value is simply kept as-is
    # and never influences a cost or a balance.
    statuses = form.getlist('charge_status[]')
    treatments = form.getlist('charge_treatment[]')
    bases = form.getlist('charge_basis[]')
    charge_ids = form.getlist('charge_id[]')
    row_keys = form.getlist('charge_row_key[]')
    selected_keys = set(form.getlist('charge_include_in_supplier[]'))
    per_row_billing = 'charge_supplier_billing_present' in form
    legacy_billing = 'include_charges_in_supplier' in form
    legacy_include = (form.get('include_charges_in_supplier') or '').strip().lower() in ('1', 'true', 'on', 'yes')
    row_count = max(len(types), len(amounts), len(labels), len(row_keys) if per_row_billing else 0)

    def at(seq, idx, default=''):
        return (seq[idx] if idx < len(seq) else default) or default

    def row_key(idx):
        return at(row_keys, idx, f'row-{idx}').strip()

    def billing_choice(idx, stored=None):
        if per_row_billing:
            return row_key(idx) in selected_keys
        if legacy_billing:
            # Compatibility with callers using the retired whole-GRN form.
            return legacy_include
        if stored is not None:
            return charge_billed_to_supplier(stored, grn)
        return False

    if per_row_billing:
        keys = [row_key(idx) for idx in range(row_count)]
        if len(set(keys)) != len(keys):
            raise ValueError('Each GRN expense must have a unique row key. Reload the GRN and try again.')
        unknown = selected_keys - set(keys)
        if unknown:
            raise ValueError('A supplier-billing selection does not match an expense row. Reload the GRN and try again.')

    existing = {int(c.id): c for c in (grn.charges or [])}
    keep = set()

    # Refuse a billing-route change before writing any row if Accounts has
    # already settled any part of this GRN.  The route rolls back on ValueError.
    billing_changed = False
    submitted_ids = set()
    for idx in range(row_count):
        raw_id = at(charge_ids, idx, '')
        stored = existing.get(int(raw_id)) if str(raw_id).isdigit() else None
        if stored is not None and not stored.is_void:
            submitted_ids.add(int(stored.id))
            if billing_choice(idx, stored) != charge_billed_to_supplier(stored, grn):
                billing_changed = True
    # A removed selected expense also changes the supplier bill.
    for cid, stored in existing.items():
        if (cid not in submitted_ids and not stored.is_void and not stored.is_legacy_mirror
                and charge_billed_to_supplier(stored, grn)):
            billing_changed = True
    if billing_changed:
        has_payments = SupplierPayment.query.filter(
            SupplierPayment.grn_id == grn.id,
            SupplierPayment.is_void == False,  # noqa: E712
        ).first() is not None
        if has_payments:
            raise ValueError(
                'This GRN already has Accounts payments. Void those payments before '
                'changing which expenses are billed to the supplier.'
            )

    def _paid_minor_for(charge_id):
        rows = SupplierPayment.query.filter(
            SupplierPayment.grn_charge_id == charge_id,
            SupplierPayment.is_void == False,  # noqa: E712
        ).all()
        return sum(int(getattr(r, 'amount_minor', None) if getattr(r, 'amount_minor', None) is not None
                       else _minor(getattr(r, 'amount', 0))) for r in rows)
    for idx in range(row_count):
        raw_amount = at(amounts, idx, '0')
        try:
            amount_minor = _minor(float(str(raw_amount).replace(',', '').strip() or 0), field='Charge amount')
        except (TypeError, ValueError):
            raise ValueError(f"Charge #{idx + 1} has an invalid amount: '{raw_amount}'.")
        ctype = at(types, idx, 'OTHER').strip().upper()
        if ctype not in CHARGE_TYPES:
            ctype = 'OTHER'
        raw_id = at(charge_ids, idx, '')
        stored_charge = existing.get(int(raw_id)) if str(raw_id).isdigit() else None
        if statuses:
            status = at(statuses, idx, 'UNPAID').strip().upper()
            if status not in DECLARED_STATUSES:
                status = 'UNPAID'
        else:
            # Field removed from the GRN screen: never invent a declaration.
            status = (getattr(stored_charge, 'declared_status', None) or 'UNPAID').upper()
            if status not in DECLARED_STATUSES:
                status = 'UNPAID'
        if status == 'NOT_APPLICABLE':
            amount_minor = 0
        treatment = at(treatments, idx, 'INVENTORY').strip().upper()
        if treatment not in COST_TREATMENTS:
            treatment = 'INVENTORY'
        basis = at(bases, idx, 'VALUE').strip().upper()
        if basis not in ALLOCATION_BASES:
            basis = 'VALUE'
        recipient = at(recipients, idx, '').strip().upper()
        if recipient not in RECIPIENT_TYPES:
            recipient = DEFAULT_RECIPIENT_BY_CHARGE.get(ctype, 'OTHER')
        party_name = at(party_names, idx, '').strip()
        party = get_or_create_party(
            party_name,
            role=('transporter' if recipient == 'TRANSPORTER' else
                  'loading_labour' if recipient == 'LABOUR' else 'other'),
            actor=actor,
        ) if party_name and recipient != 'UNSPECIFIED' else None

        charge = stored_charge
        if charge is None and amount_minor > 0:
            charge = GRNCharge(grn_id=grn.id, created_by=actor)
            db.session.add(charge)
            try:
                grn.charges.append(charge)
            except Exception:
                pass
        if charge is None:
            continue
        # A charge that Accounts has already paid can still be corrected —
        # editing must go through (EDIT_POLICY.md).  The only impossible
        # numbers are: an amount below what was paid, and paying a *different*
        # recipient than the money already went to.  Increasing the amount,
        # renaming the expense or changing its cost treatment is allowed and
        # keeps the posted payment honest.
        if charge.id is not None:
            paid_minor = _paid_minor_for(int(charge.id))
            if paid_minor > 0:
                charge_label = charge.label or CHARGE_TYPE_LABELS.get(ctype, ctype)
                if amount_minor < paid_minor:
                    raise ValueError(
                        f"Charge '{charge_label}' already has Rs {paid_minor / 100:,.2f} paid in "
                        f"Accounts, so its amount cannot be set below that. Enter at least "
                        f"Rs {paid_minor / 100:,.2f}, or void that payment first. Nothing was saved."
                    )
                if (getattr(charge, 'recipient_type', None) or '').upper() != recipient:
                    raise ValueError(
                        f"Charge '{charge_label}' already has Rs {paid_minor / 100:,.2f} paid to "
                        "its current recipient. Void that payment before paying a different "
                        "recipient for this expense. Nothing was saved."
                    )
        charge.charge_type = ctype
        charge.label = at(labels, idx, '').strip()
        charge.amount_minor = amount_minor
        charge.amount = _f(amount_minor)
        charge.recipient_type = recipient
        charge.recipient_party_id = party.id if party else None
        charge.recipient_name = party.name if party else party_name
        charge.declared_status = status
        charge.cost_treatment = treatment
        charge.include_in_supplier = billing_choice(idx, stored_charge)
        charge.allocation_basis = basis
        charge.is_void = False
        charge.is_legacy_mirror = False
        charge.legacy_column = None
        charge.updated_by = actor
        charge.note = at(form.getlist('charge_note[]'), idx, '').strip()
        db.session.flush()
        keep.add(int(charge.id))

    for cid, charge in existing.items():
        if cid not in keep and not bool(getattr(charge, 'is_legacy_mirror', False)):
            if _paid_minor_for(cid) > 0:
                raise ValueError(
                    f"Charge '{charge.label or charge.charge_type}' already has payments "
                    "recorded in Accounts and cannot be removed. Void those payments first."
                )
            charge.is_void = True
    db.session.flush()
    return active_charges(grn)


def calculate_grn_total(grn):
    """Backwards-compatible document total.

    LANDED GRNs: the landed cost (material + capitalised charges).  LEGACY
    GRNs: the historical formula, unchanged, so old documents/prints/reports
    keep their original numbers.  This is NEVER used as a supplier payable
    (owner rule A2) — use ``grn_breakdown()['supplier_material_amount']``.
    """
    mode = (getattr(grn, 'costing_mode', 'LEGACY') or 'LEGACY').upper()
    if mode == 'LANDED':
        return grn_breakdown(grn)['landed_cost_total']
    item_total = sum(
        float(getattr(i, 'qty', 0) or 0) * float(getattr(i, 'price_at_time', 0) or 0)
        for i in (getattr(grn, 'items', None) or [])
        if not bool(getattr(i, 'is_void', False))
    )
    expenses = sum(float(getattr(grn, col, 0) or 0) for col in ('loading_cost', 'freight_cost', 'other_expense'))
    return (item_total + expenses + float(getattr(grn, 'tax_amount', 0) or 0)
            - float(getattr(grn, 'discount', 0) or 0)
            + float(getattr(grn, 'adjustment_amount', 0) or 0))


__all__ = [
    'CHARGE_TYPES', 'CHARGE_TYPE_LABELS', 'RECIPIENT_TYPES', 'RECIPIENT_LABELS',
    'DEFAULT_RECIPIENT_BY_CHARGE', 'COST_TREATMENTS', 'DECLARED_STATUSES',
    'ALLOCATION_BASES', 'active_items', 'active_charges', 'lot_cost_rate',
    'charge_effective_minor', 'lot_cost_components', 'landed_cost_signature', 'grn_has_consumed_quantity',
    'get_or_create_party', 'supplier_material_minor', 'charge_billed_to_supplier',
    'charge_supplier_billed_minor', 'charges_billed_to_supplier',
    'supplier_billed_charge_minor', 'supplier_ledger_credit_minor', 'grn_breakdown',
    'charge_recipient_paid_minor', 'charge_recipient_open_minor', 'expense_route_allocation',
    'parse_obligation_key', 'make_obligation_key', 'supplier_obligation_paid_minor',
    'supplier_bill_breakdown', 'grn_expense_payment_rows',
    'payment_obligation_label', 'OBLIGATION_SUPPLIER', 'OBLIGATION_MATERIAL',
    'OBLIGATION_EXPENSES', 'OBLIGATION_CHARGE', 'PAYMENT_KIND_BY_OBLIGATION',
    'OBLIGATION_LABEL',
    'recompute_landed_costs', 'mirror_legacy_charge_columns',
    'sync_charges_from_form', 'calculate_grn_total',
]
