"""Domain service module — extracted from legacy ERP core."""
from __future__ import annotations

import os
import io
import secrets
import json
import calendar
import threading
import time
import smtplib
import shutil
import sqlite3
import zipfile
import urllib.request
import urllib.error
import re
import logging
import importlib
from itertools import zip_longest
from urllib.parse import unquote
from contextlib import redirect_stderr
from email.message import EmailMessage
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from sqlalchemy import func, case, text, or_, and_, exists, not_
from sqlalchemy.orm import selectinload
from types import SimpleNamespace
from flask import (
    current_app as app,
    render_template, request, redirect, url_for, flash, jsonify,
    send_file, Response, make_response, send_from_directory,
    got_request_exception, abort, session, g,
)
from flask_login import login_user, login_required, logout_user, current_user

from models import *
from utils.audit import audit_log
from utils.reconciliation import run_auto_reconcile
from cash_flow_reconciliation_helpers import (
    create_reconciliation, update_reconciliation, delete_reconciliation,
    get_reconciliation_history, migrate_legacy_record,
)
from app.services import constants as C
from app.services import state

# === explicit service imports ===
from app.services.time_money import (
    _money_round,
    pk_now,
)


# Rebind constants used as bare names
OPEN_KHATA_CODE = C.OPEN_KHATA_CODE
OPEN_KHATA_NAME = C.OPEN_KHATA_NAME
PK_TZ = C.PK_TZ
SALE_CATEGORY_CHOICES = C.SALE_CATEGORY_CHOICES
_SALE_CATEGORY_ALIASES = C._SALE_CATEGORY_ALIASES
DOMAIN_WIPE_REGISTRY = C.DOMAIN_WIPE_REGISTRY
USER_PERMISSION_DEFAULTS = C.USER_PERMISSION_DEFAULTS
PERMISSION_LEGACY_FALLBACKS = C.PERMISSION_LEGACY_FALLBACKS
ENDPOINT_PERMISSION_MAP = C.ENDPOINT_PERMISSION_MAP
AUTO_BILL_NS_DEFAULT = C.AUTO_BILL_NS_DEFAULT
AUTO_BILL_NAMESPACES = C.AUTO_BILL_NAMESPACES
EDITABLE_USER_PERMISSION_FIELDS = C.EDITABLE_USER_PERMISSION_FIELDS
basedir = C.basedir
legacy_instance_dir = C.legacy_instance_dir
legacy_db_path = C.legacy_db_path
db_path = C.db_path
_DB_HEALTH_SNAPSHOT_PATH = C._DB_HEALTH_SNAPSHOT_PATH
_max_upload_mb = C._max_upload_mb
_AUTO_BACKUP_ENABLED = C._AUTO_BACKUP_ENABLED
_WIPE_BACKUP_ENABLED = C._WIPE_BACKUP_ENABLED
_AUTO_RECONCILE_ENABLED = C._AUTO_RECONCILE_ENABLED
_AUTO_RECONCILE_FIX = C._AUTO_RECONCILE_FIX
_AUTO_RECONCILE_INTERVAL_SEC = C._AUTO_RECONCILE_INTERVAL_SEC
_AUTO_RECONCILE_TOL = C._AUTO_RECONCILE_TOL
_ALLOW_EMPTY_DB = C._ALLOW_EMPTY_DB
_ALLOW_DB_DROP = C._ALLOW_DB_DROP
_DB_HEALTH_DROP_RATIO = C._DB_HEALTH_DROP_RATIO
_DB_HEALTH_DROP_MIN = C._DB_HEALTH_DROP_MIN
_DB_HEALTH_MIN_BYTES = C._DB_HEALTH_MIN_BYTES

def _cash_flow_net_between(start_date=None, end_date=None):
    if end_date and start_date and start_date > end_date:
        return 0.0

    def _date_filters(column):
        filters = []
        if start_date:
            filters.append(func.date(column) >= start_date.strftime('%Y-%m-%d'))
        if end_date:
            filters.append(func.date(column) <= end_date.strftime('%Y-%m-%d'))
        return filters

    cash_bank_ids = [a.id for a in _cf_company_accounts(active_only=False)]
    if not cash_bank_ids:
        return 0.0

    tx_in = float(AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.to_account_id.in_(cash_bank_ids),
        *_date_filters(AccountTransaction.date_posted)
    ).with_entities(func.sum(AccountTransaction.amount)).scalar() or 0)

    tx_out = float(AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.from_account_id.in_(cash_bank_ids),
        *_date_filters(AccountTransaction.date_posted)
    ).with_entities(func.sum(AccountTransaction.amount)).scalar() or 0)

    return tx_in - tx_out

def _legacy_adjustments_total(start_date=None, end_date=None):
    query = CashFlowDifferenceAdjustment.query.filter(
        CashFlowDifferenceAdjustment.physical_cash_available.is_(None)
    )
    if start_date:
        query = query.filter(CashFlowDifferenceAdjustment.adjustment_date >= start_date)
    if end_date:
        query = query.filter(CashFlowDifferenceAdjustment.adjustment_date <= end_date)
    return float(query.with_entities(func.coalesce(func.sum(CashFlowDifferenceAdjustment.amount), 0)).scalar() or 0)


def _cash_flow_in_out_between(start_date, end_date):
    if end_date and start_date and start_date > end_date:
        return 0.0, 0.0

    def _date_filters(column):
        filters = []
        if start_date:
            filters.append(func.date(column) >= start_date.strftime('%Y-%m-%d'))
        if end_date:
            filters.append(func.date(column) <= end_date.strftime('%Y-%m-%d'))
        return filters

    cash_bank_ids = [a.id for a in _cf_company_accounts(active_only=False)]
    if not cash_bank_ids:
        return 0.0, 0.0

    cash_in = float(AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.to_account_id.in_(cash_bank_ids),
        or_(AccountTransaction.from_account_id.notin_(cash_bank_ids), AccountTransaction.from_account_id.is_(None)),
        *_date_filters(AccountTransaction.date_posted)
    ).with_entities(func.sum(AccountTransaction.amount)).scalar() or 0)

    cash_out = float(AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.from_account_id.in_(cash_bank_ids),
        or_(AccountTransaction.to_account_id.notin_(cash_bank_ids), AccountTransaction.to_account_id.is_(None)),
        *_date_filters(AccountTransaction.date_posted)
    ).with_entities(func.sum(AccountTransaction.amount)).scalar() or 0)

    return cash_in, cash_out

def _cash_accounts_opening_total(as_of_date=None):
    """Sum of opening balances of active company CASH accounts.

    Account opening balances are part of the physical cash in hand but are
    NOT ledger transactions, so the automatic Cash Flow opening must add
    them explicitly — otherwise the report's closing balance never ties to
    the cash account balance shown in Accounts.
    """
    from utils.money import from_minor
    total = 0.0
    for acc in _cf_company_accounts(active_only=True):
        if (acc.category or '').lower() != 'cash':
            continue
        ob_date = getattr(acc, 'opening_balance_date', None)
        if as_of_date is not None and ob_date is not None:
            ob_day = ob_date.date() if hasattr(ob_date, 'date') else ob_date
            if ob_day > as_of_date:
                continue
        if getattr(acc, 'opening_balance_minor', None) is not None:
            total += float(from_minor(acc.opening_balance_minor))
        elif acc.opening_balance is not None:
            total += float(acc.opening_balance or 0)
    return total


def _automatic_cash_opening_balance(from_date_dt):
    previous_day = from_date_dt - timedelta(days=1)
    last_physical = CashFlowDifferenceAdjustment.query.filter(
        CashFlowDifferenceAdjustment.adjustment_date < from_date_dt,
        CashFlowDifferenceAdjustment.physical_cash_available.isnot(None)
    ).order_by(CashFlowDifferenceAdjustment.adjustment_date.desc()).first()

    if last_physical:
        # A physical count is an absolute cash figure: it already contains
        # any account opening balances, so only roll movements forward.
        start_date = last_physical.adjustment_date + timedelta(days=1)
        opening = float(last_physical.physical_cash_available or 0)
        opening += _cash_flow_net_between(start_date, previous_day)
        opening -= _legacy_adjustments_total(start_date, previous_day)
        return opening

    opening = _cash_accounts_opening_total(from_date_dt)
    opening += _cash_flow_net_between(None, previous_day)
    opening -= _legacy_adjustments_total(None, previous_day)
    return opening


