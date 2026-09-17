"""Two-layer locks (tech design §10).

* in-process: one :class:`asyncio.Lock` per target — serializes concurrent
  asyncio jobs inside a single Runner;
* cross-process: a non-blocking OS file lock (flock / msvcrt) — protects
  against a second Runner instance touching the same target.

Lock files are never deleted while in use, and a held file lock never
blocks the event loop: acquisition is try-lock only (LOCK_NB / LK_NBLCK).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path


class FileLock:
    """Named non-blocking exclusive lock backed by a lock file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o660)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                with contextlib.suppress(OSError):
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None


class TargetLocks:
    """asyncio per-target mutex plus optional cross-process file lock."""

    def __init__(self, lock_dir: str | Path | None = None) -> None:
        self._lock_dir = Path(lock_dir) if lock_dir else None
        self._async_locks: dict[str, asyncio.Lock] = {}
        self._file_locks: dict[str, FileLock] = {}

    def _key(self, app: str, environment: str) -> str:
        return f"{app}.{environment}"

    def async_lock(self, app: str, environment: str) -> asyncio.Lock:
        key = self._key(app, environment)
        if key not in self._async_locks:
            self._async_locks[key] = asyncio.Lock()
        return self._async_locks[key]

    def acquire_file_lock(self, app: str, environment: str) -> bool:
        """Non-blocking cross-process lock; False means someone else holds it."""
        if self._lock_dir is None:
            return True
        key = self._key(app, environment)
        if key not in self._file_locks:
            self._file_locks[key] = FileLock(self._lock_dir / f"{key}.lock")
        return self._file_locks[key].acquire()

    def release_file_lock(self, app: str, environment: str) -> None:
        key = self._key(app, environment)
        lock = self._file_locks.get(key)
        if lock is not None:
            lock.release()
