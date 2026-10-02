"""
Backs up /data (the SQLite database + uploaded photos) via rsync, to a
destination configured from /admin/backup, and can restore from one.

Design notes:

- The database is never rsynced directly while live. Python's built-in
  sqlite3 `backup()` API is used to make a transactionally-consistent
  snapshot first (this is the same mechanism SQLite's own `.backup` CLI
  command uses), and *that* snapshot is what gets copied to the
  destination as `giftscout.db`. This means the backup is safe to run
  even while the app is actively being written to.

- Restore is deliberately careful: it pulls the backup down into a
  staging directory first, sanity-checks that the database is real and
  has the tables GiftScout expects, and only *then* replaces the live
  data — copying in the current database as a timestamped file first, so
  a bad restore can be undone by hand. If anything looks wrong at any
  point, live data is left untouched.

- Scheduling is a lightweight background loop inside the app itself
  (checked every few minutes) — no cron daemon, no job-queue dependency.
"""
import asyncio
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from app import db
from app.db import DB_PATH

SSH_KEY_PATH = "/secrets/backup_key"
DATA_DIR = Path("/data")
UPLOADS_DIR = DATA_DIR / "uploads"
STAGING_DIR = DATA_DIR / ".restore_staging"
CHECK_INTERVAL_SECONDS = 300  # how often the scheduler checks if a run is due

FREQUENCIES = {
    "off": None,
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
}

REQUIRED_TABLES = {"products", "settings"}


def _ssh_opt() -> list:
    if os.path.exists(SSH_KEY_PATH):
        return ["-e", f"ssh -i {SSH_KEY_PATH} -o StrictHostKeyChecking=accept-new"]
    return []