def _current_username():
    return current_user.username if current_user and current_user.is_authenticated else None


def _cash_flow_today_opening_override(today_str):
    override = session.get('cash_flow_today_opening_override') or {}
    if override.get('date') != today_str:
        return None
    try:
        return _money_round(override.get('amount', 0))
    except Exception:
        return None


def _cash_flow_fresh_start_cutoff(today_str):
    cutoff = session.get('cash_flow_fresh_start_cutoff') or {}
    if cutoff.get('date') != today_str or not cutoff.get('at'):
        cutoff = {'date': today_str, 'at': pk_now().strftime('%Y-%m-%d %H:%M:%S')}
        session['cash_flow_fresh_start_cutoff'] = cutoff
    try:
        return datetime.strptime(cutoff['at'], '%Y-%m-%d %H:%M:%S')
    except Exception:
        cutoff = {'date': today_str, 'at': pk_now().strftime('%Y-%m-%d %H:%M:%S')}
        session['cash_flow_fresh_start_cutoff'] = cutoff
        return datetime.strptime(cutoff['at'], '%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------------------
# Configuration-driven Cash Flow engine
# Rules live here. Business category names do not.
# ---------------------------------------------------------------------------

CF_DIR_IN = 'in'
CF_DIR_OUT = 'out'
CF_DIR_TRANSFER = 'transfer'
CF_DIRECTIONS = (CF_DIR_IN, CF_DIR_OUT, CF_DIR_TRANSFER)
CF_CAT_DIRECTIONS = (CF_DIR_IN, CF_DIR_OUT, 'both')

SRC_MANUAL = 'MANUAL_CASH_FLOW'
SRC_CLIENT_PAYMENT = 'CLIENT_PAYMENT'
SRC_SUPPLIER_PAYMENT = 'SUPPLIER_PAYMENT'
SRC_SALE = 'SALE'
SRC_TRANSFER = 'TRANSFER'
SRC_DRIVER_PAYMENT = 'DRIVER_PAYMENT'
SRC_ACCOUNT_RECEIPT = 'ACCOUNT_RECEIPT'
SRC_ACCOUNT_EXPENSE = 'ACCOUNT_EXPENSE'

CF_SOURCE_LABELS = {
    SRC_MANUAL: 'Manual',
    SRC_CLIENT_PAYMENT: 'Client',
    SRC_SUPPLIER_PAYMENT: 'Supplier',
    SRC_SALE: 'Sale',
    SRC_TRANSFER: 'Transfer',
    SRC_DRIVER_PAYMENT: 'Driver',
    SRC_ACCOUNT_RECEIPT: 'Other system-generated',
    SRC_ACCOUNT_EXPENSE: 'Other system-generated',
}

# ---------------------------------------------------------------------------
# Section (module) attribution
# ---------------------------------------------------------------------------
# Every cash-flow row can be traced back to the screen it was posted from:
# Cash Flow, Accounts, Bookings, Direct Sales, Supplier Payments, GRN, …
# The label is derived from the source document stamped on the ledger row, so
# historical entries get the column as well — no schema change / backfill.
CF_SECTION_CASH_FLOW = 'Cash Flow'
CF_SECTION_ACCOUNTS = 'Accounts'
CF_SECTION_BOOKINGS = 'Bookings'
CF_SECTION_DIRECT_SALES = 'Direct Sales'
CF_SECTION_CLIENT_PAYMENTS = 'Client Payments'
CF_SECTION_CLIENT_REFUNDS = 'Client Refunds'
CF_SECTION_WAIVE_OFFS = 'Waive-Offs'
CF_SECTION_SUPPLIER_PAYMENTS = 'Supplier Payments'
CF_SECTION_GRN = 'GRN'
CF_SECTION_SALES_RETURNS = 'Sales Returns'
CF_SECTION_DELIVERY_RENT = 'Delivery & Rent'
CF_SECTION_RECONCILIATION = 'Reconciliation'

_CF_SECTION_BY_SOURCE = {
    'cashflowentry': CF_SECTION_CASH_FLOW,
    'manual_cash_flow': CF_SECTION_CASH_FLOW,
    'transfer': CF_SECTION_CASH_FLOW,
    'booking': CF_SECTION_BOOKINGS,
    'directsale': CF_SECTION_DIRECT_SALES,
    'direct_sale': CF_SECTION_DIRECT_SALES,
    'sale': CF_SECTION_DIRECT_SALES,
    'clientrefund': CF_SECTION_CLIENT_REFUNDS,
    'client_refund': CF_SECTION_CLIENT_REFUNDS,
    'supplierpayment': CF_SECTION_SUPPLIER_PAYMENTS,
    'supplier_payment': CF_SECTION_SUPPLIER_PAYMENTS,
    'supplier': CF_SECTION_SUPPLIER_PAYMENTS,
    'grn': CF_SECTION_GRN,
    'materialreturn': CF_SECTION_SALES_RETURNS,
    'material_return': CF_SECTION_SALES_RETURNS,
    'deliverypersonpayment': CF_SECTION_DELIVERY_RENT,
    'delivery_person_payment': CF_SECTION_DELIVERY_RENT,
    'driver_payment': CF_SECTION_DELIVERY_RENT,
    'deliveryperson': CF_SECTION_DELIVERY_RENT,
    'delivery_person': CF_SECTION_DELIVERY_RENT,
    'deliveryrent': CF_SECTION_DELIVERY_RENT,
    'delivery_rent': CF_SECTION_DELIVERY_RENT,
    'saledeliveryperson': CF_SECTION_DELIVERY_RENT,
    'sale_delivery_person': CF_SECTION_DELIVERY_RENT,
    'accountreconciliation': CF_SECTION_RECONCILIATION,
    'account_reconciliation': CF_SECTION_RECONCILIATION,
    'account_receipt': CF_SECTION_ACCOUNTS,
    'account_expense': CF_SECTION_ACCOUNTS,
    'entry': CF_SECTION_ACCOUNTS,
}

_CF_SECTION_BY_TX_TYPE = {
    'receipt': CF_SECTION_CLIENT_PAYMENTS,
    'refund': CF_SECTION_CLIENT_REFUNDS,
    'waive-off': CF_SECTION_WAIVE_OFFS,
    'waive off': CF_SECTION_WAIVE_OFFS,
    'discount / waive-off': CF_SECTION_WAIVE_OFFS,
    'supplier payment': CF_SECTION_SUPPLIER_PAYMENTS,
    'driver payment': CF_SECTION_DELIVERY_RENT,
}


def cf_section_label(source_type=None, transaction_type=None, *, default=CF_SECTION_CASH_FLOW):
    """Human label for the app section that produced a cash-flow row."""
    raw_source = (source_type or '').strip()
    key = raw_source.lower().replace('-', '_').replace(' ', '_')
    tx_key = (transaction_type or '').strip().lower()

    if key in ('payment', 'client', 'client_payment'):
        if tx_key in ('refund',):
            return CF_SECTION_CLIENT_REFUNDS
        if tx_key in ('loss', 'waive-off', 'waive off'):
            return CF_SECTION_WAIVE_OFFS
        return CF_SECTION_CLIENT_PAYMENTS

    label = _CF_SECTION_BY_SOURCE.get(key)
    if label:
        if label == CF_SECTION_DELIVERY_RENT and tx_key == 'loss':
            return CF_SECTION_WAIVE_OFFS
        return label

    if tx_key in _CF_SECTION_BY_TX_TYPE:
        return _CF_SECTION_BY_TX_TYPE[tx_key]

    if raw_source and raw_source.upper() not in ('SYSTEM', 'UNKNOWN'):
        return raw_source.replace('_', ' ').strip().title()

    return default


# Source documents that hold the *creator* of a projected ledger row.  The
# ledger row itself is often written by a sync helper without a user, so the
# username is resolved from the document that was actually entered.
_CF_CREATOR_SOURCES = {
    'payment': ('Payment', 'created_by', None),
    'client': ('Payment', 'created_by', None),
    'client_payment': ('Payment', 'created_by', None),
    'clientrefund': ('Payment', 'created_by', None),
    'supplierpayment': ('SupplierPayment', 'created_by', None),
    'supplier_payment': ('SupplierPayment', 'created_by', None),
    'deliverypersonpayment': ('DeliveryPersonPayment', 'created_by', None),
    'delivery_person_payment': ('DeliveryPersonPayment', 'created_by', None),
    'accountreconciliation': ('AccountReconciliation', 'created_by', None),
    'account_reconciliation': ('AccountReconciliation', 'created_by', None),
    'materialreturn': ('MaterialReturn', 'created_by', 'payment'),
    'material_return': ('MaterialReturn', 'created_by', 'payment'),
}

# Last resort: the structured accounting audit trail records who created the
# document even when the document model has no ``created_by`` column.
_CF_CREATOR_AUDIT_ENTITIES = {
    'booking': ('Booking', 'Entry'),
    'payment': ('Payment',),
    'clientrefund': ('Payment',),
    'supplierpayment': ('SupplierPayment',),
    'supplier_payment': ('SupplierPayment',),
    'grn': ('GRN', 'SupplierPayment'),
    'directsale': ('DirectSale',),
    'direct_sale': ('DirectSale',),
    'materialreturn': ('MaterialReturn',),
    'deliverypersonpayment': ('DeliveryPersonPayment',),
}


def _cf_chunks(values, size=400):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _cf_creator_lookup(txs):
    """Bulk-resolve ``(source_type, source_id) -> username`` for ledger rows.

    Two batched lookups (one per document type, plus one audit-trail query) keep
    this O(1) per page instead of a query per row.  Ids are chunked so a wide
    date range ("All") can never trip SQLite's bound-parameter limit.
    """
    wanted = {}
    for tx in txs:
        if (getattr(tx, 'created_by', None) or '').strip():
            continue
        source_type = (getattr(tx, 'source_type', None) or '').strip().lower().replace(' ', '_')
        source_id = getattr(tx, 'source_id', None)
        if not source_type or not source_id:
            continue
        wanted.setdefault(source_type, set()).add(int(source_id))

    resolved = {}
    if not wanted:
        return resolved

    model_by_name = {
        'Payment': Payment,
        'SupplierPayment': SupplierPayment,
        'DeliveryPersonPayment': DeliveryPersonPayment,
        'AccountReconciliation': AccountReconciliation,
        'MaterialReturn': MaterialReturn,
    }
    audit_wanted = {}
    for source_type, ids in wanted.items():
        spec = _CF_CREATOR_SOURCES.get(source_type)
        if spec:
            model_name, attr, via = spec
            model = model_by_name.get(model_name)
            if model is not None:
                for chunk in _cf_chunks(ids):
                    try:
                        for obj in model.query.filter(model.id.in_(chunk)).all():
                            name = (getattr(obj, attr, None) or '').strip()
                            if not name and via:
                                rel = getattr(obj, via, None)
                                name = (getattr(rel, attr, None) or '').strip() if rel else ''
                            if name:
                                resolved[(source_type, int(obj.id))] = name
                    except Exception:  # pragma: no cover - defensive only
                        logging.getLogger(__name__).debug(
                            'Cash flow creator lookup failed for %s', source_type, exc_info=True
                        )
                        break
        unresolved_ids = {i for i in ids if (source_type, i) not in resolved}
        for entity in _CF_CREATOR_AUDIT_ENTITIES.get(source_type, ()):
            if unresolved_ids:
                audit_wanted.setdefault(entity, set()).update(unresolved_ids)

    if audit_wanted:
        entity_types = list(audit_wanted.keys())
        all_ids = sorted({i for ids in audit_wanted.values() for i in ids})
        seen = set()
        for chunk in _cf_chunks(all_ids):
            try:
                q = AccountingAuditLog.query.filter(
                    AccountingAuditLog.entity_type.in_(entity_types),
                    AccountingAuditLog.entity_id.in_(chunk),
                ).order_by(AccountingAuditLog.created_at.asc(), AccountingAuditLog.id.asc())
                for row in q.all():
                    if not (row.username or '').strip():
                        continue
                    key = ((row.entity_type or '').strip().lower(), int(row.entity_id or 0))
                    if key in seen:
                        continue
                    seen.add(key)
                    # Map the audit entity back to every source type that uses it.
                    for source_type, entities in _CF_CREATOR_AUDIT_ENTITIES.items():
                        if key[0] in [e.lower() for e in entities]:
                            resolved.setdefault((source_type, key[1]), row.username.strip())
            except Exception:  # pragma: no cover - defensive only
                logging.getLogger(__name__).debug('Cash flow creator audit lookup failed', exc_info=True)
                break

    return resolved


CF_PARTY_TYPES = [
    ('person', 'Person'),
    ('outsider', 'Outsider / other party'),
    ('loan', 'Loan (person or outside lender)'),
    ('other', 'Other'),
]

CF_SRC_MARKER = '[SRC:CashFlow]'
_MIRROR_MARKERS = (
    '[SRC:BOOKING:',
    '[SRC:DIRECTSALE:',
    '[SRC:PAYMENT:',
    '[SRC:SUPPLIERPAYMENT:',
    '[SRC:CLIENTREFUND:',
)


def _cf_actor_name(actor=None):
    if actor is not None and getattr(actor, 'username', None):
        return actor.username
    return _current_username()


def _cf_is_money_account(account):
    if account is None:
        return False
    if getattr(account, 'is_active', True) is False:
        return False
    return (account.category or '').lower() in ('cash', 'bank')


def _cf_company_accounts(active_only=True):
    q = Account.query
    if active_only:
        q = q.filter(func.coalesce(Account.is_active, True) == True)
    rows = []
    for acc in q.order_by(Account.name.asc(), Account.id.asc()).all():
        if (acc.category or '').lower() in ('cash', 'bank'):
            rows.append(acc)
    return rows


def _cf_account_label(account):
    if account is None:
        return ''
    cat = (account.category or 'cash').upper()
    bal = _money_round(account.balance or 0)
    return f'{account.name} · {cat} · Rs. {bal:,.0f}'


def _cf_normalize_direction(value):
    raw = (value or '').strip().lower()
    if raw in ('received', 'receive', 'in', 'cash_in'):
        return CF_DIR_IN
    if raw in ('spent', 'spend', 'out', 'cash_out', 'paid'):
        return CF_DIR_OUT
    if raw in ('transfer', 'xfer'):
        return CF_DIR_TRANSFER
    return raw


def _cf_type_label(direction):
    return {
        CF_DIR_IN: 'Received',
        CF_DIR_OUT: 'Spent',
        CF_DIR_TRANSFER: 'Transfer',
    }.get(direction, direction or '')


def _cf_ensure_indexes():
    try:
        db.session.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_cash_flow_entry_idempotency_key '
            'ON cash_flow_entry(idempotency_key) WHERE idempotency_key IS NOT NULL'
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _cf_snapshot(entry):
    return {
        'id': entry.id,
        'direction': entry.direction,
        'amount': float(entry.amount or 0),
        'account_id': entry.account_id,
        'destination_account_id': getattr(entry, 'destination_account_id', None),
        'category_id': entry.category_id,
        'subcategory_id': entry.subcategory_id,
        'party_id': entry.party_id,
        'party_name': entry.party_name,
        'party_type': entry.party_type,
        'description': entry.description,
        'note': entry.note,
        'reference': getattr(entry, 'reference', None),
        'date_posted': str(entry.date_posted or ''),
        'is_void': bool(entry.is_void),
        'source_type': getattr(entry, 'source_type', None) or SRC_MANUAL,
    }


def _cf_write_audit(entry, action, before=None, after=None, reason=None, actor=None):
    db.session.add(CashFlowEntryAudit(
        entry_id=entry.id,
        action=action,
        before_json=json.dumps(before, default=str) if before is not None else None,
        after_json=json.dumps(after, default=str) if after is not None else None,
        reason=(reason or '').strip() or None,
        changed_by=_cf_actor_name(actor),
        changed_at=pk_now(),
    ))


def _cf_find_category(category_id=None, name=None, active_only=True):
    if category_id:
        cat = db.session.get(CashFlowCategory, category_id)
        if cat and (not active_only or cat.is_active):
            return cat
        return None
    name = (name or '').strip()
    if not name:
        return None
    q = CashFlowCategory.query.filter(func.lower(CashFlowCategory.name) == name.lower())
    if active_only:
        q = q.filter(CashFlowCategory.is_active == True)
    return q.first()


def _cf_find_subcategory(category, subcategory_id=None, name=None, active_only=True):
    if not category:
        return None
    if subcategory_id:
        sub = db.session.get(CashFlowSubcategory, subcategory_id)
        if sub and sub.category_id == category.id and (not active_only or sub.is_active):
            return sub
        return None
    name = (name or '').strip()
    if not name:
        return None
    q = CashFlowSubcategory.query.filter(
        CashFlowSubcategory.category_id == category.id,
        func.lower(CashFlowSubcategory.name) == name.lower(),
    )
    if active_only:
        q = q.filter(CashFlowSubcategory.is_active == True)
    return q.first()


def _cf_find_party(party_id=None, name=None, party_type=None, active_only=True):
    if party_id:
        party = db.session.get(CashFlowParty, party_id)
        if party and (not active_only or party.is_active):
            return party
        return None
    name = (name or '').strip()
    if not name:
        return None
    q = CashFlowParty.query.filter(func.lower(CashFlowParty.name) == name.lower())
    if party_type:
        q = q.filter(func.lower(func.coalesce(CashFlowParty.party_type, '')) == party_type.lower())
    if active_only:
        q = q.filter(CashFlowParty.is_active == True)
    return q.first()


def save_cf_category(name, direction='both', notes=None, actor=None):
    name = (name or '').strip()
    direction = (direction or 'both').strip().lower()
    if not name:
        raise ValueError('Category name is required.')
    if direction not in CF_CAT_DIRECTIONS:
        raise ValueError('Category direction must be Received, Spent, or Both.')
    existing = CashFlowCategory.query.filter(func.lower(CashFlowCategory.name) == name.lower()).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
        existing.direction = direction
        if notes is not None:
            existing.notes = (notes or '').strip() or None
        existing.updated_at = pk_now()
        db.session.flush()
        return existing, False
    cat = CashFlowCategory(
        name=name, direction=direction, is_active=True,
        notes=(notes or '').strip() or None, created_at=pk_now(), updated_at=pk_now(),
    )
    db.session.add(cat)
    db.session.flush()
    return cat, True


def save_cf_subcategory(category_id, name, notes=None, actor=None):
    name = (name or '').strip()
    cat = db.session.get(CashFlowCategory, category_id) if category_id else None
    if not cat:
        raise ValueError('Pick a parent category.')
    if not name:
        raise ValueError('Sub-category name is required.')
    existing = CashFlowSubcategory.query.filter(
        CashFlowSubcategory.category_id == cat.id,
        func.lower(CashFlowSubcategory.name) == name.lower(),
    ).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
        if notes is not None:
            existing.notes = (notes or '').strip() or None
        existing.updated_at = pk_now()
        db.session.flush()
        return existing, False
    sub = CashFlowSubcategory(
        category_id=cat.id, name=name, is_active=True,
        notes=(notes or '').strip() or None, created_at=pk_now(), updated_at=pk_now(),
    )
    db.session.add(sub)
    db.session.flush()
    return sub, True


def save_cf_party(name, party_type='person', note=None, actor=None):
    name = (name or '').strip()
    party_type = (party_type or 'other').strip().lower() or 'other'
    if not name:
        raise ValueError('Name is required.')
    existing = _cf_find_party(name=name, party_type=party_type, active_only=False)
    if existing:
        existing.is_active = True
        if note is not None:
            existing.note = (note or '').strip() or None
        existing.updated_at = pk_now()
        db.session.flush()
        return existing, False
    party = CashFlowParty(
        name=name, party_type=party_type, note=(note or '').strip() or None,
        is_active=True, created_at=pk_now(), updated_at=pk_now(),
    )
    db.session.add(party)
    db.session.flush()
    return party, True


def disable_cf_category(category_id):
    cat = db.session.get(CashFlowCategory, category_id)
    if not cat:
        raise ValueError('Category not found.')
    cat.is_active = False
    cat.updated_at = pk_now()
    db.session.flush()
    return cat


def enable_cf_category(category_id):
    cat = db.session.get(CashFlowCategory, category_id)
    if not cat:
        raise ValueError('Category not found.')
    cat.is_active = True
    cat.updated_at = pk_now()
    db.session.flush()
    return cat


def disable_cf_subcategory(subcategory_id):
    sub = db.session.get(CashFlowSubcategory, subcategory_id)
    if not sub:
        raise ValueError('Sub-category not found.')
    sub.is_active = False
    sub.updated_at = pk_now()
    db.session.flush()
    return sub


def enable_cf_subcategory(subcategory_id):
    sub = db.session.get(CashFlowSubcategory, subcategory_id)
    if not sub:
        raise ValueError('Sub-category not found.')
    sub.is_active = True
    sub.updated_at = pk_now()
    db.session.flush()
    return sub


def disable_cf_party(party_id):
    party = db.session.get(CashFlowParty, party_id)
    if not party:
        raise ValueError('Party not found.')
    party.is_active = False
    party.updated_at = pk_now()
    db.session.flush()
    return party


def enable_cf_party(party_id):
    party = db.session.get(CashFlowParty, party_id)
    if not party:
        raise ValueError('Party not found.')
    party.is_active = True
    party.updated_at = pk_now()
    db.session.flush()
    return party


def update_cf_party(party_id, name, party_type=None, note=None):
    party = db.session.get(CashFlowParty, party_id)
    if not party:
        raise ValueError('Party not found.')
    name = (name or '').strip()
    if not name:
        raise ValueError('Name is required.')
    party.name = name
    if party_type:
        party.party_type = (party_type or 'other').strip().lower() or 'other'
    if note is not None:
        party.note = (note or '').strip() or None
    party.updated_at = pk_now()
    db.session.flush()
    return party


def rename_cf_category(category_id, name, direction=None, notes=None):
    cat = db.session.get(CashFlowCategory, category_id)
    if not cat:
        raise ValueError('Category not found.')
    name = (name or '').strip()
    if not name:
        raise ValueError('Category name is required.')
    clash = CashFlowCategory.query.filter(
        func.lower(CashFlowCategory.name) == name.lower(),
        CashFlowCategory.id != cat.id,
    ).first()
    if clash:
        raise ValueError('Another category already uses that name.')
    cat.name = name
    if direction in CF_CAT_DIRECTIONS:
        cat.direction = direction
    if notes is not None:
        cat.notes = (notes or '').strip() or None
    cat.updated_at = pk_now()
    db.session.flush()
    return cat


def rename_cf_subcategory(subcategory_id, name, notes=None):
    sub = db.session.get(CashFlowSubcategory, subcategory_id)
    if not sub:
        raise ValueError('Sub-category not found.')
    name = (name or '').strip()
    if not name:
        raise ValueError('Sub-category name is required.')
    clash = CashFlowSubcategory.query.filter(
        CashFlowSubcategory.category_id == sub.category_id,
        func.lower(CashFlowSubcategory.name) == name.lower(),
        CashFlowSubcategory.id != sub.id,
    ).first()
    if clash:
        raise ValueError('Another sub-category already uses that name under this category.')
    sub.name = name
    if notes is not None:
        sub.notes = (notes or '').strip() or None
    sub.updated_at = pk_now()
    db.session.flush()
    return sub


def cf_used_category_ids():
    rows = db.session.query(CashFlowEntry.category_id).filter(
        CashFlowEntry.category_id.isnot(None)
    ).distinct().all()
    return {row[0] for row in rows}


def cf_used_subcategory_ids():
    rows = db.session.query(CashFlowEntry.subcategory_id).filter(
        CashFlowEntry.subcategory_id.isnot(None)
    ).distinct().all()
    return {row[0] for row in rows}


def cf_used_party_ids():
    rows = db.session.query(CashFlowEntry.party_id).filter(
        CashFlowEntry.party_id.isnot(None)
    ).distinct().all()
    return {row[0] for row in rows}


def _cf_category_is_used(category_id):
    if CashFlowEntry.query.filter(CashFlowEntry.category_id == category_id).first():
        return True
    sub_ids = [
        row[0] for row in db.session.query(CashFlowSubcategory.id).filter(
            CashFlowSubcategory.category_id == category_id
        ).all()
    ]
    if sub_ids and CashFlowEntry.query.filter(CashFlowEntry.subcategory_id.in_(sub_ids)).first():
        return True
    return False


def delete_cf_category(category_id):
    """Hard-delete an unused category. Historically used categories must be disabled."""
    cat = db.session.get(CashFlowCategory, category_id)
    if not cat:
        raise ValueError('Category not found.')
    linked_modules = []
    if CashFlowEntry.query.filter(CashFlowEntry.category_id == cat.id).first():
        linked_modules.append('CashFlowEntry')
    sub_ids = [
        row[0] for row in db.session.query(CashFlowSubcategory.id).filter(
            CashFlowSubcategory.category_id == cat.id
        ).all()
    ]
    if sub_ids:
        if CashFlowEntry.query.filter(CashFlowEntry.subcategory_id.in_(sub_ids)).first():
            linked_modules.append('CashFlowEntry (via subcategory)')
        # If subcategories exist but are not directly linked to entries,
        # deleting the category will cascade-remove them (clear link message)
        if not linked_modules:
            linked_modules.append('CashFlowSubcategory (will be removed with category)')
    if _cf_category_is_used(cat.id):
        msg = 'This category is linked to: ' + ', '.join(linked_modules) if linked_modules else 'This category is used by historical transactions.'
        msg += ' Cannot delete — disable it instead, or delete from the linked modules first.'
        raise ValueError(msg)
    CashFlowSubcategory.query.filter(CashFlowSubcategory.category_id == cat.id).delete(
        synchronize_session=False
    )
    db.session.delete(cat)
    db.session.flush()
    return True


def delete_cf_subcategory(subcategory_id):
    """Hard-delete an unused sub-category. Historically used ones must be disabled."""
    sub = db.session.get(CashFlowSubcategory, subcategory_id)
    if not sub:
        raise ValueError('Sub-category not found.')
    linked_modules = []
    if CashFlowEntry.query.filter(CashFlowEntry.subcategory_id == sub.id).first():
        linked_modules.append('CashFlowEntry')
    if linked_modules:
        msg = 'This sub-category is linked to: ' + ', '.join(linked_modules)
        msg += '. Cannot delete — disable it instead, or delete from the linked modules first.'
        raise ValueError(msg)
    db.session.delete(sub)
    db.session.flush()
    return True


def delete_cf_party(party_id):
    """Hard-delete an unused party. Historically used parties must be disabled."""
    party = db.session.get(CashFlowParty, party_id)
    if not party:
        raise ValueError('Party not found.')
    linked_modules = []
    if CashFlowEntry.query.filter(CashFlowEntry.party_id == party.id).first():
        linked_modules.append('CashFlowEntry')
    if linked_modules:
        msg = 'This party is linked to: ' + ', '.join(linked_modules)
        msg += '. Cannot delete — disable it instead, or delete from the linked modules first.'
        raise ValueError(msg)
    db.session.delete(party)
    db.session.flush()
    return True


def parse_physical_cash_amount(raw):
    """Parse Physical Cash Available.

    Zero is valid. Empty / missing is invalid. Invalid text is rejected.
    Do not treat 0 as missing.
    """
    if raw is None:
        raise ValueError('Physical Cash Available is required. Difference is calculated by the system.')
    text = str(raw).strip().replace(',', '')
    for prefix in ('Rs.', 'rs.', 'RS.', 'PKR', 'pkr'):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if text == '':
        raise ValueError('Physical Cash Available is required. Difference is calculated by the system.')
    try:
        amount = Decimal(text)
    except Exception:
        raise ValueError('Physical Cash Available must be a valid number.')
    amount = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if amount == Decimal('-0.00'):
        amount = Decimal('0.00')
    return float(amount)


def compute_physical_cash_difference(physical_cash, calculated_closing):
    """Difference = Physical Cash Available - System Calculated Closing."""
    return _money_round(physical_cash) - _money_round(calculated_closing)


def _cf_resolve_category(direction, category_id, category_name, *, required, create_if_missing):
    if direction == CF_DIR_TRANSFER:
        return None
    cat = _cf_find_category(category_id=category_id, name=category_name, active_only=True)
    if cat:
        allowed = (cat.direction or 'both').lower()
        if allowed not in ('both', direction):
            raise ValueError('Selected category is not allowed for this transaction type.')
        return cat
    name = (category_name or '').strip()
    if required and not name and not category_id:
        raise ValueError('Category is required for Received and Spent.')
    if category_id and not cat:
        raise ValueError('Selected category is missing or inactive.')
    if name and create_if_missing:
        cat, _ = save_cf_category(name, direction=direction)
        return cat
    if required:
        raise ValueError('Category must exist and be active. Create it in Categories first.')
    return None


def _cf_resolve_subcategory(category, subcategory_id, subcategory_name, *, create_if_missing):
    if not category:
        if subcategory_id or (subcategory_name or '').strip():
            raise ValueError('Sub-category requires a category.')
        return None
    sub = _cf_find_subcategory(category, subcategory_id=subcategory_id, name=subcategory_name, active_only=True)
    if sub:
        return sub
    if subcategory_id:
        raise ValueError('Sub-category does not belong to the selected category.')
    name = (subcategory_name or '').strip()
    if name and create_if_missing:
        sub, _ = save_cf_subcategory(category.id, name)
        return sub
    if name:
        raise ValueError('Sub-category must belong to the selected category.')
    return None


def _cf_build_tx_fields(direction, amount, account, destination, description, note, posted, entry_id=None):
    from utils.money import to_minor
    marker = CF_SRC_MARKER if not entry_id else f'[SRC:CashFlow:{int(entry_id)}]'
    note_txt = ' '.join(x for x in [(note or '').strip(), marker] if x).strip()
    amount = _money_round(amount)
    fields = {
        'amount': amount,
        'amount_minor': to_minor(amount),
        'description': description,
        'note': note_txt,
        'date_posted': posted,
        'source_type': 'CashFlowEntry',
        'source_id': entry_id,
        'is_void': False,
    }
    if direction == CF_DIR_IN:
        fields.update(from_account_id=None, to_account_id=account.id, transaction_type='Receipt')
    elif direction == CF_DIR_OUT:
        fields.update(from_account_id=account.id, to_account_id=None, transaction_type='Expense')
    else:
        fields.update(
            from_account_id=account.id,
            to_account_id=destination.id,
            transaction_type='Transfer',
        )
    return fields


def validate_manual_cash_flow(
    *, direction, amount, account_id, destination_account_id=None,
    category_id=None, category_name=None, subcategory_id=None, subcategory_name=None,
    require_category=True, create_missing=False, check_balance=True,
):
    direction = _cf_normalize_direction(direction)
    if direction not in CF_DIRECTIONS:
        raise ValueError('Choose Received, Spent, or Transfer.')
    amount = _money_round(amount)
    if amount <= 0:
        raise ValueError('Amount must be greater than zero.')
    account = db.session.get(Account, account_id) if account_id else None
    if not _cf_is_money_account(account):
        raise ValueError('Select a valid company cash or bank account.')
    destination = None
    if direction == CF_DIR_TRANSFER:
        destination = db.session.get(Account, destination_account_id) if destination_account_id else None
        if not _cf_is_money_account(destination):
            raise ValueError('Select a valid destination account.')
        if destination.id == account.id:
            raise ValueError('Source and destination accounts cannot be the same.')
    else:
        cat = _cf_resolve_category(
            direction, category_id, category_name,
            required=require_category, create_if_missing=create_missing,
        )
        _cf_resolve_subcategory(cat, subcategory_id, subcategory_name, create_if_missing=create_missing)
    if check_balance and direction in (CF_DIR_OUT, CF_DIR_TRANSFER) and float(account.balance or 0) < amount:
        raise ValueError(f'Insufficient balance in {account.name}.')
    return direction, amount, account, destination


def save_manual_cash_flow_entry(
    *, direction, amount, account_id, destination_account_id=None,
    category_id=None, category_name=None, subcategory_id=None, subcategory_name=None,
    party_id=None, party_name=None, party_type=None, description=None, note=None,
    reference=None, date_posted=None, idempotency_key=None, actor=None,
    create_missing=True,
):
    from app.services.accounting import _apply_account_tx_effect

    _cf_ensure_indexes()
    key = (idempotency_key or '').strip() or None
    if key:
        existing = CashFlowEntry.query.filter(CashFlowEntry.idempotency_key == key).first()
        if existing:
            return existing, False

    direction, amount, account, destination = validate_manual_cash_flow(
        direction=direction, amount=amount, account_id=account_id,
        destination_account_id=destination_account_id, category_id=category_id,
        category_name=category_name, subcategory_id=subcategory_id,
        subcategory_name=subcategory_name, require_category=True,
        create_missing=create_missing,
    )
    cat = _cf_resolve_category(
        direction, category_id, category_name, required=(direction != CF_DIR_TRANSFER),
        create_if_missing=create_missing,
    ) if direction != CF_DIR_TRANSFER else None
    sub = _cf_resolve_subcategory(cat, subcategory_id, subcategory_name, create_if_missing=create_missing)
    ptype = (party_type or 'other').strip().lower() or 'other'
    party = _cf_find_party(party_id=party_id, name=party_name, party_type=ptype, active_only=True)
    if not party and (party_name or '').strip():
        party, _ = save_cf_party(party_name, ptype)
    if party:
        party_name = party.name
        ptype = party.party_type or ptype

    posted = date_posted or pk_now()
    desc = (description or '').strip()
    if not desc:
        desc = (cat.name if cat else _cf_type_label(direction))
        if (party_name or '').strip():
            desc = f'{desc} — {party_name.strip()}'
    note = (note or '').strip() or None
    reference = (reference or '').strip() or None
    actor_name = _cf_actor_name(actor)

    from utils.money import to_minor
    entry = CashFlowEntry(
        direction=direction, amount=amount, amount_minor=to_minor(amount),
        account_id=account.id,
        destination_account_id=(destination.id if destination else None),
        category_id=cat.id if cat else None,
        subcategory_id=sub.id if sub else None,
        party_id=party.id if party else None,
        party_name=(party_name or '').strip() or None,
        party_type=ptype,
        description=desc, note=note, reference=reference,
        date_posted=posted, created_by=actor_name, updated_by=actor_name,
        source_type=SRC_MANUAL, is_void=False, revision=1,
        idempotency_key=key, created_at=pk_now(), updated_at=pk_now(),
    )
    db.session.add(entry)
    db.session.flush()

    tx_fields = _cf_build_tx_fields(direction, amount, account, destination, desc, note, posted, entry.id)
    tx_fields['created_by'] = actor_name
    tx = AccountTransaction(**tx_fields)
    db.session.add(tx)
    db.session.flush()
    _apply_account_tx_effect(tx)
    entry.account_tx_id = tx.id
    _cf_write_audit(entry, 'Created', after=_cf_snapshot(entry), actor=actor)
    db.session.flush()
    return entry, True


def update_manual_cash_flow_entry(
    entry, *, direction, amount, account_id, destination_account_id=None,
    category_id=None, category_name=None, subcategory_id=None, subcategory_name=None,
    party_id=None, party_name=None, party_type=None, description=None, note=None,
    reference=None, date_posted=None, reason=None, actor=None, create_missing=True,
):
    from app.services.accounting import _apply_account_tx_effect, _reverse_account_tx_effect

    if entry is None:
        raise ValueError('Entry not found.')
    if entry.is_void:
        raise ValueError('A voided transaction cannot be edited. Restore it first.')

    before = _cf_snapshot(entry)
    direction, amount, account, destination = validate_manual_cash_flow(
        direction=direction, amount=amount, account_id=account_id,
        destination_account_id=destination_account_id, category_id=category_id,
        category_name=category_name, subcategory_id=subcategory_id,
        subcategory_name=subcategory_name, require_category=True,
        create_missing=create_missing, check_balance=False,
    )
    # Reverse first so the insufficient-balance check sees the pre-transaction balance.
    tx = db.session.get(AccountTransaction, entry.account_tx_id) if entry.account_tx_id else None
    if tx and not tx.is_void:
        _reverse_account_tx_effect(tx)

    if direction in (CF_DIR_OUT, CF_DIR_TRANSFER) and float(account.balance or 0) < amount:
        if tx and not tx.is_void:
            _apply_account_tx_effect(tx)
        raise ValueError(f'Insufficient balance in {account.name}.')

    cat = _cf_resolve_category(
        direction, category_id, category_name, required=(direction != CF_DIR_TRANSFER),
        create_if_missing=create_missing,
    ) if direction != CF_DIR_TRANSFER else None
    sub = _cf_resolve_subcategory(cat, subcategory_id, subcategory_name, create_if_missing=create_missing)
    ptype = (party_type or entry.party_type or 'other').strip().lower() or 'other'
    party = _cf_find_party(party_id=party_id, name=party_name, party_type=ptype, active_only=True)
    if not party and (party_name or '').strip():
        party, _ = save_cf_party(party_name, ptype)
    if party:
        party_name = party.name
        ptype = party.party_type or ptype

    posted = date_posted or entry.date_posted or pk_now()
    desc = (description or '').strip() or entry.description or _cf_type_label(direction)
    note = (note or '').strip() or None
    reference = (reference if reference is not None else entry.reference)
    reference = (reference or '').strip() or None
    actor_name = _cf_actor_name(actor)

    from utils.money import to_minor
    entry.direction = direction
    entry.amount = amount
    entry.amount_minor = to_minor(amount)
    entry.account_id = account.id
    entry.destination_account_id = destination.id if destination else None
    entry.category_id = cat.id if cat else None
    entry.subcategory_id = sub.id if sub else None
    entry.party_id = party.id if party else None
    entry.party_name = (party_name or '').strip() or None
    entry.party_type = ptype
    entry.description = desc
    entry.note = note
    entry.reference = reference
    entry.date_posted = posted
    entry.updated_by = actor_name
    entry.updated_at = pk_now()
    entry.revision = int(entry.revision or 1) + 1

    tx_fields = _cf_build_tx_fields(direction, amount, account, destination, desc, note, posted, entry.id)
    if tx is None:
        tx = AccountTransaction(created_by=actor_name, **tx_fields)
        db.session.add(tx)
        db.session.flush()
        entry.account_tx_id = tx.id
    else:
        for key, value in tx_fields.items():
            setattr(tx, key, value)
    _apply_account_tx_effect(tx)
    _cf_write_audit(entry, 'Edited', before=before, after=_cf_snapshot(entry), reason=reason, actor=actor)
    db.session.flush()
    return entry


def void_manual_cash_flow_entry(entry, reason=None, actor=None):
    from app.services.accounting import _void_account_tx

    if entry is None:
        raise ValueError('Entry not found.')
    if entry.is_void:
        raise ValueError('This transaction is already voided.')
    before = _cf_snapshot(entry)
    tx = db.session.get(AccountTransaction, entry.account_tx_id) if entry.account_tx_id else None
    if tx and not tx.is_void:
        _void_account_tx(tx)
        tx.voided_by = _cf_actor_name(actor)
        tx.voided_at = pk_now()
    entry.is_void = True
    entry.voided_at = pk_now()
    entry.voided_by = _cf_actor_name(actor)
    entry.void_reason = (reason or '').strip() or None
    entry.updated_by = _cf_actor_name(actor)
    entry.updated_at = pk_now()
    entry.revision = int(entry.revision or 1) + 1
    _cf_write_audit(entry, 'Voided', before=before, after=_cf_snapshot(entry), reason=reason, actor=actor)
    db.session.flush()
    return entry


def restore_manual_cash_flow_entry(entry, reason=None, actor=None):
    from app.services.accounting import _unvoid_account_tx

    if entry is None:
        raise ValueError('Entry not found.')
    if not entry.is_void:
        raise ValueError('This transaction is already active.')
    before = _cf_snapshot(entry)
    tx = db.session.get(AccountTransaction, entry.account_tx_id) if entry.account_tx_id else None
    if tx and tx.is_void:
        _unvoid_account_tx(tx)
    entry.is_void = False
    entry.voided_at = None
    entry.voided_by = None
    entry.void_reason = None
    entry.updated_by = _cf_actor_name(actor)
    entry.updated_at = pk_now()
    entry.revision = int(entry.revision or 1) + 1
    _cf_write_audit(entry, 'Restored', before=before, after=_cf_snapshot(entry), reason=reason, actor=actor)
    db.session.flush()
    return entry


def _cf_row(
    *, sort_dt, direction, amount, reference, description, note='',
    category='', subcategory='', party_name='', party_type='',
    account_id=None, account_name='', dest_account_id=None, dest_account_name='',
    source_type=SRC_MANUAL, origin='recorded', origin_label='',
    created_by='', status='active', entry_id=None, tx_id=None,
):
    amount = float(amount or 0)
    cash_in = amount if direction == CF_DIR_IN else 0.0
    cash_out = amount if direction == CF_DIR_OUT else 0.0
    transfer_amount = amount if direction == CF_DIR_TRANSFER else 0.0
    if dest_account_name and direction == CF_DIR_TRANSFER:
        account_display = f'{account_name or "—"} → {dest_account_name}'
    else:
        account_display = account_name or '—'
    dt = sort_dt
    date_val = dt.date() if hasattr(dt, 'date') else dt
    return {
        'date': date_val,
        'sort_dt': dt,
        'type': direction,
        'tx_type_label': _cf_type_label(direction),
        'cash_in': cash_in,
        'cash_out': cash_out,
        'transfer_amount': transfer_amount,
        'amount': amount,
        'account_id': account_id,
        'account_name': account_name or '',
        'account_to_id': dest_account_id,
        'account_to_name': dest_account_name or '',
        'account_display': account_display,
        'category': category or '',
        'subcategory': subcategory or '',
        'party_name': party_name or '',
        'party_type': party_type or '',
        'description': description or '',
        'note': note or '',
        'reference': reference or '',
        'source': 'MANUAL' if source_type == SRC_MANUAL else 'SYSTEM',
        'source_type': source_type,
        'origin': origin,
        'origin_label': origin_label or CF_SOURCE_LABELS.get(source_type, source_type),
        'created_by': created_by or '',
        'section_label': cf_section_label(source_type, direction),
        'status': status,
        'entry_id': entry_id,
        'tx_id': tx_id,
    }


def _cf_is_mirror_tx(tx):
    note_u = (tx.note or '').upper()
    if CF_SRC_MARKER.upper() in note_u or '[SRC:CASHFLOW' in note_u:
        return True
    if (getattr(tx, 'source_type', None) or '') == 'CashFlowEntry':
        return True
    return any(marker in note_u for marker in _MIRROR_MARKERS)


def collect_cash_flow_rows(from_date, to_date, *, posted_after=None, include_voided=True):
    rows = []
    account_by_id = {a.id: a for a in Account.query.all()}
    cash_bank_ids = {a.id for a in account_by_id.values() if (a.category or '').lower() in ('cash', 'bank')}
    
    if not cash_bank_ids:
        return rows

    tx_q = AccountTransaction.query.filter(
        func.date(AccountTransaction.date_posted) >= from_date,
        func.date(AccountTransaction.date_posted) <= to_date,
        or_(
            AccountTransaction.from_account_id.in_(cash_bank_ids),
            AccountTransaction.to_account_id.in_(cash_bank_ids)
        )
    )
    if posted_after is not None:
        tx_q = tx_q.filter(AccountTransaction.date_posted > posted_after)
    if not include_voided:
        tx_q = tx_q.filter(AccountTransaction.is_void == False)

    cf_entries = {e.account_tx_id: e for e in CashFlowEntry.query.filter(CashFlowEntry.account_tx_id.isnot(None)).all()}

    txs = tx_q.all()
    # Who made each entry: the ledger row may have been written by a sync helper
    # without a user, so fall back to the source document / audit trail.
    creator_by_source = _cf_creator_lookup(txs)

    for tx in txs:
        from_acc = account_by_id.get(tx.from_account_id)
        to_acc = account_by_id.get(tx.to_account_id)
        
        is_from_cash = tx.from_account_id in cash_bank_ids
        is_to_cash = tx.to_account_id in cash_bank_ids

        if is_from_cash and is_to_cash:
            direction = CF_DIR_TRANSFER
            account_id = tx.from_account_id
            account_name = from_acc.name if from_acc else ''
            dest_account_id = tx.to_account_id
            dest_account_name = to_acc.name if to_acc else ''
            cash_in = 0.0
            cash_out = 0.0
            transfer_amount = float(tx.amount or 0)
        elif is_to_cash:
            direction = CF_DIR_IN
            account_id = tx.to_account_id
            account_name = to_acc.name if to_acc else ''
            dest_account_id = None
            dest_account_name = ''
            cash_in = float(tx.amount or 0)
            cash_out = 0.0
            transfer_amount = 0.0
        else:
            direction = CF_DIR_OUT
            account_id = tx.from_account_id
            account_name = from_acc.name if from_acc else ''
            dest_account_id = None
            dest_account_name = ''
            cash_in = 0.0
            cash_out = float(tx.amount or 0)
            transfer_amount = 0.0

        e = cf_entries.get(tx.id)
        if e:
            category = e.category.name if e.category else ''
            subcategory = e.subcategory.name if e.subcategory else ''
            party_name = e.party_name or (e.party.name if e.party else '')
            party_type = e.party_type or (e.party.party_type if e.party else '')
            description = e.description or tx.description or ''
            note = e.note or tx.note or ''
            reference = getattr(e, 'reference', None) or f'TX-{tx.id}'
            origin = 'recorded'
            origin_label = 'Recorded on Cash Flow'
            source_type = getattr(e, 'source_type', None) or SRC_MANUAL
        else:
            category = ''
            subcategory = ''
            party_name = ''
            party_type = ''
            description = tx.description or tx.transaction_type or ''
            note = tx.note or ''
            reference = f'TX-{tx.id}'
            origin = 'derived'
            origin_label = f'From Accounts · {tx.source_type or tx.transaction_type}'
            source_type = tx.source_type or 'SYSTEM'

        rows.append({
            'date': tx.date_posted.date() if hasattr(tx.date_posted, 'date') else tx.date_posted,
            'sort_dt': tx.date_posted,
            'type': direction,
            'tx_type_label': _cf_type_label(direction),
            'cash_in': cash_in,
            'cash_out': cash_out,
            'transfer_amount': transfer_amount,
            'amount': float(tx.amount or 0),
            'account_id': account_id,
            'account_name': account_name,
            'account_to_id': dest_account_id,
            'account_to_name': dest_account_name,
            'account_display': f"{account_name} → {dest_account_name}" if direction == CF_DIR_TRANSFER else account_name,
            'category': category,
            'subcategory': subcategory,
            'party_name': party_name,
            'party_type': party_type,
            'description': description,
            'note': note,
            'reference': reference,
            'source': 'MANUAL' if origin == 'recorded' else 'SYSTEM',
            'source_type': source_type,
            'origin': origin,
            'origin_label': origin_label,
            'created_by': (
                (getattr(e, 'created_by', '') if e else '')
                or (tx.created_by or '')
                or creator_by_source.get(
                    ((tx.source_type or '').strip().lower().replace(' ', '_'),
                     int(tx.source_id or 0)),
                    '',
                )
                or ''
            ),
            'section_label': cf_section_label(
                tx.source_type or (source_type if e else None),
                tx.transaction_type,
                default=(CF_SECTION_CASH_FLOW if e else CF_SECTION_ACCOUNTS),
            ),
            'status': 'voided' if tx.is_void else 'active',
            'entry_id': e.id if e else None,
            'tx_id': tx.id,
        })
    return rows

def _cf_sort_key(row):
    d = row.get('sort_dt') or row.get('date')
    ref = row.get('reference') or ''
    if hasattr(d, 'timestamp'):
        return (d.timestamp(), ref)
    try:
        return (datetime.combine(d, datetime.min.time()).timestamp(), ref)
    except Exception:
        return (0, ref)


def filter_cash_flow_rows(rows, filters):
    out = list(rows)
    ftype = (filters.get('filter_type') or 'all').strip().lower()
    if ftype in ('cash_in', 'received', 'in'):
        out = [r for r in out if r['type'] == CF_DIR_IN]
    elif ftype in ('cash_out', 'spent', 'out'):
        out = [r for r in out if r['type'] == CF_DIR_OUT]
    elif ftype == 'transfer':
        out = [r for r in out if r['type'] == CF_DIR_TRANSFER]

    origin = (filters.get('origin') or 'all').strip().lower()
    if origin in ('derived', 'recorded'):
        out = [r for r in out if r.get('origin') == origin]
    elif origin == 'manual':
        out = [r for r in out if r.get('source') == 'MANUAL']
    elif origin == 'system':
        out = [r for r in out if r.get('source') == 'SYSTEM']
    elif origin and origin not in ('all', ''):
        out = [r for r in out if (r.get('source_type') or '').lower() == origin.lower()]

    category = (filters.get('category') or '').strip().lower()
    if category:
        out = [r for r in out if category in (r.get('category') or '').lower()]
    subcategory = (filters.get('subcategory') or '').strip().lower()
    if subcategory:
        out = [r for r in out if subcategory in (r.get('subcategory') or '').lower()]
    party_type = (filters.get('party_type') or '').strip().lower()
    if party_type:
        out = [r for r in out if (r.get('party_type') or '').lower() == party_type]
    party = (filters.get('party') or '').strip().lower()
    if party:
        out = [r for r in out if party in (r.get('party_name') or '').lower()]
    account_id = filters.get('account_id')
    if account_id:
        out = [r for r in out if r.get('account_id') == account_id or r.get('account_to_id') == account_id]
    notes = (filters.get('notes') or '').strip().lower()
    if notes:
        out = [r for r in out if notes in (r.get('note') or '').lower()]
    reference = (filters.get('reference') or '').strip().lower()
    if reference:
        out = [r for r in out if reference in (r.get('reference') or '').lower()]
    description = (filters.get('description') or '').strip().lower()
    if description:
        out = [r for r in out if description in (r.get('description') or '').lower()]
    created_by = (filters.get('created_by') or '').strip().lower()
    if created_by:
        out = [r for r in out if created_by in (r.get('created_by') or '').lower()]
    status = (filters.get('status') or 'active').strip().lower()
    if status in ('active', 'voided'):
        out = [r for r in out if (r.get('status') or 'active') == status]
    try:
        amin = filters.get('amount_min')
        if amin not in (None, ''):
            amin = float(amin)
            out = [r for r in out if float(r.get('amount') or 0) >= amin]
    except (TypeError, ValueError):
        pass
    try:
        amax = filters.get('amount_max')
        if amax not in (None, ''):
            amax = float(amax)
            out = [r for r in out if float(r.get('amount') or 0) <= amax]
    except (TypeError, ValueError):
        pass
    q = (filters.get('q') or '').strip().lower()
    if q:
        def _blob(r):
            return ' '.join([
                str(r.get('entry_id') or ''),
                str(r.get('reference') or ''),
                str(r.get('description') or ''),
                str(r.get('note') or ''),
                str(r.get('party_name') or ''),
                str(r.get('category') or ''),
                str(r.get('subcategory') or ''),
                str(r.get('account_display') or ''),
            ]).lower()
        out = [r for r in out if q in _blob(r)]
    return out


def apply_running_balance(rows, opening_balance, account_id=None):
    running = float(opening_balance or 0)
    for row in rows:
        if (row.get('status') or 'active') == 'voided':
            row['running_balance'] = running
            continue
        if account_id:
            if row['type'] == CF_DIR_IN and row.get('account_id') == account_id:
                running += float(row.get('cash_in') or 0)
            elif row['type'] == CF_DIR_OUT and row.get('account_id') == account_id:
                running -= float(row.get('cash_out') or 0)
            elif row['type'] == CF_DIR_TRANSFER:
                if row.get('account_to_id') == account_id:
                    running += float(row.get('transfer_amount') or 0)
                if row.get('account_id') == account_id:
                    running -= float(row.get('transfer_amount') or 0)
        else:
            running += float(row.get('cash_in') or 0) - float(row.get('cash_out') or 0)
        row['running_balance'] = running
    return running


def summarize_cash_flow_rows(rows, account_id=None):
    active = [r for r in rows if (r.get('status') or 'active') == 'active']
    total_in = sum(float(r.get('cash_in') or 0) for r in active)
    total_out = sum(float(r.get('cash_out') or 0) for r in active)
    transfer_in = 0.0
    transfer_out = 0.0
    for r in active:
        if r.get('type') != CF_DIR_TRANSFER:
            continue
        amt = float(r.get('transfer_amount') or 0)
        if account_id:
            if r.get('account_to_id') == account_id:
                transfer_in += amt
            if r.get('account_id') == account_id:
                transfer_out += amt
        else:
            transfer_in += amt
            transfer_out += amt
    breakdown_cat, breakdown_party, breakdown_account = {}, {}, {}
    for r in active:
        ck = (r.get('category') or '—').strip() or '—'
        breakdown_cat.setdefault(ck, {'in': 0.0, 'out': 0.0, 'transfer': 0.0})
        breakdown_cat[ck]['in'] += float(r.get('cash_in') or 0)
        breakdown_cat[ck]['out'] += float(r.get('cash_out') or 0)
        breakdown_cat[ck]['transfer'] += float(r.get('transfer_amount') or 0)
        pk = ((r.get('party_type') or '') + ' · ' + (r.get('party_name') or '—')).strip(' ·')
        breakdown_party.setdefault(pk, {'in': 0.0, 'out': 0.0, 'transfer': 0.0})
        breakdown_party[pk]['in'] += float(r.get('cash_in') or 0)
        breakdown_party[pk]['out'] += float(r.get('cash_out') or 0)
        breakdown_party[pk]['transfer'] += float(r.get('transfer_amount') or 0)
        ak = (r.get('account_display') or r.get('account_name') or '—').strip() or '—'
        breakdown_account.setdefault(ak, {'in': 0.0, 'out': 0.0, 'transfer': 0.0})
        breakdown_account[ak]['in'] += float(r.get('cash_in') or 0)
        breakdown_account[ak]['out'] += float(r.get('cash_out') or 0)
        breakdown_account[ak]['transfer'] += float(r.get('transfer_amount') or 0)
    return {
        'total_cash_in': total_in,
        'total_cash_out': total_out,
        'total_transfer_in': transfer_in,
        'total_transfer_out': transfer_out,
        'breakdown_cat': breakdown_cat,
        'breakdown_party': breakdown_party,
        'breakdown_account': breakdown_account,
    }


def categories_for_direction(direction, include_inactive=False):
    direction = _cf_normalize_direction(direction)
    q = CashFlowCategory.query
    if not include_inactive:
        q = q.filter(CashFlowCategory.is_active == True)
    rows = q.order_by(CashFlowCategory.sort_order, CashFlowCategory.name).all()
    if direction in (CF_DIR_IN, CF_DIR_OUT):
        rows = [c for c in rows if (c.direction or 'both') in (direction, 'both')]
    return rows


def subcategories_for_category(category_id, include_inactive=False):
    q = CashFlowSubcategory.query.filter(CashFlowSubcategory.category_id == category_id)
    if not include_inactive:
        q = q.filter(CashFlowSubcategory.is_active == True)
    return q.order_by(CashFlowSubcategory.name).all()


