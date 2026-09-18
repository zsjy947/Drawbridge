"""Server-side log pagination over a bounded snapshot (MVP spec §4).

``compose logs`` output is fetched once as a bounded snapshot; ``query``
is a literal substring filter applied server-side; pages are addressed by
an opaque cursor that is bound to that snapshot, so pagination never
re-scans (and never misses lines added between pages).  Snapshots expire
after 10 minutes and the cache holds at most a fixed number of them —
an expired or evicted cursor is rejected, the client simply re-fetches.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

SNAPSHOT_TTL_SECONDS = 600.0
MAX_SNAPSHOTS = 32


@dataclass
class LogSnapshot:
    lines: list[str] = field(default_factory=list)
    created_at: float = 0.0


class UnknownCursorError(Exception):
    """The cursor is malformed, expired, or its snapshot was evicted."""


class LogCursorCache:
    def __init__(
        self,
        *,
        ttl_seconds: float = SNAPSHOT_TTL_SECONDS,
        max_snapshots: int = MAX_SNAPSHOTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_snapshots = max_snapshots
        self._clock = clock
        self._snapshots: dict[str, LogSnapshot] = {}

    def create(self, lines: list[str]) -> str:
        self._evict()
        snapshot_id = uuid.uuid4().hex
        self._snapshots[snapshot_id] = LogSnapshot(
            lines=list(lines), created_at=self._clock()
        )
        return snapshot_id

    def resolve(self, cursor: str) -> tuple[str, LogSnapshot, int]:
        """Parse ``{snapshot_id}:{offset}`` and return (id, snapshot, offset)."""
        snapshot_id, sep, offset_raw = cursor.rpartition(":")
        if not sep or not snapshot_id or not offset_raw.isdigit():
            raise UnknownCursorError("malformed cursor")
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None or (self._clock() - snapshot.created_at) > self._ttl:
            self._snapshots.pop(snapshot_id, None)
            raise UnknownCursorError("cursor expired; re-fetch the log snapshot")
        offset = int(offset_raw)
        if offset < 0 or offset > len(snapshot.lines):
            raise UnknownCursorError("cursor offset out of range")
        return snapshot_id, snapshot, offset

    def _evict(self) -> None:
        now = self._clock()
        expired = [
            key
            for key, snap in self._snapshots.items()
            if (now - snap.created_at) > self._ttl
        ]
        for key in expired:
            del self._snapshots[key]
        while len(self._snapshots) >= self._max_snapshots:
            oldest = min(self._snapshots, key=lambda k: self._snapshots[k].created_at)
            del self._snapshots[oldest]


def paginate_lines(
    lines: list[str],
    *,
    query: str = "",
    offset: int = 0,
    limit: int = 100,
    max_bytes: int = 262144,
) -> dict[str, object]:
    """Filter by literal substring and cut one page under byte/line budgets.

    Returns a JSON-safe page description; ``next_offset`` is None when the
    matched stream is exhausted.
    """
    matched = [line for line in lines if query in line] if query else list(lines)
    page: list[str] = []
    page_bytes = 0
    page_truncated = False
    index = offset
    while index < len(matched):
        line = matched[index]
        cost = len(line.encode("utf-8", errors="replace")) + 1
        if page and page_bytes + cost > max_bytes:
            page_truncated = True
            break
        if len(page) >= limit:
            break
        page.append(line)
        page_bytes += cost
        index += 1
    more = index < len(matched)
    return {
        "lines": page,
        "matched_total": len(matched),
        "snapshot_total": len(lines),
        "truncated": page_truncated or more,
        "next_offset": index if more else None,
    }
