"""Automatic daily database backup — saved outside ``instance/`` and emailed.

What happens once a day:

1.  The live SQLite database is copied with SQLite's own online backup API
    (safe while the app keeps running, WAL mode included).
2.  The copy is validated (``PRAGMA integrity_check`` + the application tables
    must be present) and only then published, so a half-written file never
    becomes "the" backup.
3.  The file is written to a folder **outside** ``instance/`` (default
    ``<project>/backups/db``; override with ``BACKUP_DIR``), named

        ams_db_YYYY-MM-DD_HHMMSS.db

    carrying the live database's own size.  By default retention is 1, so
    today's file replaces yesterday's and only one copy is kept on disk.
4.  Optionally the file is emailed as an attachment through SMTP.

Everything is idempotent per calendar day and guarded by a cross-process file
lock, so an embedded scheduler thread and an external cron command can both be
enabled — only one of them creates the day's backup.
"""
from __future__ import annotations

import logging
import json
import os
import shutil
import smtplib
import sqlite3
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path

from flask import current_app

from models import db
from app.services import state
from app.services.constants import _ENABLE_BACKGROUND_WORKERS
from app.services.maintenance import _sqlite_path
from app.services.time_money import PK_TZ, pk_now

LOG = logging.getLogger("daily-backup")


def _background_workers_enabled() -> bool:
    """True only in a process that was started to run scheduled work.

    Re-read from the environment so a test or an operator can flip it without
    reimporting the module.
    """
    override = (os.environ.get("AMS_ENABLE_BACKGROUND_WORKERS") or "").strip().lower()
    if override:
        return override in ("1", "true", "yes", "on")
    return bool(_ENABLE_BACKGROUND_WORKERS)

# Keep in sync with app.migrations (these tables must exist in any real backup).
_PROBE_TABLES = ("user", "client", "settings")

DEFAULT_MAIL_PORT = {"starttls": 587, "ssl": 465, "none": 25}

FILE_PREFIX = "ams_db_"
_STAGING_PREFIX = ".tmp-daily-db-"
_LOCK_DIR_NAME = ".daily-backup.lock"

_worker_guard = threading.Lock()
_worker_started = False
_last_attempt_utc: datetime | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class DailyBackupError(RuntimeError):
    """The daily backup could not be completed."""


class DailyBackupBusy(DailyBackupError):
    """Another process is already writing today's backup."""


def backup_dir(app=None) -> Path:
    """Folder that holds the daily database files (outside ``instance/``)."""
    cfg = (app or current_app)
    configured = str(cfg.config.get("BACKUP_DIR") or "").strip()
    project_root = Path(str(cfg.root_path)).resolve().parent
    target = Path(configured) if configured else project_root / "backups" / "db"
    if not target.is_absolute():
        target = project_root / target
    return target.resolve()


def temp_dir(app=None) -> Path:
    cfg = (app or current_app)
    return Path(str(cfg.config.get("MAINTENANCE_TEMP_DIR"))).resolve()


def get_settings(create: bool = True):
    """Return the single settings row (``None`` when *create* is False)."""
    from models import DailyDbBackupSettings

    row = db.session.get(DailyDbBackupSettings, 1)
    if row is None and create:
        row = DailyDbBackupSettings(
            id=1,
            enabled=False,
            run_at="auto",
            include_db=True,
            include_uploads=False,
            keep_days=1,
            email_db_attachment=True,
            mail_port=0,
            mail_security="starttls",
            mail_username="",
            mail_from="",
            smtp_timeout_seconds=60,
        )
        db.session.add(row)
        db.session.commit()
    return row


def _resolve_port(row) -> int:
    """Explicit port wins; 0/None falls back to the security default."""
    port = int(row.mail_port or 0)
    if port > 0:
        return port
    return DEFAULT_MAIL_PORT.get((row.mail_security or "starttls").lower(), 587)


def _smtp_password() -> str:
    # Never persisted in the database: a DB copy must not carry the credential.
    return (os.environ.get("AMS_BACKUP_SMTP_PASSWORD") or "").strip()


def _parse_recipients(raw: str | None) -> list[str]:
    seen, out = set(), []
    for part in str(raw or "").replace(";", ",").split(","):
        addr = part.strip()
        if not addr or "@" not in addr:
            continue
        if addr.lower() in seen:
            continue
        seen.add(addr.lower())
        out.append(addr)
    return out


