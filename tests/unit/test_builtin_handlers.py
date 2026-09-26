"""Built-in diagnostic handler tests (config_read / project_list)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from drawbridge.config.models import ConfigFileAlias
from drawbridge.errors import DrawbridgeError
from drawbridge.runner.builtin import (
    BuiltinContext,
    handle_config_read,
    handle_project_list,
)

ROOT_NAME = "current"


def make_ctx(root: Path, config_files: dict[str, ConfigFileAlias]) -> BuiltinContext:
    return BuiltinContext(
        app_id="demo",
        environment="staging",
        diagnostics_root=str(root),
        config_files=config_files,
    )


@pytest.fixture()
def diag_root(tmp_path: Path) -> Path:
    root = tmp_path / ROOT_NAME
    (root / "config").mkdir(parents=True)
    payload = {
        "name": "demo",
        "listen_port": 8080,
        "log_level": "info",
        "admin_token": "super-secret",
    }
    (root / "config" / "app.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    # bytes: deterministic line endings across platforms (raw alias asserts
    # exact on-disk content)
    (root / "README.md").write_bytes(b"hello\n")
    return root


class TestConfigRead:
    def test_field_whitelist_with_sensitive_masked(self, diag_root: Path) -> None:
        ctx = make_ctx(
            diag_root,
            {
                "app_config": ConfigFileAlias.model_validate(
                    {
                        "path": "config/app.json",
                        "fields": ["name", "listen_port", "admin_token"],
                        "sensitive_fields": ["admin_token"],
                    }
                )
            },
        )
        result = handle_config_read(ctx, "app_config")
        assert result["fields"]["name"] == "demo"
        assert result["fields"]["listen_port"] == 8080
        assert "admin_token" not in result["fields"]
        assert result["truncated"] is False

    def test_raw_alias_allowed(self, diag_root: Path) -> None:
        ctx = make_ctx(
            diag_root,
            {
                "readme": ConfigFileAlias.model_validate(
                    {"path": "README.md", "raw": True}
                )
            },
        )
        result = handle_config_read(ctx, "readme")
        assert result["content"] == "hello\n"

    def test_unregistered_alias_rejected(self, diag_root: Path) -> None:
        ctx = make_ctx(diag_root, {})
        with pytest.raises(DrawbridgeError, match="not registered"):
            handle_config_read(ctx, "anything")

    def test_missing_file_rejected(self, diag_root: Path) -> None:
        ctx = make_ctx(
            diag_root,
            {
                "ghost": ConfigFileAlias.model_validate(
                    {"path": "config/ghost.json", "fields": ["a"]}
                )
            },
        )
        with pytest.raises(FileNotFoundError):
            handle_config_read(ctx, "ghost")

    @pytest.mark.skipif(os.name == "nt", reason="needs symlink privilege")
    def test_symlink_component_rejected(self, diag_root: Path, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.txt"
        secrets.write_text("token=1", encoding="utf-8")
        link = diag_root / "config" / "leak.json"
        os.symlink(secrets, link)
        ctx = make_ctx(
            diag_root,
            {
                "leak": ConfigFileAlias.model_validate(
                    {"path": "config/leak.json", "raw": True}
                )
            },
        )
        with pytest.raises(PermissionError, match="symbol link"):
            handle_config_read(ctx, "leak")

    def test_traversal_rejected(self, diag_root: Path) -> None:
        ctx = make_ctx(
            diag_root,
            {
                "escape": ConfigFileAlias.model_validate(
                    {"path": "../secrets.txt", "raw": True}
                )
            },
        )
        with pytest.raises((ValueError, FileNotFoundError, PermissionError)):
            handle_config_read(ctx, "escape")

    def test_no_diagnostics_root(self) -> None:
        ctx = BuiltinContext(
            app_id="demo",
            environment="staging",
            diagnostics_root=None,
            config_files={},
        )
        with pytest.raises(DrawbridgeError, match="NO_BASELINE|diagnostics root"):
            handle_config_read(ctx, "app_config")

    def test_oversized_file_truncated(self, diag_root: Path) -> None:
        big = diag_root / "README.md"
        big.write_text("x" * (200 * 1024), encoding="utf-8")
        ctx = make_ctx(
            diag_root,
            {"readme": ConfigFileAlias.model_validate({"path": "README.md", "raw": True})},
        )
        result = handle_config_read(ctx, "readme")
        assert result["truncated"] is True
        assert len(result["content"]) <= 64 * 1024


class TestProjectList:
    def test_lists_single_directory(self, diag_root: Path) -> None:
        result = handle_project_list(make_ctx(diag_root, {}), ".")
        names = {e["name"] for e in result["entries"]}
        assert {"config", "README.md"} <= names
        kinds = {e["name"]: e["type"] for e in result["entries"]}
        assert kinds["config"] == "dir"
        assert kinds["README.md"] == "file"

    def test_subdirectory_listing(self, diag_root: Path) -> None:
        result = handle_project_list(make_ctx(diag_root, {}), "config")
        assert [e["name"] for e in result["entries"]] == ["app.json"]

    def test_traversal_rejected(self, diag_root: Path) -> None:
        with pytest.raises(DrawbridgeError):
            handle_project_list(make_ctx(diag_root, {}), "../..")

    @pytest.mark.skipif(os.name == "nt", reason="needs symlink privilege")
    def test_symlinked_entry_marked_not_followed(self, diag_root: Path) -> None:
        target = diag_root / "README.md"
        link = diag_root / "readme-link"
        os.symlink(target, link)
        result = handle_project_list(make_ctx(diag_root, {}), ".")
        kinds = {e["name"]: e["type"] for e in result["entries"]}
        assert kinds["readme-link"] == "symlink"

    def test_pagination_cursor(self, diag_root: Path) -> None:
        for i in range(250):
            (diag_root / f"f{i:03d}.txt").write_text(str(i), encoding="utf-8")
        ctx = make_ctx(diag_root, {})
        page1 = handle_project_list(ctx, ".")
        assert page1["truncated"] is True
        assert page1["next_cursor"] is not None
        assert len(page1["entries"]) == 200
        page2 = handle_project_list(ctx, ".", cursor=page1["next_cursor"])
        assert len(page2["entries"]) >= 50
        assert page2["entries"][0]["name"] != page1["entries"][0]["name"]

    def test_limit_drives_page_size_and_traversal(self, diag_root: Path) -> None:
        """D8: limit paginates >200-entry directories; cursor traversal sees
        every entry exactly once in sorted order."""
        for i in range(230):
            (diag_root / f"g{i:03d}.txt").write_text(str(i), encoding="utf-8")
        ctx = make_ctx(diag_root, {})
        page1 = handle_project_list(ctx, ".", limit=100)
        assert len(page1["entries"]) == 100
        assert page1["truncated"] is True
        page2 = handle_project_list(ctx, ".", cursor=page1["next_cursor"], limit=100)
        page3 = handle_project_list(ctx, ".", cursor=page2["next_cursor"], limit=100)
        names = [e["name"] for p in (page1, page2, page3) for e in p["entries"]]
        assert len(names) == len(set(names))  # no overlap across pages
        assert names == sorted(names)  # sorted globally across pages
        assert len(names) >= 230

    def test_invalid_cursor_strictly_rejected(self, diag_root: Path) -> None:
        """D8: a charset-valid but non-numeric cursor must fail, not silently
        restart paging from the top."""
        ctx = make_ctx(diag_root, {})
        with pytest.raises(DrawbridgeError, match="cursor"):
            handle_project_list(ctx, ".", cursor="abc")
        with pytest.raises(DrawbridgeError, match="cursor"):
            handle_project_list(ctx, ".", cursor="0;rm")

    def test_limit_bounds_enforced(self, diag_root: Path) -> None:
        ctx = make_ctx(diag_root, {})
        for bad in (0, 201, -1):
            with pytest.raises(DrawbridgeError, match="limit"):
                handle_project_list(ctx, ".", limit=bad)

    @pytest.mark.skipif(os.name == "nt", reason="mkfifo is POSIX-only")
    def test_special_file_type_reported_other(self, diag_root: Path) -> None:
        fifo = diag_root / "pipe"
        os.mkfifo(fifo)
        result = handle_project_list(make_ctx(diag_root, {}), ".")
        kinds = {e["name"]: e["type"] for e in result["entries"]}
        assert kinds["pipe"] == "other"


class TestLoadavgParsing:
    def test_parse_loadavg_uses_kernel_values_as_is(self) -> None:
        from drawbridge.runner.builtin import parse_loadavg

        # /proc/loadavg values are already load-normalized (e.g. 0.52)
        assert parse_loadavg("0.52 0.58 0.59 1/487 12345") == (0.52, 0.58, 0.59)

    def test_parse_loadavg_rejects_garbage(self) -> None:
        from drawbridge.runner.builtin import parse_loadavg

        with pytest.raises(ValueError):
            parse_loadavg("garbage")


class TestBoundedScan:
    def test_scan_stops_at_entry_cap(self, diag_root: Path, monkeypatch) -> None:
        import drawbridge.runner.builtin as builtin

        monkeypatch.setattr(builtin, "PROJECT_LIST_MAX_ENTRIES", 5)
        bulk = diag_root / "bulk"
        bulk.mkdir()
        for i in range(20):
            (bulk / f"f{i:02d}").write_text("x", encoding="utf-8")
        result = handle_project_list(make_ctx(diag_root, {}), "bulk")
        # the directory iteration stopped at cap+1: never all 20 entries
        assert len(result["entries"]) == 6
        assert result["truncated"] is True


class TestDiagnosticsRootNotReady:
    """Plan D14: a missing diagnostics root returns structured NO_BASELINE
    with guidance instead of leaking INTERNAL(FileNotFoundError)."""

    @staticmethod
    def _ctx(tmp_path: Path) -> BuiltinContext:
        return BuiltinContext(
            app_id="demo",
            environment="staging",
            diagnostics_root=str(tmp_path / "never-created"),
            config_files={},
        )

    def test_config_read_missing_root(self, tmp_path: Path) -> None:
        with pytest.raises(DrawbridgeError) as exc:
            handle_config_read(self._ctx(tmp_path), "app_config")
        assert exc.value.code == "NO_BASELINE"
        assert "diagnostics root" in str(exc.value)

    def test_project_list_missing_root(self, tmp_path: Path) -> None:
        with pytest.raises(DrawbridgeError) as exc:
            handle_project_list(self._ctx(tmp_path), ".")
        assert exc.value.code == "NO_BASELINE"
        assert "diagnostics root" in str(exc.value)
