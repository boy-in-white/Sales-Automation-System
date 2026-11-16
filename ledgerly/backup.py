"""Online PostgreSQL/SQLite backups with retention."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from config import (
    BACKUP_DIR,
    BACKUP_INTERVAL_HOURS,
    BACKUP_RETENTION_DAYS,
    DATABASE_URL,
    DATABASE_PATH,
)
from db_runtime import database_backend, sqlite_path


LOGGER = logging.getLogger(__name__)


def _find_pg_dump() -> str | None:
    executable = shutil.which("pg_dump")
    if executable:
        return executable
    # Homebrew versioned PostgreSQL formulas are keg-only, so their tools may
    # not be in PATH even though PostgreSQL itself is installed and running.
    for prefix in ("/opt/homebrew", "/usr/local"):
        candidate = Path(prefix) / "opt" / "postgresql@18" / "bin" / "pg_dump"
        if candidate.is_file():
            return str(candidate)
    return None


def create_backup() -> Path | None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    BACKUP_DIR.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    if database_backend(DATABASE_URL) == "postgresql":
        executable = _find_pg_dump()
        if not executable:
            raise RuntimeError("pg_dump is not installed or is not in PATH")
        parsed = urlsplit(DATABASE_URL)
        database_name = unquote(parsed.path.lstrip("/"))
        if not database_name:
            raise RuntimeError("DATABASE_URL does not contain a database name")
        environment = os.environ.copy()
        environment["PGHOST"] = parsed.hostname or "127.0.0.1"
        environment["PGPORT"] = str(parsed.port or 5432)
        if parsed.username:
            environment["PGUSER"] = unquote(parsed.username)
        if parsed.password:
            environment["PGPASSWORD"] = unquote(parsed.password)
        query = parse_qs(parsed.query)
        if query.get("sslmode"):
            environment["PGSSLMODE"] = query["sslmode"][0]
        destination = BACKUP_DIR / f"postgres-{stamp}.dump"
        partial = destination.with_name(destination.name + ".partial")
        command = [
            executable,
            "--format=custom",
            "--compress=6",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(partial),
            database_name,
        ]
        try:
            try:
                subprocess.run(
                    command,
                    check=True,
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                # Minimal PostgreSQL builds can omit gzip support.  An
                # uncompressed custom dump is still complete and restorable.
                if "does not support compression" not in (exc.stderr or ""):
                    message = (exc.stderr or "pg_dump failed").strip()[:500]
                    raise RuntimeError(message) from None
                command[2] = "--compress=0"
                try:
                    subprocess.run(
                        command,
                        check=True,
                        env=environment,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                except subprocess.CalledProcessError as retry_exc:
                    message = (retry_exc.stderr or "pg_dump failed").strip()[:500]
                    raise RuntimeError(message) from None
            if not partial.is_file() or partial.stat().st_size <= 0:
                raise RuntimeError("pg_dump did not create a valid backup file")
            partial.chmod(0o600)
            partial.replace(destination)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        return destination

    source_path = sqlite_path(DATABASE_URL, Path(DATABASE_PATH))
    if not source_path.exists():
        return None
    destination = BACKUP_DIR / f"sqlite-{stamp}.sqlite3"
    partial = destination.with_name(destination.name + ".partial")
    try:
        with sqlite3.connect(source_path) as source, sqlite3.connect(partial) as target:
            source.backup(target)
        if not partial.is_file() or partial.stat().st_size <= 0:
            raise RuntimeError("SQLite did not create a valid backup file")
        partial.chmod(0o600)
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return destination


def prune_backups() -> int:
    if not BACKUP_DIR.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKUP_RETENTION_DAYS)
    removed = 0
    paths = (
        list(BACKUP_DIR.glob("*.sqlite3"))
        + list(BACKUP_DIR.glob("*.dump"))
        + list(BACKUP_DIR.glob("*.partial"))
    )
    for path in paths:
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            path.unlink()
            removed += 1
    return removed


def backup_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            path = create_backup()
            removed = prune_backups()
            if path:
                LOGGER.info("Created database backup %s; pruned=%s", path.name, removed)
        except Exception:
            LOGGER.exception("Database backup failed")
        stop_event.wait(BACKUP_INTERVAL_HOURS * 3600)


def start_backup_worker() -> tuple[threading.Thread, threading.Event]:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=backup_loop,
        args=(stop_event,),
        name="backup-worker",
        daemon=True,
    )
    thread.start()
    return thread, stop_event

