from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, make_response, send_from_directory, abort, session
from flask_login import login_required, login_user, logout_user, current_user
from datetime import datetime, date, timedelta
from sqlalchemy import func, case, text, or_, and_, exists, not_
from types import SimpleNamespace
from decimal import Decimal, ROUND_HALF_UP
import os, io, json, re, logging, calendar, time
from models import *
from app.services.api import *
from utils.audit import audit_log

from itertools import zip_longest
from sqlalchemy.orm import selectinload
from app.services.constants import (
    AUTO_BILL_NAMESPACES,
    AUTO_BILL_NS_DEFAULT,
    SALE_CATEGORY_CHOICES,
    OPEN_KHATA_CODE,
    OPEN_KHATA_NAME,
    ENDPOINT_PERMISSION_MAP,
)
from models import MaterialCategory

bp = Blueprint("masters", __name__)


def ensure_client_category_options():
    """Backfill category masters from existing client records and return choices.

    Client.category remains a string for compatibility with transaction/import
    code, while this master list makes categories selectable and manageable in
    the client ledger UI.
    """
    names_by_key = {"general": "General"}
    for (value,) in db.session.query(Client.category).all():
        name = str(value).strip() if value else ""
        if name:
            names_by_key.setdefault(name.casefold(), name)
    names = list(names_by_key.values())
    existing = {
        str(row.name).strip().casefold(): row
        for row in ClientCategory.query.filter_by(is_active=True).all()
        if row.name and str(row.name).strip()
    }
    changed = False
    for name in sorted(names, key=str.casefold):
        if name.casefold() not in existing:
            db.session.add(ClientCategory(name=name))
            changed = True
    if changed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    return ClientCategory.query.filter_by(is_active=True).order_by(ClientCategory.name.asc()).all()


def resolve_client_category(raw_name, create=True):
    """Return the canonical client category name for a submitted value.

    Categories are user-managed (``/client-ledger/categories``), but client
    records, legacy imports and API callers still submit free-text category
    names.  A name that is not yet in the master is registered on the fly
    (``create=True``) so those callers keep working and the category becomes
    selectable/renamable in the UI afterwards.  Returns ``None`` for an empty
    or over-long value.
    """
    name = str(raw_name or '').strip()
    if not name or len(name) > 50:
        return None
    row = ClientCategory.query.filter(
        func.lower(func.trim(ClientCategory.name)) == name.casefold()
    ).first()
    if row:
        if not row.is_active:
            row.is_active = True
        return row.name
    if not create:
        return None
    db.session.add(ClientCategory(name=name, is_active=True))
    try:
        db.session.flush()
    except Exception:
        db.session.rollback()
        row = ClientCategory.query.filter(
            func.lower(func.trim(ClientCategory.name)) == name.casefold()
        ).first()
        return row.name if row else None
    return name
