"""Filesystem safety helpers: safe tar extraction and bounded path opens.

Used by the source snapshot unpacker (MVP spec §5) and by the built-in
project diagnostic handlers (config_read / project_list, MVP spec §4).

Snapshot extraction limits and refusals:

* at most 100 000 members, 1 GiB total expansion, 100 MiB per file;
* absolute paths, ``..`` segments, device/FIFO entries, hardlinks and
  symlinks are rejected — a snapshot containing a symlink is rejected as a
  whole (MVP: 拒绝含源码符号链接的项目);
* duplicate members must not overwrite each other;
* extraction happens into a fresh job-specific directory and never applies
  the archive's permission bits.
"""

from __future__ import annotations

import os
import stat
import tarfile
from pathlib import Path, PurePosixPath

MAX_MEMBERS = 100_000
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024


class UnsafeArchiveError(Exception):
    """The archive violates snapshot extraction rules."""


def check_member_name(name: str) -> PurePosixPath:
    """Reject absolute paths, drive letters, ``..`` and empty members."""
    if not name:
        raise UnsafeArchiveError("empty member name")
    pure = PurePosixPath(name)
    if pure.is_absolute() or name.startswith("/"):
        raise UnsafeArchiveError(f"absolute path member: {name!r}")
    if "\\" in name or ":" in name:
        raise UnsafeArchiveError(f"non-posix path member: {name!r}")
    parts = pure.parts
    if any(part == ".." for part in parts):
        raise UnsafeArchiveError(f"path traversal member: {name!r}")
    if not parts:
        raise UnsafeArchiveError(f"invalid member: {name!r}")
    return pure


def safe_extract_tar(archive_path: str | Path, dest_dir: str | Path) -> dict[str, int]:
    """Extract a git archive tarball under hard limits; returns statistics."""
    archive = Path(archive_path)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    total = 0
    members = 0
    with tarfile.open(archive, "r:") as tar:
        for member in tar:
            members += 1
            if members > MAX_MEMBERS:
                raise UnsafeArchiveError(f"more than {MAX_MEMBERS} archive members")
            if member.issym() or member.islnk():
                raise UnsafeArchiveError(
                    f"link member is not allowed: {member.name!r}"
                )
            if not member.isfile():
                if member.isdir():
                    continue
                raise UnsafeArchiveError(
                    f"special file member is not allowed: {member.name!r}"
                )
            pure = check_member_name(member.name)
            if member.name in seen:
                raise UnsafeArchiveError(f"duplicate member: {member.name!r}")
            seen.add(member.name)
            if not member.size:
                target = dest.joinpath(*pure.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
                continue
            if member.size > MAX_FILE_BYTES:
                raise UnsafeArchiveError(
                    f"member exceeds {MAX_FILE_BYTES} bytes: {member.name!r}"
                )
            total += member.size
            if total > MAX_TOTAL_BYTES:
                raise UnsafeArchiveError(f"archive expands beyond {MAX_TOTAL_BYTES} bytes")
            target = dest.joinpath(*pure.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                raise UnsafeArchiveError(f"unreadable member: {member.name!r}")
            with target.open("wb") as out:
                remaining = member.size
                while remaining > 0:
                    chunk = extracted.read(min(65536, remaining))
                    if not chunk:
                        raise UnsafeArchiveError(
                            f"truncated member: {member.name!r}"
                        )
                    out.write(chunk)
                    remaining -= len(chunk)
            # Never apply archive permission bits (no setuid/setgid propagation).
            os.chmod(target, 0o644)
    return {"members": members, "bytes": total}


def is_within_root(root: str | Path, candidate: str | Path) -> bool:
    """True if ``candidate`` resolves inside ``root`` (after resolution)."""
    root_real = Path(os.path.realpath(root))
    cand_real = Path(os.path.realpath(candidate))
    try:
        cand_real.relative_to(root_real)
    except ValueError:
        return False
    return True


def reject_symlink_components(root: str | Path, relative: str) -> Path:
    """Open-safe path resolution for diagnostic file access.

    Every component under ``root`` is checked with O_NOFOLLOW-equivalent
    semantics (lstat per segment); the final component must be a regular
    file.  Returns the resolved path or raises.
    """
    root_path = Path(root)
    candidate = root_path
    for part in Path(relative).parts:
        candidate = candidate / part
        try:
            st = os.lstat(candidate)
        except OSError as exc:
            raise FileNotFoundError(str(candidate)) from exc
        if stat.S_ISLNK(st.st_mode):
            raise PermissionError(f"symbol link component is not allowed: {candidate}")
    final_st = os.lstat(candidate)
    if not stat.S_ISREG(final_st.st_mode):
        raise PermissionError(f"not a regular file: {candidate}")
    if not is_within_root(root_path, candidate):
        raise PermissionError(f"path escapes diagnostic root: {relative}")
    return candidate
