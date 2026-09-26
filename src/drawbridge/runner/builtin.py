"""Built-in read handlers (MVP spec §4).

Pure-Python implementations — no cat, no ls, no find:

* ``config_read``   — registered alias only, dirfd-style no-follow opening,
  64 KiB cap, JSON/TOML field whitelists or admin-declared raw files;
* ``project_list``  — single registered directory, non-recursive, at most
  1000 entries with a truncation flag;
* ``host_metrics``  — aggregated CPU/memory/disk from /proc and os on
  Linux; UNSUPPORTED elsewhere (MVP: 主机聚合指标).
"""

from __future__ import annotations

import json
import os
import stat as stat_module
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from drawbridge.config.models import ConfigFileAlias
from drawbridge.errors import DrawbridgeError, ErrorCode
from drawbridge.fsops import open_file_nofollow

CONFIG_READ_MAX_BYTES = 64 * 1024
PROJECT_LIST_MAX_ENTRIES = 1000


def parse_loadavg(text: str) -> tuple[float, float, float]:
    """Parse /proc/loadavg — values are already load-normalized."""
    fields = text.split()
    if len(fields) < 3:
        raise ValueError(f"unparseable /proc/loadavg: {text[:40]!r}")
    loads = tuple(float(field) for field in fields[:3])
    return loads[0], loads[1], loads[2]


class UnsupportedError(DrawbridgeError):
    code = ErrorCode.UNSUPPORTED


@dataclass(frozen=True)
class BuiltinContext:
    """Everything a read handler may see — admin config, nothing else."""

    app_id: str
    environment: str
    diagnostics_root: str | None
    config_files: dict[str, ConfigFileAlias]


def _load_structured(raw: str, fmt: str) -> dict[str, Any]:
    if fmt == "json":
        try:
            loaded: dict[str, Any] = json.loads(raw)
            return loaded
        except json.JSONDecodeError as exc:
            raise DrawbridgeError(
                f"config file is not valid JSON: {exc}", code=ErrorCode.SOURCE_INVALID
            ) from exc
    if fmt == "toml":
        import tomllib

        try:
            parsed: dict[str, Any] = tomllib.loads(raw)
            return parsed
        except Exception as exc:  # tomllib.TOMLDecodeError
            raise DrawbridgeError(
                f"config file is not valid TOML: {exc}", code=ErrorCode.SOURCE_INVALID
            ) from exc
    raise DrawbridgeError(f"unknown structured format {fmt!r}", code=ErrorCode.INTERNAL)


def _require_diagnostics_root(ctx: BuiltinContext) -> str:
    """Structured gate for an unavailable diagnostics root (plan D14).

    A missing root previously leaked INTERNAL(FileNotFoundError) — the
    init-config skeleton used to point at a directory that is never created,
    so every diagnostic failed confusingly before AND after the first
    deployment.  NO_BASELINE carries the guidance instead; returns the
    non-optional root for the caller."""
    root = ctx.diagnostics_root
    if root is None or not Path(root).is_dir():
        raise DrawbridgeError(
            "diagnostics root is not ready (not registered, or the "
            "directory does not exist); before the first deployment, point "
            "apps.yaml diagnostics.root at an existing snapshot/repository "
            "directory",
            code=ErrorCode.NO_BASELINE,
        )
    return root