def _parse_run_at(raw: str | None) -> tuple[str, bool]:
    """Return ``(mode, fixed_time)``.

    ``auto``  → one backup per Pakistan calendar day, whenever the app runs.
    ``HH:MM`` → once that clock time has passed today (24h cadence).
    """
    value = (raw or "auto").strip().lower()
    if value in ("", "auto"):
        return "auto", False
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError):
        return "auto", False
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return "auto", False
    return f"{hour:02d}:{minute:02d}", True


# ---------------------------------------------------------------------------
# Listing / due calculation
# ---------------------------------------------------------------------------
def _is_daily_file(path: Path) -> bool:
    return path.is_file() and path.name.startswith(FILE_PREFIX) and path.suffix == ".db"


def _list_in(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.iterdir() if _is_daily_file(p)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def list_backups(app=None) -> list[Path]:
    """Daily database files, newest first."""
    return _list_in(backup_dir(app))


def latest_backup(app=None) -> Path | None:
    files = list_backups(app)
    return files[0] if files else None


def backup_due(app, now: datetime | None = None) -> bool:
    """True when today's (Pakistan time) backup has not been made yet."""
    now_local = now or pk_now()
    latest = latest_backup(app)
    if latest is None:
        return True
    row = get_settings(create=False)
    mode, _ = _parse_run_at(getattr(row, "run_at", None))
    stamp = datetime.fromtimestamp(latest.stat().st_mtime, PK_TZ).replace(tzinfo=None)
    if mode == "auto":
        return stamp.date() < now_local.date()
    hour, minute = (int(x) for x in mode.split(":"))
    scheduled = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now_local >= scheduled and stamp < scheduled



# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _DailyLock:
    """Cross-process mkdir lock with stale-owner recovery.

    A process killed mid-backup would otherwise leave the lock directory
    behind and every later backup would report "already running" forever.  The
    lock is only broken when BOTH the age and the owner-liveness checks agree
    that the owner is gone.
    """

    def __init__(self, root: Path, stale_seconds: int = 7200):
        self.lock_dir = root / _LOCK_DIR_NAME
        self.stale_seconds = max(300, int(stale_seconds or 7200))
        self.acquired = False

    def _owner(self) -> dict:
        try:
            return json.loads((self.lock_dir / "owner.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    def _try_acquire(self) -> bool:
        try:
            self.lock_dir.mkdir(mode=0o700)
        except FileExistsError:
            return False
        try:
            (self.lock_dir / "owner.json").write_text(
                json.dumps({"pid": os.getpid(), "created_at": _utc_now().isoformat()}),
                encoding="utf-8",
            )
        except OSError:
            pass
        return True

    def __enter__(self):
        self.lock_dir.parent.mkdir(parents=True, exist_ok=True)
        if self._try_acquire():
            self.acquired = True
            return self
        try:
            age = time.time() - self.lock_dir.stat().st_mtime
        except OSError:
            age = 0
        owner_pid = int(self._owner().get("pid") or 0)
        if age <= self.stale_seconds or _pid_alive(owner_pid):
            self.acquired = False
            return self
        LOG.warning("Clearing stale daily backup lock (owner pid %s, age %ss)", owner_pid, int(age))
        shutil.rmtree(self.lock_dir, ignore_errors=True)
        self.acquired = self._try_acquire()
        return self

    def __exit__(self, *exc_info):
        if self.acquired:
            shutil.rmtree(self.lock_dir, ignore_errors=True)
        return False


def _seal_rollback_journal(path: Path) -> None:
    """Make a copied database a single self-contained file.

    SQLite's backup API preserves the source journal mode, so a WAL database
    would otherwise be published as ``x.db`` + ``x.db-wal`` + ``x.db-shm``.
    The WAL holds committed transactions that are *not* in the main file yet,
    so a lone ``.db`` copy would silently lose data.  Switching the copy to a
    rollback journal (DELETE) checkpoints everything into the main file and
    leaves no sidecars — the copy is then one file that can be emailed and
    restored on its own.  The live database is not touched.
    """
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        conn.commit()
        if not mode or str(mode[0]).lower() != "delete":
            raise DailyBackupError(f"Could not seal the backup journal: {mode}")
    finally:
        conn.close()


def _free_target(root: Path, stem: str) -> Path:
    """First unused ``stem.db`` / ``stem_2.db`` / … — never overwrites a file.

    Two "Backup now" clicks inside the same second get two distinct files;
    retention then decides which one survives, so no good copy is ever lost.
    """
    candidate = root / f"{stem}.db"
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = root / f"{stem}_{index}.db"
        if not candidate.exists():
            return candidate
    raise DailyBackupError("Unable to allocate a free backup filename")


def _prune(root: Path, keep_days: int) -> list[str]:
    """Keep the newest *keep_days* files (each one already validated)."""
    keep = max(1, int(keep_days or 1))
    removed: list[str] = []
    for old in _list_in(root)[keep:]:
        try:
            old.unlink()
            removed.append(old.name)
        except OSError:
            LOG.warning("Could not remove old daily backup %s", old, exc_info=True)
    return removed


def _validate_database(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size <= 0:
        raise DailyBackupError(f"Backup database is missing or empty: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise DailyBackupError(f"Backup integrity check failed: {integrity}")
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            missing = [t for t in _PROBE_TABLES if t not in tables]
            if missing:
                raise DailyBackupError(f"Backup is missing expected tables: {missing}")
            users = conn.execute("SELECT COUNT(*) FROM user").fetchone()[0]
            return {"tables": len(tables), "users": int(users), "size": path.stat().st_size}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise DailyBackupError(f"Backup database is unreadable: {exc}") from exc


def create_daily_backup(app, *, reason: str = "scheduled") -> dict:
    """Copy, validate and publish one database file. Retention then applies."""
    root = backup_dir(app)
    root.mkdir(parents=True, exist_ok=True)
    source_db = _sqlite_path(app)
    if not source_db.is_file():
        raise DailyBackupError(f"Live database does not exist: {source_db}")

    free = shutil.disk_usage(root).free
    minimum = int(app.config.get("MIN_FREE_DISK_BYTES", 100 * 1024 * 1024))
    if free < minimum:
        raise DailyBackupError(
            f"Not enough free disk space for a backup ({free} bytes free, {minimum} required)"
        )

    stale = int(app.config.get("DAILY_BACKUP_LOCK_STALE_SECONDS", 7200) or 7200)
    with _DailyLock(root, stale_seconds=stale) as lock:
        if not lock.acquired:
            raise DailyBackupBusy("A daily backup is already running")

        started = pk_now()
        staging_root = temp_dir(app)
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = staging_root / f"{_STAGING_PREFIX}{os.getpid()}-{time.time_ns()}"
        staging.mkdir(mode=0o700)
        published: Path | None = None
        try:
            staged_db = staging / "database.db"
            source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=30)
            destination = sqlite3.connect(str(staged_db), timeout=30)
            try:
                source.backup(destination, pages=256, sleep=0.05)
            finally:
                destination.close()
                source.close()
            # One self-contained file before any validation/publication.
            try:
                _seal_rollback_journal(staged_db)
            finally:
                for sidecar in staged_db.parent.glob(staged_db.name + "-*"):
                    sidecar.unlink(missing_ok=True)
            report = _validate_database(staged_db)

            stem = f"{FILE_PREFIX}{started.strftime('%Y-%m-%d_%H%M%S')}"
            target = _free_target(root, stem)
            # Copy beside the destination first, then rename: the final name is
            # never a partially written file.
            partial = root / f".partial-{target.name}"
            shutil.copy2(staged_db, partial)
            os.replace(partial, target)
            _validate_database(target)
            published = target

            settings = get_settings(create=False)
            removed = _prune(root, getattr(settings, "keep_days", 1) or 1)
            LOG.info(
                "Daily database backup written: %s (%s bytes, reason=%s, removed=%s)",
                target, report["size"], reason, removed,
            )
            return {
                "ok": True,
                "path": str(target),
                "name": target.name,
                "size_bytes": report["size"],
                "tables": report["tables"],
                "users": report["users"],
                "removed": removed,
                "created_at": started,
            }
        except Exception:
            LOG.exception("Daily database backup failed (source=%s)", source_db)
            if published and published.exists():
                try:
                    published.unlink()
                except OSError:
                    pass
            raise
        finally:
            for leftover in root.glob(".partial-*"):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def _smtp_send(row, *, subject: str, body: str, attachment: Path | None) -> None:
    host = (row.mail_host or "").strip()
    if not host:
        raise DailyBackupError(
            "No mail server configured — set the SMTP host in Daily Database Backup settings."
        )
    recipients = _parse_recipients(row.recipient_emails)
    if not recipients:
        raise DailyBackupError("No recipient email address configured.")

    sender = (row.mail_from or row.mail_username or "").strip()
    if not sender:
        raise DailyBackupError("No sender address configured (mail 'from' or username).")

    security = (row.mail_security or "starttls").strip().lower()
    port = _resolve_port(row)
    timeout = max(5, int(row.smtp_timeout_seconds or 60))

    message = EmailMessage()
    company = ""
    try:
        from models import Settings

        settings_obj = Settings.query.first()
        company = (getattr(settings_obj, "company_name", "") or "").strip()
    except Exception:  # pragma: no cover - cosmetic only
        company = ""
    message["Subject"] = subject
    message["From"] = formataddr((company or "AMS Backup", sender))
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message.set_content(body)

    if attachment is not None:
        payload = attachment.read_bytes()
        message.add_attachment(
            payload,
            maintype="application",
            subtype="x-sqlite3",
            filename=attachment.name,
        )
        if len(payload) > 20 * 1024 * 1024:
            LOG.warning(
                "Backup attachment %s is %.1f MB — many mail servers reject attachments "
                "over 20-25 MB.", attachment.name, len(payload) / 1024 / 1024,
            )

    if security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(host, port, timeout=timeout)
    try:
        client.ehlo()
        if security == "starttls":
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        username = (row.mail_username or "").strip()
        if username:
            password = _smtp_password()
            if not password:
                raise DailyBackupError(
                    "The SMTP password is not set on the server. Set "
                    "AMS_BACKUP_SMTP_PASSWORD in the environment and restart the app."
                )
            client.login(username, password)
        client.send_message(message)
    finally:
        try:
            client.quit()
        except Exception:
            client.close()


def _email_body(result: dict, app, *, attached: bool = True) -> str:
    size_mb = (result.get("size_bytes") or 0) / 1024 / 1024
    source = _sqlite_path(app)
    lines = [
        "Automatic database backup",
        "",
        f"Created      : {result['created_at'].strftime('%Y-%m-%d %H:%M:%S')} (Pakistan time)",
        f"Database     : {source.name}",
        f"Backup file  : {result['name']}",
        f"Size         : {size_mb:.2f} MB",
        f"Tables       : {result.get('tables', '?')}",
        f"Users        : {result.get('users', '?')}",
        f"Stored in    : {Path(result['path']).parent}",
        "",
    ]
    if result.get("removed"):
        lines.append("Replaced older backup(s): " + ", ".join(result["removed"]))
        lines.append("")
    if attached:
        lines += [
            "The database file is attached. Keep this email: it is an independent",
            "copy of the data in case the PC that runs the software is lost.",
        ]
    else:
        lines += [
            "The database file was saved on the server only (attachments are off).",
            "Download it from Settings → Daily Database Backup.",
        ]
    lines += ["", "— AMS automatic backup"]
    return "\n".join(lines)


def run_daily_backup(app, *, force: bool = False, send_email: bool = True) -> dict:
    """Create today's backup (once per day unless *force*) and email it.

    Returns a dict with ``created``/``emailed``/``message``.  Ordinary failures
    come back as ``ok: False`` with a readable message instead of raising, so
    the scheduler never dies and the admin sees what went wrong.
    """
    started = pk_now()
    row = get_settings(create=True)
    trigger = "manual" if force else "auto"
    already_today = latest_backup(app) is not None and not backup_due(app)
    if not force and already_today:
        return {"ok": True, "created": False, "emailed": False,
                "message": "Today's database backup already exists"}

    try:
        created = create_daily_backup(app, reason=trigger)
        result: dict = {**created, "ok": True, "created": True}
    except DailyBackupBusy as exc:
        return {"ok": True, "created": False, "emailed": False, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a failed copy must never kill the scheduler
        LOG.warning("Daily database backup failed: %s", exc)
        try:
            _record(row, started, None, status="failed", message=str(exc), email_status=None,
                    trigger=trigger)
            row.last_status = "failed"
            row.last_message = str(exc)[:1000]
            row.last_run_at = pk_now()
            db.session.commit()
        except Exception:
            db.session.rollback()
        return {"ok": False, "created": False, "emailed": False, "message": str(exc)}

    email_status, email_error = None, None
    should_email = bool(send_email and row.enabled and (row.mail_host or "").strip())
    if should_email:
        attachment = Path(result["path"]) if row.email_db_attachment else None
        try:
            _smtp_send(
                row,
                subject=(
                    f"Database backup {result['created_at'].strftime('%Y-%m-%d')} — "
                    f"{_sqlite_path(app).stem}"
                ),
                body=_email_body(result, app, attached=attachment is not None),
                attachment=attachment,
            )
            email_status = "sent"
            message = f"Database backup created and emailed to {row.recipient_emails}"
        except Exception as exc:  # noqa: BLE001 - surfaced to the admin
            email_status, email_error = "failed", str(exc)[:1000]
            message = f"Database backup created, but the email failed: {exc}"
            LOG.warning("Daily backup email failed: %s", exc)
    else:
        message = "Database backup created (email disabled)"

    result.update(
        emailed=(email_status == "sent"),
        email_status=email_status,
        email_error=email_error,
        message=message,
    )
    _record(row, started, result, status="success", message=message,
            email_status=email_status, email_error=email_error, trigger=trigger)
    row.last_status = "success" if email_status != "failed" else "email_failed"
    row.last_message = message[:1000]
    row.last_run_at = pk_now()
    db.session.commit()
    return result


def _history_model():
    from models import DailyDbBackupHistory

    return DailyDbBackupHistory


def _record(row, started, result, *, status, message, email_status, email_error=None,
            trigger="auto") -> None:
    from models import DailyDbBackupHistory

    db.session.add(DailyDbBackupHistory(
        trigger_type=trigger,
        status=status,
        backup_path=(result or {}).get("path"),
        backup_name=(result or {}).get("name"),
        size_bytes=(result or {}).get("size_bytes"),
        recipients=(row.recipient_emails or "").strip()[:500] if row else None,
        email_status=email_status,
        email_error=(email_error or None),
        message=(message or "")[:1000],
        started_at=started,
        finished_at=pk_now(),
    ))
    db.session.commit()


def send_test_email(app) -> tuple[bool, str]:
    row = get_settings(create=True)
    try:
        latest = latest_backup(app)
        _smtp_send(
            row,
            subject="AMS backup email test",
            body=(
                "This is a test email from the AMS daily database backup settings.\n\n"
                "If you received it, the mail server settings work and the daily "
                "backup can be emailed.\n"
            ),
            attachment=latest,
        )
        return True, f"Test email sent to {row.recipient_emails}"
    except Exception as exc:  # noqa: BLE001 - shown to the admin
        return False, str(exc)


def status_snapshot(app) -> dict:
    """Small JSON-friendly summary for the settings page."""
    files = list_backups(app)
    latest = files[0] if files else None
    return {
        "dir": str(backup_dir(app)),
        "count": len(files),
        "files": [
            {
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "modified": datetime.fromtimestamp(p.stat().st_mtime, PK_TZ)
                .replace(tzinfo=None)
                .strftime("%Y-%m-%d %H:%M:%S"),
            }
            for p in files[:50]
        ],
        "latest": latest.name if latest else None,
        "password_configured": bool(_smtp_password()),
    }


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def _scheduler_worker(app) -> None:
    global _last_attempt_utc
    poll = max(30, min(900, int(app.config.get("DAILY_BACKUP_POLL_SECONDS", 120))))
    while True:
        try:
            from app.services import state as _state
            if getattr(_state, 'WIPE_IN_PROGRESS', False):
                # An admin wipe owns the database right now; a backup copy
                # would take the lock it needs. Try again on the next poll.
                time.sleep(poll)
                continue
            with app.app_context():
                row = get_settings(create=False)
                if row is not None and row.enabled:
                    now_utc = datetime.utcnow()
                    if _last_attempt_utc is None or (now_utc - _last_attempt_utc) >= timedelta(seconds=poll):
                        _last_attempt_utc = now_utc
                        if backup_due(app):
                            run_daily_backup(app, force=False, send_email=True)
        except Exception:
            LOG.exception("Scheduled daily backup attempt failed; existing backups were kept")
        time.sleep(poll)


def start_daily_backup_scheduler(app) -> bool:
    """Start the in-app scheduler thread once per process.

    Controlled by ``AMS_DAILY_BACKUP_SCHEDULER`` (default on).  The per-day
    check and the file lock make it harmless next to a cron job running
    ``tools/maintenance.py backup-daily``.
    """
    global _worker_started
    # Never schedule from a web/WSGI worker: the copy is taken from the live
    # database and polls the write lock.  ``wsgi.py`` leaves
    # AMS_ENABLE_BACKGROUND_WORKERS unset; cron runs the backup explicitly via
    # ``tools/maintenance.py backup-daily``.
    if not _background_workers_enabled():
        return False
    if app.config.get("TESTING") or not app.config.get("DAILY_BACKUP_SCHEDULER", True):
        return False
    with _worker_guard:
        if _worker_started:
            return False
        thread = threading.Thread(target=_scheduler_worker, args=(app,), daemon=True,
                                  name="ams-daily-db-backup")
        thread.start()
        _worker_started = True
        return True


def ensure_worker_started(app) -> None:
    """Start on the first request if this process did not start it at boot."""
    if not state.DAILY_BACKUP_WORKER_STARTED:
        if start_daily_backup_scheduler(app):
            state.DAILY_BACKUP_WORKER_STARTED = True
