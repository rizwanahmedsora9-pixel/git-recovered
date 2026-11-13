#!/usr/bin/env python3
"""AMS maintenance CLI for cron/hosting scheduled tasks.

Daily database backup (one file per day, outside instance/, emailed):
    .venv/bin/python tools/maintenance.py backup-daily

Audit the old automatic "balance reconciliation" adjustments (report only by
default; ``--apply`` deletes only provable rows and re-derives the balances):
    .venv/bin/python tools/maintenance.py recon-audit [--apply]

The legacy full-folder backup (still due-interval based):
    .venv/bin/python tools/maintenance.py backup-if-due

Both commands are idempotent for the period and use a cross-process lock, so
overlapping scheduler and WSGI fallback runs are safe.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from app.services.maintenance import (
    cleanup_owned_temp,
    create_backup,
    maintenance_status,
    restore_backup,
    run_backup_if_due,
    validate_backup,
)
from app.services import daily_backup as daily


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe AMS backup and storage maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup-if-due", help="create one current backup only when due")
    sub.add_parser("backup-now", help="create and validate a backup immediately")
    daily_run = sub.add_parser(
        "backup-daily",
        help="create today's single database file (outside instance/) and email it",
    )
    daily_run.add_argument("--force", action="store_true",
                           help="run even if today's backup already exists")
    daily_run.add_argument("--no-email", action="store_true", help="save the file without emailing it")
    sub.add_parser("daily-status", help="print the daily backup folder and files as JSON")
    sub.add_parser("status", help="print storage and backup health as JSON")
    sub.add_parser("cleanup-temp", help="remove only stale maintenance-owned temp paths")
    recon = sub.add_parser(
        "recon-audit",
        help="report (or with --apply, delete) provable [RECON:ACCOUNT:*] adjustment rows",
    )
    recon.add_argument("--apply", action="store_true",
                       help="delete the provable rows and re-derive the affected balances")
    verify = sub.add_parser("verify", help="fully validate one backup without restoring it")
    verify.add_argument("path")
    restore = sub.add_parser("restore", help="restore during a controlled maintenance window")
    restore.add_argument("path")
    restore.add_argument("--confirm", action="store_true", help="required acknowledgement")
    args = parser.parse_args()

    app = create_app({"BACKUP_EMBEDDED_SCHEDULER": False})
    try:
        with app.app_context():
            if args.command == "backup-if-due":
                result = run_backup_if_due(app)
            elif args.command == "backup-now":
                result = create_backup(app, reason="manual-cli")
            elif args.command == "backup-daily":
                result = daily.run_daily_backup(app, force=args.force, send_email=not args.no_email)
            elif args.command == "daily-status":
                result = daily.status_snapshot(app)
            elif args.command == "status":
                result = maintenance_status(app)
            elif args.command == "cleanup-temp":
                result = {"removed": cleanup_owned_temp(app)}
            elif args.command == "recon-audit":
                from tools.recon_cleanup import audit
                result = audit(apply=args.apply)
            elif args.command == "verify":
                result = validate_backup(args.path)
            elif args.command == "restore":
                if not args.confirm:
                    parser.error("restore requires --confirm and a controlled maintenance window")
                result = restore_backup(app, args.path)
            else:  # pragma: no cover
                parser.error("unknown command")
            print(json.dumps(result, indent=2, default=str))
            return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