def handle_config_read(ctx: BuiltinContext, alias: str) -> dict[str, Any]:
    diagnostics_root = _require_diagnostics_root(ctx)
    alias_spec = ctx.config_files.get(alias)
    if alias_spec is None:
        raise DrawbridgeError(
            f"file alias {alias!r} is not registered", code=ErrorCode.INVALID_PARAMETER
        )

    fd = open_file_nofollow(diagnostics_root, alias_spec.path)
    with os.fdopen(fd, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        data = fh.read(CONFIG_READ_MAX_BYTES + 1)
    truncated = len(data) > CONFIG_READ_MAX_BYTES
    raw = data[:CONFIG_READ_MAX_BYTES].decode("utf-8", errors="replace")

    observed_at = time.time()
    if alias_spec.raw:
        return {
            "file": alias,
            "format": "raw",
            "content": raw,
            "truncated": truncated,
            "size_bytes": size,
            "observed_at": observed_at,
        }

    fmt = "json" if alias_spec.path.endswith((".json",)) else "toml"
    structured = _load_structured(raw, fmt)
    fields: dict[str, Any] = {}
    missing: list[str] = []
    for field_name in alias_spec.fields:
        if field_name in structured:
            fields[field_name] = structured[field_name]
        else:
            missing.append(field_name)
    for sensitive in alias_spec.sensitive_fields:
        fields.pop(sensitive, None)
    return {
        "file": alias,
        "format": fmt,
        "fields": fields,
        "missing_fields": missing,
        "truncated": truncated,
        "size_bytes": size,
        "observed_at": observed_at,
    }


def handle_project_list(
    ctx: BuiltinContext,
    subdir: str,
    *,
    cursor: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Bounded single-directory listing with cursor pagination (plan D8).

    ``cursor`` is an opaque offset string produced by this handler's own
    ``next_cursor``; anything else is rejected, never silently restarted
    from the top.  ``limit`` bounds the page size (1-200; the directory
    scan itself stays capped at PROJECT_LIST_MAX_ENTRIES).
    """
    if not 1 <= limit <= 200:
        raise DrawbridgeError(
            f"limit must be between 1 and 200 (got {limit})",
            code=ErrorCode.INVALID_PARAMETER,
        )
    diagnostics_root = _require_diagnostics_root(ctx)
    target = (
        Path(os.path.realpath(diagnostics_root))
        if subdir in (".", "")
        else _resolve_dir(diagnostics_root, subdir)
    )
    if not target.is_dir():
        raise DrawbridgeError(
            f"{subdir!r} is not a directory inside the diagnostic root",
            code=ErrorCode.INVALID_PARAMETER,
        )

    # Bounded scan: stop consuming the directory after the entry cap so a
    # huge directory cannot be fully materialized, then sort only the page.
    scanned: list[os.DirEntry[str]] = []
    scan_truncated = False
    try:
        with os.scandir(target) as iterator:
            for entry in iterator:
                scanned.append(entry)
                if len(scanned) > PROJECT_LIST_MAX_ENTRIES:
                    scan_truncated = True
                    break
    except OSError as exc:
        raise DrawbridgeError(f"cannot list {subdir!r}: {exc}", code=ErrorCode.INTERNAL) from exc
    scanned.sort(key=lambda e: e.name)

    entries: list[dict[str, Any]] = []
    truncated = scan_truncated
    if cursor in (None, ""):
        start_index = 0
    elif cursor.isdigit() and len(cursor) <= 7:
        start_index = int(cursor)
    else:
        raise DrawbridgeError(
            "cursor is not a valid project_list cursor",
            code=ErrorCode.INVALID_PARAMETER,
        )
    for index, entry in enumerate(scanned):
        if index < start_index:
            continue
        if len(entries) >= limit:
            truncated = True
            break
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat_module.S_ISLNK(st.st_mode):
            kind = "symlink"
        elif stat_module.S_ISDIR(st.st_mode):
            kind = "dir"
        elif stat_module.S_ISREG(st.st_mode):
            kind = "file"
        else:
            kind = "other"
        size = st.st_size if kind == "file" else None
        entries.append({"name": entry.name, "type": kind, "size_bytes": size})

    next_cursor = None
    if truncated and entries:
        next_cursor = str(start_index + len(entries))
    return {
        "subdir": subdir,
        "entries": entries,
        "truncated": truncated,
        "next_cursor": next_cursor,
        "observed_at": time.time(),
    }


def _resolve_dir(root: str, subdir: str) -> Path:
    """Directory variant of the no-follow walk (each component lstat'ed)."""
    current = Path(root)
    for part in Path(subdir).parts:
        if part in ("..", "."):
            raise DrawbridgeError(
                f"subdir {subdir!r} is not allowed", code=ErrorCode.INVALID_PARAMETER
            )
        current = current / part
        try:
            st = os.lstat(current)
        except OSError as exc:
            raise FileNotFoundError(str(current)) from exc
        if stat_module.S_ISLNK(st.st_mode):
            raise PermissionError(f"symbol link component is not allowed: {current}")
        if not stat_module.S_ISDIR(st.st_mode) and not stat_module.S_ISREG(st.st_mode):
            raise PermissionError(f"not a directory: {current}")
    if not current.is_dir():
        raise DrawbridgeError(
            f"subdir {subdir!r} is not a directory", code=ErrorCode.INVALID_PARAMETER
        )
    return current


#: Runtime platform constant (kept out of inline sys.platform checks so
#: static analysis on a dev host cannot fold away the Linux code paths).
_RUNTIME_LINUX = sys.platform == "linux"


def handle_config_validate(ctx: BuiltinContext, alias: str) -> dict[str, Any]:
    """Built-in syntax validation of a registered config file.

    JSON/TOML use the built-in parsers (MVP §4: 首批内置解析器).  Plugin
    validators that execute project code are isolated_test-classified and
    are NOT part of this read handler.
    """
    result = handle_config_read(ctx, alias)
    fmt = result.get("format")
    if fmt == "raw":
        raise DrawbridgeError(
            f"file alias {alias!r} is declared raw and has no structured format",
            code=ErrorCode.INVALID_PARAMETER,
        )
    observed_at = time.time()
    if result["truncated"]:
        return {
            "file": alias,
            "valid": False,
            "errors": ["file exceeds the 64 KiB validation budget"],
            "observed_at": observed_at,
        }
    # The parse already happened inside handle_config_read; a failure would
    # have raised. Reaching this point means the file parsed cleanly.
    return {
        "file": alias,
        "format": fmt,
        "valid": True,
        "errors": [],
        "observed_at": observed_at,
    }


def handle_host_metrics() -> dict[str, Any]:
    """Aggregated host metrics; Linux-only by design (910B deployment)."""
    if _RUNTIME_LINUX:
        return _linux_metrics()
    raise UnsupportedError("host metrics are only available on Linux")


def _linux_metrics() -> dict[str, Any]:
    def read(path: str) -> str:
        with open(path, encoding="ascii") as fh:
            return fh.read()

    load1, load5, load15 = parse_loadavg(read("/proc/loadavg"))
    mem: dict[str, int] = {}
    for line in read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        mem[key.strip()] = int(rest.strip().split()[0]) * 1024
    mem_total = mem.get("MemTotal", 0)
    mem_available = mem.get("MemAvailable", 0)
    # POSIX-only API; guarded at runtime by handle_host_metrics.
    statvfs = getattr(os, "statvfs")  # noqa: B009
    usage = statvfs("/")
    disk_total = usage.f_blocks * usage.f_frsize
    disk_free = usage.f_bavail * usage.f_frsize
    return {
        "load": {"1m": load1, "5m": load5, "15m": load15},
        "memory": {
            "total_bytes": mem_total,
            "available_bytes": mem_available,
            "used_ratio": round(1 - mem_available / mem_total, 4) if mem_total else None,
        },
        "disk_root": {
            "total_bytes": disk_total,
            "free_bytes": disk_free,
            "used_ratio": round(1 - disk_free / disk_total, 4) if disk_total else None,
        },
        "observed_at": time.time(),
    }
