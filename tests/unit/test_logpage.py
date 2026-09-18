"""Log snapshot pagination and cursor cache tests (MVP spec §4)."""

from __future__ import annotations

import pytest

from drawbridge.runner.logpage import (
    LogCursorCache,
    UnknownCursorError,
    paginate_lines,
)


class TestPaginateLines:
    def test_first_page_and_cursor_offset(self) -> None:
        lines = [f"line-{i}" for i in range(10)]
        page = paginate_lines(lines, limit=4)
        assert page["lines"] == ["line-0", "line-1", "line-2", "line-3"]
        assert page["next_offset"] == 4
        assert page["truncated"] is True
        assert page["matched_total"] == 10

        page2 = paginate_lines(lines, offset=4, limit=4)
        assert page2["lines"] == ["line-4", "line-5", "line-6", "line-7"]
        page3 = paginate_lines(lines, offset=8, limit=4)
        assert page3["lines"] == ["line-8", "line-9"]
        assert page3["next_offset"] is None
        assert page3["truncated"] is False

    def test_literal_query_filter(self) -> None:
        lines = ["INFO ok", "ERROR bad", "INFO fine", "error lower"]
        page = paginate_lines(lines, query="ERROR", limit=10)
        assert page["lines"] == ["ERROR bad"]
        assert page["matched_total"] == 1
        assert page["snapshot_total"] == 4
        assert page["next_offset"] is None

    def test_empty_query_matches_everything(self) -> None:
        page = paginate_lines(["a", "b"], query="", limit=10)
        assert page["matched_total"] == 2

    def test_byte_budget_truncates(self) -> None:
        lines = ["x" * 100 for _ in range(10)]
        page = paginate_lines(lines, limit=10, max_bytes=250)
        assert len(page["lines"]) == 2  # 2 full lines + separators fit, 3rd does not
        assert page["truncated"] is True
        assert page["next_offset"] == 2

    def test_single_oversized_line_is_returned(self) -> None:
        page = paginate_lines(["x" * 100], limit=10, max_bytes=10)
        assert page["lines"] == ["x" * 100]  # never return an empty page


class TestLogCursorCache:
    def test_roundtrip_and_pagination(self) -> None:
        cache = LogCursorCache()
        snapshot_id = cache.create(["a", "b", "c"])
        sid, snapshot, offset = cache.resolve(f"{snapshot_id}:2")
        assert sid == snapshot_id
        assert offset == 2
        assert snapshot.lines == ["a", "b", "c"]

    def test_expired_cursor_rejected(self) -> None:
        now = {"t": 0.0}
        cache = LogCursorCache(ttl_seconds=10.0, clock=lambda: now["t"])
        snapshot_id = cache.create(["a"])
        now["t"] = 11.0
        with pytest.raises(UnknownCursorError):
            cache.resolve(f"{snapshot_id}:0")

    def test_malformed_cursor_rejected(self) -> None:
        cache = LogCursorCache()
        with pytest.raises(UnknownCursorError):
            cache.resolve("no-colon-here")
        with pytest.raises(UnknownCursorError):
            cache.resolve("abc:not-a-number")
        with pytest.raises(UnknownCursorError):
            cache.resolve("nonexistent00:0")

    def test_offset_out_of_range_rejected(self) -> None:
        cache = LogCursorCache()
        snapshot_id = cache.create(["a"])
        with pytest.raises(UnknownCursorError):
            cache.resolve(f"{snapshot_id}:5")

    def test_eviction_keeps_bounded_memory(self) -> None:
        now = {"t": 0.0}
        cache = LogCursorCache(max_snapshots=3, clock=lambda: now["t"])
        first = cache.create(["first"])
        for _ in range(3):
            cache.create(["x"])
        with pytest.raises(UnknownCursorError):
            cache.resolve(f"{first}:0")
