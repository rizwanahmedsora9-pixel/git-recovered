"""Concurrent login sessions (many users / many IPs at once)."""
from __future__ import annotations

import secrets
from datetime import timedelta

from flask import current_app, request, session
from sqlalchemy.orm import Session as SASession

from models import db, UserLoginSession
from utils.audit import audit_log


def _client_ip():
    forwarded = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    return forwarded or (request.remote_addr or '')


def _ua():
    return (request.headers.get('User-Agent') or '')[:300]


def _now():
    try:
        from app.services.time_money import pk_now
        return pk_now()
    except Exception:
        from datetime import datetime
        return datetime.utcnow()


def open_login_session(user):
    """Record a new browser session. Never closes other users or other IPs."""
    sid = secrets.token_hex(16)
    session['ams_sid'] = sid
    # The row is written right below, so that *is* this session's first
    # heartbeat: don't make the next request rewrite it immediately.
    import time as _time
    session['ams_sid_touch'] = _time.time()
    session.permanent = True
    row = UserLoginSession(
        sid=sid,
        user_id=user.id,
        username=user.username,
        role=user.role,
        ip=_client_ip(),
        user_agent=_ua(),
    )
    try:
        bind = db.session.get_bind()
        with SASession(bind) as s:
            s.add(row)
            s.commit()
    except Exception:
        try:
            db.session.add(row)
            db.session.commit()
        except Exception:
            db.session.rollback()
    audit_log(
        user,
        'auth.login',
        f'ip={_client_ip()} role={user.role} sid={sid} concurrent=allowed',
    )
    return sid


# How often one browser session rewrites its ``last_seen_at``.  The session list
# (``list_active_sessions``) treats a row as live for 45 minutes, so a coarse
# heartbeat is enough; writing it every minute meant a database write on nearly
# every fifth page view for no visible benefit.
TOUCH_INTERVAL_SECONDS = 300
# A heartbeat is not worth waiting for: if another writer (a scheduled backup, an
# import, a report) holds the SQLite lock, skip this one and try again on a later
# request rather than making the user's page wait out the busy timeout.
TOUCH_BUSY_TIMEOUT_MS = 250


def _write_heartbeat_sqlite(sid, seen, ip):
    """Write the heartbeat on its own short-lived connection.

    Deliberately *not* the pooled request connection: changing ``busy_timeout``
    on a pooled connection would apply it to every later request as well.  This
    connection waits at most ``TOUCH_BUSY_TIMEOUT_MS`` and gives up.
    """
    import sqlite3

    db_path = current_app.config.get('APP_DB_PATH')
    if not db_path:
        raise RuntimeError('no database path configured')
    connection = sqlite3.connect(str(db_path), timeout=TOUCH_BUSY_TIMEOUT_MS / 1000.0)
    try:
        connection.execute(
            'UPDATE user_login_session SET last_seen_at = ?, '
            'ip = COALESCE(?, ip) WHERE sid = ? AND ended_at IS NULL',
            (seen, ip or None, sid),
        )
        connection.commit()
    finally:
        connection.close()


def touch_login_session(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return
    sid = session.get('ams_sid')
    if not sid:
        open_login_session(user)
        return
    import time
    now_ts = time.time()
    if now_ts - float(session.get('ams_sid_touch') or 0) < TOUCH_INTERVAL_SECONDS:
        return
    # Stamp the cookie first: even if the write below is skipped because the
    # database is busy, the next four minutes of requests stay read-only.
    session['ams_sid_touch'] = now_ts
    try:
        seen = _now()
        try:
            _write_heartbeat_sqlite(sid, seen, _client_ip())
        except RuntimeError:
            # Not a configured-file SQLite app: fall back to the ORM session.
            bind = db.session.get_bind()
            with SASession(bind) as s:
                row = s.query(UserLoginSession).filter_by(sid=sid, ended_at=None).first()
                if row:
                    row.last_seen_at = seen
                    row.ip = _client_ip() or row.ip
                    s.commit()
    except Exception:
        # Busy database, deleted row, anything else: the heartbeat is optional.
        pass


def close_login_session(user):
    sid = session.pop('ams_sid', None)
    now = _now()
    if sid:
        try:
            bind = db.session.get_bind()
            with SASession(bind) as s:
                row = s.query(UserLoginSession).filter_by(sid=sid).first()
                if row and row.ended_at is None:
                    row.ended_at = now
                    s.commit()
        except Exception:
            pass
    audit_log(user, 'auth.logout', f'ip={_client_ip()} sid={sid or "-"}')


def list_active_sessions(fresh_minutes=45):
    now = _now()
    cutoff = now - timedelta(minutes=fresh_minutes)
    rows = UserLoginSession.query.filter(
        UserLoginSession.ended_at.is_(None)
    ).order_by(UserLoginSession.last_seen_at.desc()).all()
    for r in rows:
        seen = r.last_seen_at or r.created_at
        r.is_live = bool(seen and seen >= cutoff)
    return rows
