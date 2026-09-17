"""aiosqlite connection management with the mandated PRAGMA profile.

WAL + foreign_keys + busy_timeout(5s) + synchronous=FULL (tech design §10).
Connections are per-process: Gateway and Runner each own their connection;
transactions stay short and never span subprocess execution.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite

from drawbridge.state.schema import SCHEMA_STATEMENTS, SCHEMA_VERSION

_BUSY_TIMEOUT_MS = 5000


class Database:
    """Thin async wrapper around one SQLite connection."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        await conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = FULL")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    async def initialize(self) -> None:
        """Create the schema if absent and verify the version (fail closed)."""
        conn = self.conn
        async with self._write_lock:
            for statement in SCHEMA_STATEMENTS:
                await conn.execute(statement)
            await conn.execute(
                "INSERT INTO control_state(key, value, updated_at) "
                "VALUES('schema_version', ?, strftime('%s','now') * 1.0) "
                "ON CONFLICT(key) DO NOTHING",
                (str(SCHEMA_VERSION),),
            )
            await conn.execute(
                "INSERT INTO control_state(key, value, updated_at) "
                "VALUES('maintenance', 'false', strftime('%s','now') * 1.0) "
                "ON CONFLICT(key) DO NOTHING"
            )
            await conn.commit()
        row = await self.get_control("schema_version")
        if row is None or int(row) != SCHEMA_VERSION:
            raise RuntimeError(
                f"state database schema version mismatch: expected {SCHEMA_VERSION}, "
                f"found {row!r}; run the migration/backup procedure before starting"
            )

    # -- control_state ------------------------------------------------------

    async def get_control(self, key: str) -> str | None:
        async with self.conn.execute(
            "SELECT value FROM control_state WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["value"] if row is not None else None

    async def set_control(self, key: str, value: str) -> None:
        async with self._write_lock:
            await self.conn.execute(
                "INSERT INTO control_state(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, _now()),
            )
            await self.conn.commit()

    # -- generic helpers ----------------------------------------------------

    def write_lock(self) -> asyncio.Lock:
        """Serialize multi-statement write transactions within this process."""
        return self._write_lock


def _now() -> float:
    import time

    return time.time()
