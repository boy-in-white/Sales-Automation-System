"""Operational tracking data in PostgreSQL, with SQLite test compatibility."""

from __future__ import annotations

import csv
import io
import threading
import time
from pathlib import Path
from typing import Any

from config import DATABASE_PATH, DATABASE_URL as CONFIG_DATABASE_URL
from db_runtime import (
    Session,
    bootstrap_postgres_schemas,
    database_backend,
    transaction,
)


# These module variables remain assignable so isolated SQLite tests and the
# migration tool can select a temporary database without changing config.py.
DATABASE_URL = CONFIG_DATABASE_URL
DB_PATH = DATABASE_PATH
_LOCK = threading.RLock()


def _transaction():
    return transaction(
        DATABASE_URL,
        schemas=("bot", "public"),
        sqlite_default_path=Path(DB_PATH),
    )


def _ensure_column(
    session: Session,
    table: str,
    column: str,
    sqlite_ddl: str,
    postgres_ddl: str | None = None,
) -> None:
    if session.backend == "postgresql":
        session.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} "
            f"{postgres_ddl or sqlite_ddl}"
        )
        return
    columns = {str(row["name"]) for row in session.all(f"PRAGMA table_info({table})")}
    if column not in columns:
        session.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sqlite_ddl}")


def init_db() -> None:
    bootstrap_postgres_schemas(DATABASE_URL, ("bot",))
    backend = database_backend(DATABASE_URL)
    integer_type = "BIGINT" if backend == "postgresql" else "INTEGER"
    with _LOCK, _transaction() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                applied_at {integer_type} NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS shipments (
                ttn TEXT PRIMARY KEY,
                account_name TEXT NOT NULL DEFAULT '',
                profile_name TEXT NOT NULL DEFAULT '',
                shipment_kind TEXT NOT NULL DEFAULT 'Наложка',
                description TEXT NOT NULL DEFAULT '',
                cost TEXT NOT NULL DEFAULT '',
                cost_text TEXT NOT NULL DEFAULT '',
                olx_order_id TEXT NOT NULL DEFAULT '',
                source_text TEXT NOT NULL DEFAULT '',
                source_user_id {integer_type} NOT NULL DEFAULT 0,
                source_user_name TEXT NOT NULL DEFAULT '',
                source_chat_id {integer_type} NOT NULL DEFAULT 0,
                source_thread_id {integer_type} NOT NULL DEFAULT 0,
                source_message_id {integer_type} NOT NULL DEFAULT 0,
                tracking_chat_id {integer_type} NOT NULL DEFAULT 0,
                tracking_thread_id {integer_type} NOT NULL DEFAULT 0,
                tracking_message_id {integer_type} NOT NULL DEFAULT 0,
                tracking_claimed_at {integer_type} NOT NULL DEFAULT 0,
                last_status TEXT NOT NULL DEFAULT '',
                last_status_code TEXT NOT NULL DEFAULT '',
                status_hash TEXT NOT NULL DEFAULT '',
                received_at {integer_type} NOT NULL DEFAULT 0,
                onec_sent_at {integer_type} NOT NULL DEFAULT 0,
                onec_claimed_at {integer_type} NOT NULL DEFAULT 0,
                active SMALLINT NOT NULL DEFAULT 1,
                next_check_at {integer_type} NOT NULL DEFAULT 0,
                last_checked_at {integer_type} NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at {integer_type} NOT NULL,
                updated_at {integer_type} NOT NULL
            )
            """
        )
        migrations = {
            "profile_name": ("TEXT NOT NULL DEFAULT ''", None),
            "shipment_kind": ("TEXT NOT NULL DEFAULT 'Наложка'", None),
            "cost_text": ("TEXT NOT NULL DEFAULT ''", None),
            "olx_order_id": ("TEXT NOT NULL DEFAULT ''", None),
            "source_text": ("TEXT NOT NULL DEFAULT ''", None),
            "source_user_id": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "source_user_name": ("TEXT NOT NULL DEFAULT ''", None),
            "source_chat_id": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "source_thread_id": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "source_message_id": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "tracking_chat_id": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "tracking_thread_id": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "tracking_message_id": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "tracking_claimed_at": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "last_status": ("TEXT NOT NULL DEFAULT ''", None),
            "last_status_code": ("TEXT NOT NULL DEFAULT ''", None),
            "last_event_at": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "status_hash": ("TEXT NOT NULL DEFAULT ''", None),
            "received_at": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "onec_sent_at": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "onec_claimed_at": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "active": ("INTEGER NOT NULL DEFAULT 1", "SMALLINT NOT NULL DEFAULT 1"),
            "next_check_at": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "last_checked_at": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "consecutive_failures": ("INTEGER NOT NULL DEFAULT 0", None),
            "last_error": ("TEXT NOT NULL DEFAULT ''", None),
            "created_at": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
            "updated_at": ("INTEGER NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
        }
        for column, (sqlite_ddl, postgres_ddl) in migrations.items():
            _ensure_column(conn, "shipments", column, sqlite_ddl, postgres_ddl)

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS processed_group_messages (
                chat_id {integer_type} NOT NULL,
                thread_id {integer_type} NOT NULL,
                message_id {integer_type} NOT NULL,
                status TEXT NOT NULL DEFAULT 'done',
                last_error TEXT NOT NULL DEFAULT '',
                created_at {integer_type} NOT NULL,
                updated_at {integer_type} NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, thread_id, message_id)
            )
            """
        )
        _ensure_column(conn, "processed_group_messages", "status", "TEXT NOT NULL DEFAULT 'done'")
        _ensure_column(conn, "processed_group_messages", "last_error", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(
            conn,
            "processed_group_messages",
            "updated_at",
            "INTEGER NOT NULL DEFAULT 0",
            "BIGINT NOT NULL DEFAULT 0",
        )
        conn.execute(
            "UPDATE processed_group_messages SET updated_at = created_at WHERE updated_at = 0"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shipments_due "
            "ON shipments(active, next_check_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shipments_created "
            "ON shipments(created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_processed_updated "
            "ON processed_group_messages(updated_at)"
        )
        # Ukrposhta polling state belongs to the tracking database too.  Create
        # it during the normal migration so health checks, manual status calls
        # and the worker all see the same fully initialized schema.
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS ukr_provider_state (
                provider TEXT PRIMARY KEY,
                period TEXT NOT NULL DEFAULT '',
                used_month INTEGER NOT NULL DEFAULT 0,
                used_total INTEGER NOT NULL DEFAULT 0,
                next_round {integer_type} NOT NULL DEFAULT 0,
                next_call {integer_type} NOT NULL DEFAULT 0,
                pending TEXT NOT NULL DEFAULT '[]',
                failures INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS ukr_tracking_cache (
                ttn TEXT PRIMARY KEY,
                snapshot TEXT NOT NULL,
                pending SMALLINT NOT NULL DEFAULT 1,
                next_retry {integer_type} NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ukr_cache_pending "
            "ON ukr_tracking_cache(pending, next_retry)"
        )
        conn.execute(
            """
            INSERT INTO schema_migrations(component, version, applied_at)
            VALUES ('tracking', 3, ?)
            ON CONFLICT(component) DO UPDATE SET
                version = excluded.version,
                applied_at = excluded.applied_at
            """,
            (int(time.time()),),
        )


def get_shipment(ttn: str) -> dict[str, Any] | None:
    with _LOCK, _transaction() as conn:
        row = conn.execute("SELECT * FROM shipments WHERE ttn = ?", (ttn,)).fetchone()
    return dict(row) if row else None


def upsert_shipment(
    ttn: str,
    account_name: str | None = None,
    profile_name: str | None = None,
    shipment_kind: str | None = None,
    description: str | None = None,
    cost: str | None = None,
    cost_text: str | None = None,
    olx_order_id: str | None = None,
    source_text: str | None = None,
    source_user_id: int | None = None,
    source_user_name: str | None = None,
    source_chat_id: int | None = None,
    source_thread_id: int | None = None,
    source_message_id: int | None = None,
    tracking_chat_id: int | None = None,
    tracking_thread_id: int | None = None,
    tracking_message_id: int | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    values = {
        "ttn": str(ttn),
        "account_name": (account_name or "").strip(),
        "profile_name": (profile_name or "").strip(),
        "shipment_kind": (shipment_kind or "Наложка").strip(),
        "shipment_kind_supplied": 1 if shipment_kind else 0,
        "description": (description or "").strip(),
        "cost": str(cost or "").strip(),
        "cost_text": str(cost_text or "").strip(),
        "olx_order_id": str(olx_order_id or "").strip(),
        "source_text": str(source_text or "").strip(),
        "source_user_id": int(source_user_id or 0),
        "source_user_name": str(source_user_name or "").strip(),
        "source_chat_id": int(source_chat_id or 0),
        "source_thread_id": int(source_thread_id or 0),
        "source_message_id": int(source_message_id or 0),
        "tracking_chat_id": int(tracking_chat_id or 0),
        "tracking_thread_id": int(tracking_thread_id or 0),
        "tracking_message_id": int(tracking_message_id or 0),
        "next_check_at": now + 30,
        "created_at": now,
        "updated_at": now,
    }
    with _LOCK, _transaction() as conn:
        conn.execute(
            """
            INSERT INTO shipments (
                ttn, account_name, profile_name, shipment_kind, description,
                cost, cost_text, olx_order_id, source_text, source_user_id,
                source_user_name, source_chat_id, source_thread_id,
                source_message_id, tracking_chat_id, tracking_thread_id,
                tracking_message_id, next_check_at, created_at, updated_at
            ) VALUES (
                :ttn, :account_name, :profile_name, :shipment_kind, :description,
                :cost, :cost_text, :olx_order_id, :source_text, :source_user_id,
                :source_user_name, :source_chat_id, :source_thread_id,
                :source_message_id, :tracking_chat_id, :tracking_thread_id,
                :tracking_message_id, :next_check_at, :created_at, :updated_at
            )
            ON CONFLICT(ttn) DO UPDATE SET
                account_name = CASE WHEN excluded.account_name <> ''
                    THEN excluded.account_name ELSE shipments.account_name END,
                profile_name = CASE WHEN excluded.profile_name <> ''
                    THEN excluded.profile_name ELSE shipments.profile_name END,
                shipment_kind = CASE WHEN :shipment_kind_supplied = 1
                    THEN excluded.shipment_kind ELSE shipments.shipment_kind END,
                description = CASE WHEN excluded.description <> ''
                    THEN excluded.description ELSE shipments.description END,
                cost = CASE WHEN excluded.cost <> ''
                    THEN excluded.cost ELSE shipments.cost END,
                cost_text = CASE WHEN excluded.cost_text <> ''
                    THEN excluded.cost_text ELSE shipments.cost_text END,
                olx_order_id = CASE WHEN excluded.olx_order_id <> ''
                    THEN excluded.olx_order_id ELSE shipments.olx_order_id END,
                source_text = CASE WHEN excluded.source_text <> ''
                    THEN excluded.source_text ELSE shipments.source_text END,
                source_user_id = CASE WHEN excluded.source_user_id <> 0
                    THEN excluded.source_user_id ELSE shipments.source_user_id END,
                source_user_name = CASE WHEN excluded.source_user_name <> ''
                    THEN excluded.source_user_name ELSE shipments.source_user_name END,
                source_chat_id = CASE WHEN excluded.source_chat_id <> 0
                    THEN excluded.source_chat_id ELSE shipments.source_chat_id END,
                source_thread_id = CASE WHEN excluded.source_thread_id <> 0
                    THEN excluded.source_thread_id ELSE shipments.source_thread_id END,
                source_message_id = CASE WHEN excluded.source_message_id <> 0
                    THEN excluded.source_message_id ELSE shipments.source_message_id END,
                tracking_chat_id = CASE WHEN excluded.tracking_chat_id <> 0
                    THEN excluded.tracking_chat_id ELSE shipments.tracking_chat_id END,
                tracking_thread_id = CASE WHEN excluded.tracking_thread_id <> 0
                    THEN excluded.tracking_thread_id ELSE shipments.tracking_thread_id END,
                tracking_message_id = CASE WHEN excluded.tracking_message_id <> 0
                    THEN excluded.tracking_message_id ELSE shipments.tracking_message_id END,
                updated_at = excluded.updated_at
            """,
            values,
        )
    shipment = get_shipment(str(ttn))
    if shipment is None:
        raise RuntimeError("Shipment upsert failed")
    return shipment


_UPDATABLE_COLUMNS = {
    "account_name", "profile_name", "shipment_kind", "description", "cost",
    "cost_text", "olx_order_id", "source_text", "source_user_id",
    "source_user_name", "source_chat_id", "source_thread_id",
    "source_message_id", "tracking_chat_id", "tracking_thread_id",
    "tracking_message_id", "tracking_claimed_at", "last_status",
    "last_status_code", "last_event_at", "status_hash", "received_at", "onec_sent_at",
    "onec_claimed_at", "active", "next_check_at",
    "last_checked_at", "consecutive_failures", "last_error",
}


def update_shipment(ttn: str, **fields: Any) -> None:
    unknown = set(fields) - _UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(
            "Unknown shipment field(s): " + ", ".join(sorted(unknown))
        )
    updates: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if key in _UPDATABLE_COLUMNS and value is not None:
            updates.append(f"{key} = ?")
            values.append(value)
    updates.append("updated_at = ?")
    values.extend([int(time.time()), ttn])
    with _LOCK, _transaction() as conn:
        conn.execute(
            f"UPDATE shipments SET {', '.join(updates)} WHERE ttn = ?",
            values,
        )


def set_tracking_message_id(
    ttn: str,
    tracking_message_id: int,
    chat_id: int | None = None,
    thread_id: int | None = None,
) -> None:
    update_shipment(
        ttn,
        tracking_message_id=int(tracking_message_id),
        tracking_claimed_at=0,
        tracking_chat_id=int(chat_id) if chat_id is not None else None,
        tracking_thread_id=int(thread_id) if thread_id is not None else None,
    )


def reserve_tracking_message_slot(ttn: str, stale_after_seconds: int = 300) -> tuple[str, int | None]:
    now = int(time.time())
    stale_before = now - stale_after_seconds
    with _LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT tracking_message_id, tracking_claimed_at FROM shipments WHERE ttn = ?",
            (ttn,),
        ).fetchone()
        if not row:
            return "missing", None
        message_id = int(row["tracking_message_id"] or 0)
        if message_id > 0:
            return "existing", message_id
        if message_id == -1 and now - int(row["tracking_claimed_at"] or 0) <= stale_after_seconds:
            return "busy", None
        cursor = conn.execute(
            """
            UPDATE shipments
            SET tracking_message_id = -1, tracking_claimed_at = ?, updated_at = ?
            WHERE ttn = ? AND (
                tracking_message_id = 0
                OR (tracking_message_id = -1 AND tracking_claimed_at < ?)
            )
            """,
            (now, now, ttn, stale_before),
        )
        return ("claimed", None) if cursor.rowcount == 1 else ("busy", None)


def release_tracking_claim(ttn: str) -> None:
    with _LOCK, _transaction() as conn:
        conn.execute(
            """
            UPDATE shipments
            SET tracking_message_id = 0, tracking_claimed_at = 0, updated_at = ?
            WHERE ttn = ? AND tracking_message_id = -1
            """,
            (int(time.time()), ttn),
        )


def reserve_1c_delivery(ttn: str, stale_after_seconds: int = 300) -> bool:
    now = int(time.time())
    stale_before = now - stale_after_seconds
    with _LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT onec_sent_at, onec_claimed_at FROM shipments WHERE ttn = ?", (ttn,)
        ).fetchone()
        if not row:
            return False
        sent_at = int(row["onec_sent_at"] or 0)
        if sent_at > 0:
            return False
        if sent_at == -1 and now - int(row["onec_claimed_at"] or 0) <= stale_after_seconds:
            return False
        cursor = conn.execute(
            """
            UPDATE shipments
            SET onec_sent_at = -1, onec_claimed_at = ?, updated_at = ?
            WHERE ttn = ? AND (
                onec_sent_at = 0
                OR (onec_sent_at = -1 AND onec_claimed_at < ?)
            )
            """,
            (now, now, ttn, stale_before),
        )
        return cursor.rowcount == 1


def finish_1c_delivery(ttn: str, *, success: bool) -> None:
    with _LOCK, _transaction() as conn:
        conn.execute(
            """
            UPDATE shipments
            SET onec_sent_at = ?, onec_claimed_at = 0, updated_at = ?
            WHERE ttn = ? AND onec_sent_at = -1
            """,
            (
                int(time.time()) if success else 0,
                int(time.time()),
                ttn,
            ),
        )


def claim_due_shipments(limit: int = 20, lease_seconds: int = 300, nova_only: bool = False) -> list[dict[str, Any]]:
    now = int(time.time())
    with _LOCK, _transaction() as conn:
        if conn.backend == "postgresql":
            rows = conn.execute(
                """
                WITH due AS (
                    SELECT ttn FROM shipments
                    WHERE active = 1 AND next_check_at <= ? AND (?=0 OR length(ttn)=14)
                    ORDER BY next_check_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT ?
                )
                UPDATE shipments AS shipment
                SET next_check_at = ?, last_checked_at = ?, updated_at = ?
                FROM due
                WHERE shipment.ttn = due.ttn
                RETURNING shipment.*
                """,
                (
                    now,
                    int(nova_only),
                    max(1, int(limit)),
                    now + lease_seconds,
                    now,
                    now,
                ),
            ).fetchall()
        else:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM shipments
                WHERE active = 1 AND next_check_at <= ? AND (?=0 OR length(ttn)=14)
                ORDER BY next_check_at ASC LIMIT ?
                """,
                (now, int(nova_only), max(1, int(limit))),
            ).fetchall()
            if rows:
                conn.executemany(
                    "UPDATE shipments SET next_check_at = ?, last_checked_at = ? WHERE ttn = ?",
                    [(now + lease_seconds, now, row["ttn"]) for row in rows],
                )
    return [dict(row) for row in rows]


def due_shipments(limit: int = 20) -> list[dict[str, Any]]:
    """Compatibility helper; the worker uses ``claim_due_shipments``."""

    now = int(time.time())
    with _LOCK, _transaction() as conn:
        rows = conn.execute(
            """
            SELECT * FROM shipments
            WHERE active = 1 AND next_check_at <= ?
            ORDER BY next_check_at ASC LIMIT ?
            """,
            (now, max(1, int(limit))),
        ).fetchall()
    return [dict(row) for row in rows]


def record_check_failure(ttn: str, error: str) -> int:
    now = int(time.time())
    with _LOCK, _transaction() as conn:
        if conn.backend == "sqlite":
            conn.execute("BEGIN IMMEDIATE")
        row = conn.one(
            "SELECT consecutive_failures FROM shipments WHERE ttn = ?"
            + (" FOR UPDATE" if conn.backend == "postgresql" else ""),
            (ttn,),
        )
        if not row:
            return 0
        failures = int(row["consecutive_failures"] or 0) + 1
        delay = min(6 * 60 * 60, 5 * 60 * (2 ** min(failures - 1, 6)))
        conn.execute(
            """
            UPDATE shipments
            SET consecutive_failures = ?, last_error = ?, next_check_at = ?,
                updated_at = ?
            WHERE ttn = ?
            """,
            (
                failures,
                (error or "unknown error")[:500],
                now + delay,
                now,
                ttn,
            ),
        )
    return failures


def record_check_success(ttn: str) -> None:
    update_shipment(
        ttn,
        consecutive_failures=0,
        last_error="",
        last_checked_at=int(time.time()),
    )


def reactivate_shipment(ttn: str) -> bool:
    with _LOCK, _transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE shipments
            SET active = 1, next_check_at = 0, consecutive_failures = 0,
                last_error = '', updated_at = ?
            WHERE ttn = ?
            """,
            (int(time.time()), ttn),
        )
    return cursor.rowcount == 1


