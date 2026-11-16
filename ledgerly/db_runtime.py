"""Shared SQLite/PostgreSQL runtime with lazy PostgreSQL connection pools.

PostgreSQL is the production backend.  SQLite remains available for automated
tests and for importing an existing installation.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import urlsplit, urlunsplit

from config import (
    BASE_DIR,
    DATABASE_CONNECT_TIMEOUT_SECONDS,
    DATABASE_POOL_SIZE,
)


_POOLS: dict[tuple[str, tuple[str, ...]], Any] = {}
_POOL_LOCK = threading.RLock()
_NAMED_PARAM_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def database_backend(url: str) -> str:
    if url.startswith(("postgresql://", "postgres://")):
        return "postgresql"
    if url.startswith("sqlite:///"):
        return "sqlite"
    raise RuntimeError("DATABASE_URL must start with postgresql:// or sqlite:///")


def sqlite_path(url: str, default: Path | None = None) -> Path:
    if not url.startswith("sqlite:///"):
        if default is None:
            raise RuntimeError("This database URL is not SQLite")
        return Path(default).resolve()
    raw = url[len("sqlite:///") :]
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else BASE_DIR / path).resolve()


def redacted_database_url(url: str) -> str:
    if database_backend(url) == "sqlite":
        return url
    parsed = urlsplit(url)
    username = parsed.username or ""
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    auth = f"{username}:***@" if username else ""
    return urlunsplit((parsed.scheme, f"{auth}{host}{port}", parsed.path, "", ""))


def _validate_schemas(schemas: Sequence[str]) -> tuple[str, ...]:
    result = tuple(str(schema) for schema in schemas)
    if not result or any(not _SCHEMA_RE.fullmatch(schema) for schema in result):
        raise RuntimeError("Invalid PostgreSQL schema name")
    return result


def _postgres_pool(url: str, schemas: Sequence[str]) -> Any:
    schema_tuple = _validate_schemas(schemas)
    key = (url, schema_tuple)
    with _POOL_LOCK:
        existing = _POOLS.get(key)
        if existing is not None:
            return existing
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL support requires psycopg[binary] and psycopg-pool"
            ) from exc

        search_path = ", ".join(schema_tuple)

        def configure(connection: Any) -> None:
            connection.execute(f"SET search_path TO {search_path}")
            connection.commit()

        pool = ConnectionPool(
            conninfo=url,
            min_size=1,
            max_size=DATABASE_POOL_SIZE,
            open=False,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": DATABASE_CONNECT_TIMEOUT_SECONDS,
                "application_name": "olx_1c_bot",
            },
            configure=configure,
            name="olx-bot-" + "-".join(schema_tuple),
        )
        pool.open(wait=True, timeout=DATABASE_CONNECT_TIMEOUT_SECONDS)
        _POOLS[key] = pool
        return pool


def bootstrap_postgres_schemas(url: str, schemas: Sequence[str]) -> None:
    if database_backend(url) != "postgresql":
        return
    schema_tuple = _validate_schemas(schemas)
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise RuntimeError("Install psycopg[binary] to use PostgreSQL") from exc
    with psycopg.connect(
        url,
        connect_timeout=DATABASE_CONNECT_TIMEOUT_SECONDS,
        application_name="olx_1c_bot_bootstrap",
    ) as connection:
        for schema in schema_tuple:
            connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
            )


class Session:
    def __init__(self, connection: Any, backend: str):
        self.connection = connection
        self.backend = backend

    def _sql(self, statement: str) -> str:
        if self.backend == "sqlite":
            return statement
        if "?" in statement:
            statement = statement.replace("?", "%s")
        return _NAMED_PARAM_RE.sub(r"%(\1)s", statement)

    def execute(self, statement: str, params: Any = ()) -> Any:
        return self.connection.execute(self._sql(statement), params)

    def executemany(self, statement: str, params: Sequence[Any]) -> Any:
        return self.connection.executemany(self._sql(statement), params)

    def one(self, statement: str, params: Any = ()) -> dict[str, Any] | None:
        row = self.execute(statement, params).fetchone()
        return dict(row) if row else None

    def all(self, statement: str, params: Any = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.execute(statement, params).fetchall()]


@contextmanager
def transaction(
    url: str,
    *,
    schemas: Sequence[str],
    sqlite_default_path: Path | None = None,
) -> Iterator[Session]:
    backend = database_backend(url)
    if backend == "postgresql":
        pool = _postgres_pool(url, schemas)
        with pool.connection() as connection:
            try:
                yield Session(connection, backend)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return

    path = sqlite_path(url, sqlite_default_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        yield Session(connection, backend)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def close_pools() -> None:
    with _POOL_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        pool.close()

