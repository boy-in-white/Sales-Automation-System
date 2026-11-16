#!/usr/bin/env python3
"""Copy the legacy SQLite database into an initialized PostgreSQL database."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import business_db  # noqa: E402
import tracking_db  # noqa: E402
from config import DATABASE_PATH, DATABASE_URL  # noqa: E402
from db_runtime import close_pools, database_backend, redacted_database_url  # noqa: E402


TABLES = (
    ("business", "products", ("id",)),
    ("business", "product_aliases", ("alias_key",)),
    ("business", "order_submissions", ("id",)),
    ("business", "orders", ("id",)),
    ("business", "order_events", ("id",)),
    ("business", "order_notifications", ("order_id", "notification_type")),
    ("analytics", "monthly_turnover", ("month",)),
    ("bot", "shipments", ("ttn",)),
    ("bot", "processed_group_messages", ("chat_id", "thread_id", "message_id")),
)


def _source_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _source_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]


def _target_columns(connection: Any, schema: str, table: str) -> list[str]:
    rows = connection.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _target_count(connection: Any, schema: str, table: str) -> int:
    from psycopg import sql

    row = connection.execute(
        sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
            sql.Identifier(schema),
            sql.Identifier(table),
        )
    ).fetchone()
    return int(row[0])


def _copy_table(
    source: sqlite3.Connection,
    target: Any,
    schema: str,
    table: str,
    conflict_columns: tuple[str, ...],
) -> int:
    from psycopg import sql

    source_columns = _source_columns(source, table)
    target_columns = set(_target_columns(target, schema, table))
    columns = [column for column in source_columns if column in target_columns]
    if not columns:
        return 0
    rows = source.execute(
        f"SELECT {', '.join(columns)} FROM {table}"
    ).fetchall()
    if not rows:
        return 0

    statement = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) ON CONFLICT ({}) DO NOTHING").format(
        sql.Identifier(schema),
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        sql.SQL(", ").join(map(sql.Identifier, conflict_columns)),
    )
    with target.cursor() as cursor:
        cursor.executemany(
            statement,
            [tuple(row[column] for column in columns) for row in rows],
        )
    return len(rows)


def migrate(source_path: Path, merge: bool = False) -> None:
    if database_backend(DATABASE_URL) != "postgresql":
        raise RuntimeError("Set DATABASE_URL to PostgreSQL before running this tool")
    if not source_path.is_file():
        raise RuntimeError(f"SQLite source does not exist: {source_path}")

    tracking_db.init_db()
    business_db.init_business_db()
    close_pools()

    import psycopg

    with sqlite3.connect(source_path) as source:
        source.row_factory = sqlite3.Row
        available = _source_tables(source)
        with psycopg.connect(DATABASE_URL) as target:
            if not merge:
                occupied = [
                    f"{schema}.{table}"
                    for schema, table, _keys in TABLES
                    if table in available and _target_count(target, schema, table) > 0
                ]
                if occupied:
                    raise RuntimeError(
                        "Target is not empty: " + ", ".join(occupied)
                        + ". Use --merge only if this is intentional."
                    )

            total_considered = 0
            total_inserted = 0
            for schema, table, conflict_columns in TABLES:
                if table not in available:
                    print(f"skip {table}: not present in SQLite")
                    continue
                before = _target_count(target, schema, table)
                considered = _copy_table(
                    source,
                    target,
                    schema,
                    table,
                    conflict_columns,
                )
                after = _target_count(target, schema, table)
                inserted = after - before
                if inserted < 0 or inserted > considered:
                    raise RuntimeError(
                        f"Unexpected row count while copying {schema}.{table}"
                    )
                if not merge and after != considered:
                    raise RuntimeError(
                        f"Verification failed for {schema}.{table}: "
                        f"SQLite={considered}, PostgreSQL={after}"
                    )
                total_considered += considered
                total_inserted += inserted
                skipped = considered - inserted
                print(
                    f"{schema}.{table}: source={considered}, "
                    f"inserted={inserted}, skipped={skipped}"
                )
            target.commit()
    print(
        f"Migration complete: {total_considered} row(s) checked, "
        f"{total_inserted} inserted"
    )
    print(f"Target: {redacted_database_url(DATABASE_URL)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate old OLX bot data from SQLite to PostgreSQL",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(DATABASE_PATH),
        help="path to the old tracking.sqlite3 file",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="keep existing PostgreSQL rows and skip conflicting SQLite rows",
    )
    arguments = parser.parse_args()
    migrate(arguments.source.expanduser().resolve(), arguments.merge)


if __name__ == "__main__":
    main()