def claim_group_message(
    chat_id: int,
    thread_id: int | None,
    message_id: int,
    stale_after_seconds: int = 300,
) -> bool:
    thread_id = int(thread_id or 0)
    now = int(time.time())
    key = (int(chat_id), thread_id, int(message_id))
    with _LOCK, _transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO processed_group_messages
                (chat_id, thread_id, message_id, status, created_at, updated_at)
            VALUES (?, ?, ?, 'processing', ?, ?)
            ON CONFLICT(chat_id, thread_id, message_id) DO NOTHING
            """,
            (*key, now, now),
        )
        if cursor.rowcount == 1:
            return True
        row = conn.execute(
            """
            SELECT status, updated_at FROM processed_group_messages
            WHERE chat_id = ? AND thread_id = ? AND message_id = ?
            """,
            key,
        ).fetchone()
        if not row or row["status"] == "done":
            return False
        if now - int(row["updated_at"] or 0) <= stale_after_seconds:
            return False
        cursor = conn.execute(
            """
            UPDATE processed_group_messages
            SET status = 'processing', last_error = '', updated_at = ?
            WHERE chat_id = ? AND thread_id = ? AND message_id = ?
              AND status <> 'done'
              AND updated_at < ?
            """,
            (now, *key, now - stale_after_seconds),
        )
        return cursor.rowcount == 1


def complete_group_message(chat_id: int, thread_id: int | None, message_id: int) -> None:
    with _LOCK, _transaction() as conn:
        conn.execute(
            """
            UPDATE processed_group_messages
            SET status = 'done', last_error = '', updated_at = ?
            WHERE chat_id = ? AND thread_id = ? AND message_id = ?
            """,
            (int(time.time()), int(chat_id), int(thread_id or 0), int(message_id)),
        )


def release_group_message(
    chat_id: int,
    thread_id: int | None,
    message_id: int,
    error: str,
) -> None:
    with _LOCK, _transaction() as conn:
        conn.execute(
            """
            UPDATE processed_group_messages
            SET status = 'failed', last_error = ?, updated_at = ?
            WHERE chat_id = ? AND thread_id = ? AND message_id = ?
              AND status <> 'done'
            """,
            (
                (error or "unknown error")[:500],
                int(time.time()),
                int(chat_id),
                int(thread_id or 0),
                int(message_id),
            ),
        )


def seen_group_message(chat_id: int, thread_id: int | None, message_id: int) -> bool:
    with _LOCK, _transaction() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM processed_group_messages
            WHERE chat_id = ? AND thread_id = ? AND message_id = ? AND status = 'done'
            """,
            (int(chat_id), int(thread_id or 0), int(message_id)),
        ).fetchone()
    return row is not None