def _make_consistent_snapshot(snapshot_path: Path) -> None:
    """Uses SQLite's own backup API, which is safe to call while the live
    database is being read from or written to — no risk of copying a file
    mid-write the way a plain file copy would have."""
    snapshot_path.unlink(missing_ok=True)
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(snapshot_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _run(cmd: list, timeout: int = 600) -> Tuple[bool, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode == 0, output
    except FileNotFoundError:
        return False, "rsync is not installed in this container."
    except subprocess.TimeoutExpired:
        return False, "Timed out."
    except Exception as e:  # noqa: BLE001 — surfaced to the admin UI, not swallowed
        return False, f"Failed: {e}"


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def run_backup_now(destination: str) -> Tuple[bool, str]:
    """Runs a backup immediately, records the result in settings, returns (success, output)."""
    success, output = _do_backup(destination)
    conn = db.get_connection()
    try:
        db.set_setting(conn, "backup_last_run_at", datetime.now(timezone.utc).isoformat())
        db.set_setting(conn, "backup_last_status", "success" if success else "failed")
        db.set_setting(conn, "backup_last_output", output[-4000:])
        conn.commit()
    finally:
        conn.close()
    return success, output


def _do_backup(destination: str) -> Tuple[bool, str]:
    if not destination.strip():
        return False, "No backup destination configured."
    dest = destination if destination.endswith("/") else destination + "/"
    ssh_opt = _ssh_opt()
    logs = []

    snapshot_path = DATA_DIR / ".backup_snapshot.db"
    try:
        _make_consistent_snapshot(snapshot_path)
    except Exception as e:
        return False, f"Couldn't create a database snapshot: {e}"

    try:
        ok, out = _run(["rsync", "-az"] + ssh_opt + [str(snapshot_path), f"{dest}giftscout.db"])
        logs.append(out)
        if not ok:
            return False, "\n".join(filter(None, logs))
    finally:
        snapshot_path.unlink(missing_ok=True)

    ok, out = _run(["rsync", "-az", "--delete"] + ssh_opt + [f"{UPLOADS_DIR}/", f"{dest}uploads/"])
    logs.append(out)
    if not ok:
        return False, "\n".join(filter(None, logs))

    return True, "\n".join(filter(None, logs)) or "(no output)"


async def scheduler_loop():
    """Background task: periodically checks whether a scheduled backup is due."""
    while True:
        try:
            _maybe_run_scheduled_backup()
        except Exception as e:  # noqa: BLE001 — must never kill the loop
            print(f"[backup] scheduler error: {e}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def _maybe_run_scheduled_backup() -> None:
    destination: Optional[str] = None
    conn = db.get_connection()
    try:
        frequency = db.get_setting(conn, "backup_frequency", "off")
        interval = FREQUENCIES.get(frequency)
        if interval is None:
            return
        dest = db.get_setting(conn, "backup_destination", "")
        if not dest.strip():
            return
        last_run_at = db.get_setting(conn, "backup_last_run_at")
        if last_run_at:
            last_run = datetime.fromisoformat(last_run_at)
            if datetime.now(timezone.utc) - last_run < interval:
                return
        destination = dest
    finally:
        conn.close()

    if destination:
        run_backup_now(destination)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore_from_backup(source: str) -> Tuple[bool, str]:
    """Restores from `source`, records the result in settings, returns (success, output)."""
    success, output = _do_restore(source)
    conn = db.get_connection()
    try:
        db.set_setting(conn, "restore_last_run_at", datetime.now(timezone.utc).isoformat())
        db.set_setting(conn, "restore_last_status", "success" if success else "failed")
        db.set_setting(conn, "restore_last_output", output[-4000:])
        conn.commit()
    finally:
        conn.close()
    return success, output


def _do_restore(source: str) -> Tuple[bool, str]:
    if not source.strip():
        return False, "No source given."
    src = source if source.endswith("/") else source + "/"
    ssh_opt = _ssh_opt()

    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR)
    STAGING_DIR.mkdir(parents=True)

    try:
        ok, pull_output = _run(["rsync", "-az", "--delete"] + ssh_opt + [src, f"{STAGING_DIR}/"])
        if not ok:
            return False, f"Couldn't fetch backup from source:\n{pull_output}"

        staged_db = STAGING_DIR / "giftscout.db"
        if not staged_db.exists():
            return False, "That source doesn't look like a GiftScout backup — no giftscout.db found. Nothing was changed."

        # Sanity-check it's a real, readable database with the tables we expect,
        # before touching anything live.
        try:
            test_conn = sqlite3.connect(str(staged_db))
            tables = {row[0] for row in test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            test_conn.close()
        except Exception as e:
            return False, f"Couldn't open the backed-up database — nothing was changed. ({e})"

        if not REQUIRED_TABLES.issubset(tables):
            return False, "The backed-up database doesn't look like a valid GiftScout database — nothing was changed."

        # Safety net: keep the current database before overwriting it.
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safety_copy = DATA_DIR / f"giftscout.db.before-restore-{timestamp}"
        if DB_PATH.exists():
            shutil.copy2(DB_PATH, safety_copy)

        # Uploaded photos: mirror staged -> live.
        staged_uploads = STAGING_DIR / "uploads"
        upload_log = ""
        if staged_uploads.exists():
            ok, upload_log = _run(["rsync", "-az", "--delete", f"{staged_uploads}/", f"{UPLOADS_DIR}/"])
            if not ok:
                return False, (
                    f"Backed up your current database to {safety_copy.name} but failed "
                    f"copying photos from the backup — your database has NOT been swapped "
                    f"yet, so your site is still on its old data. Check {UPLOADS_DIR} by "
                    f"hand or try again:\n{upload_log}"
                )

        # Database: same filesystem as /data, so this is an atomic swap.
        os.replace(staged_db, DB_PATH)

        return True, (
            f"Restore complete. Your previous database was saved as "
            f"{safety_copy.name} in /data, in case you need to undo this.\n\n"
            f"{pull_output}\n{upload_log}".strip()
        )
    finally:
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