def mark_group_message_seen(chat_id: int, thread_id: int | None, message_id: int) -> None:
    now = int(time.time())
    with _LOCK, _transaction() as conn:
        conn.execute(
            """
            INSERT INTO processed_group_messages
                (chat_id, thread_id, message_id, status, created_at, updated_at)
            VALUES (?, ?, ?, 'done', ?, ?)
            ON CONFLICT(chat_id, thread_id, message_id) DO UPDATE SET
                status = 'done', last_error = '', updated_at = excluded.updated_at
            """,
            (int(chat_id), int(thread_id or 0), int(message_id), now, now),
        )


def cleanup_processed_messages(retention_days: int = 45) -> int:
    cutoff = int(time.time()) - max(1, retention_days) * 86400
    with _LOCK, _transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM processed_group_messages WHERE status = 'done' AND updated_at < ?",
            (cutoff,),
        )
    return cursor.rowcount


def _csv_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def export_shipments_csv(days: int = 365) -> bytes:
    cutoff = int(time.time()) - max(1, min(int(days), 3650)) * 86400
    columns = (
        "ttn", "olx_order_id", "account_name", "shipment_kind", "description",
        "cost", "cost_text", "last_status", "last_status_code", "profile_name",
        "source_user_id", "source_user_name", "received_at", "onec_sent_at",
        "active", "created_at", "updated_at", "last_error",
    )
    with _LOCK, _transaction() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM shipments "
            "WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff,),
        ).fetchall()
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_safe(row[column]) for column in columns])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def database_health() -> dict[str, int]:
    with _LOCK, _transaction() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active,
                   SUM(CASE WHEN last_error <> '' THEN 1 ELSE 0 END) AS errors
            FROM shipments
            """
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "active": int(row["active"] or 0),
        "errors": int(row["errors"] or 0),
    }

