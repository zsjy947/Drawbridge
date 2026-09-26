"""Controlled Git operations executed through the ProcessManager.

Every command uses the unified ``git_safe`` argv prefix, the hardened Git
environment (no prompts, no system config, fixed SSH wrapper) and the fixed
cwd of the registered repository.  Options that would extend protocol or
hook behaviour are pinned in the prefix; request input only fills typed
slots that were validated earlier (MVP spec §3, §5).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from drawbridge.config.models import ExecutionProfile, OutputPolicyKind
from drawbridge.errors import DrawbridgeError, ErrorCode
from drawbridge.executor.process import ProcessManager
from drawbridge.executor.spec import ExecutionSpec

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_LOG_LINE_RE = re.compile(r"^([0-9a-f]{40})\t([0-9]+)\t(.*)$")

# repo / output budgets for git operations (MVP spec §2)
MAX_REMOTE_REFS = 10_000
REMOTE_OUTPUT_LIMIT = 2 * 1024 * 1024

#: Global options prepended to EVERY git invocation (plan D5): auto-gc on a
#: large repository can block a fetch far beyond its 120s budget.
_GIT_GLOBAL_OPTIONS: tuple[str, ...] = ("-c", "gc.auto=0")

#: Repo-local .git/config scan bounds (plan D5): plain-text key matching,
#: never a git subprocess, and binary or oversized configs are rejected
#: outright rather than partially scanned.
REPO_CONFIG_MAX_BYTES = 1024 * 1024

_SECTION_RE = re.compile(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]')
_KEY_RE = re.compile(r"^([A-Za-z0-9-]+)\s*(?:=|$)")

#: Section names whose every key is rejected (external config inclusion).
_DANGEROUS_SECTIONS = frozenset({"include", "includeif"})

#: Exact fully-qualified keys rejected regardless of section context.
_DANGEROUS_FULL_KEYS = frozenset(
    {
        "core.sshcommand",
        "core.hookspath",
        "http.proxy",
        "https.proxy",
        "http.extraheader",
    }
)


def scan_repo_config_dangerous_keys(text: str) -> list[str]:
    """Plain-text scan of a repo-local ``.git/config`` (plan D5).

    The hardened git environment (GIT_CONFIG_NOSYSTEM, fixed
    GIT_CONFIG_GLOBAL, protocol.allow pins) does not cover the repository's
    OWN config: ``include``/``includeIf`` can pull in external configuration
    and ``url.<base>.insteadOf`` can rewrite where ``origin`` actually
    points while the argv still carries the literal ``origin``.  Returns the
    offending fully-qualified key names; empty means clean.
    """
    hits: list[str] = []
    section = ""
    subsection = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line[0] in "#;":
            continue
        section_match = _SECTION_RE.match(line)
        if section_match:
            section = section_match.group(1).lower()
            subsection = (section_match.group(2) or "").lower()
            if section in _DANGEROUS_SECTIONS:
                hits.append(f"[{section}]")
            continue
        key_match = _KEY_RE.match(line)
        if key_match and section:
            key = key_match.group(1).lower()
            if section in _DANGEROUS_SECTIONS:
                continue  # the section header itself was already reported
            full = f"{section}.{subsection}.{key}" if subsection else f"{section}.{key}"
            if (
                full in _DANGEROUS_FULL_KEYS
                or (section == "credential")
                or (section == "url" and subsection and key in ("insteadof", "pushinsteadof"))
                or (section == "submodule" and key == "update")
            ):
                hits.append(full)
    return hits


class GitError(Exception):
    """A git operation failed or returned an unusable result."""


class RefNotReachableError(GitError):
    pass


@dataclass(frozen=True)
class LogEntry:
    sha: str
    commit_time: int
    subject: str


class GitClient:
    """Executes whitelisted git commands inside one registered repository."""

    def __init__(
        self,
        *,
        git_path: str,
        repo_path: str,
        env: dict[str, str],
        process_manager: ProcessManager,
    ) -> None:
        self._git = git_path
        self._repo = repo_path
        self._env = env
        self._pm = process_manager
        self._repo_config_checked = False

    async def _guard_repo_local_config(self) -> None:
        """One-time scan of ``<repo>/.git/config`` before remote contact.

        Runs before ls-remote/fetch (the only remote-touching paths) and is
        deduplicated per client instance.  A missing config file is left to
        the git invocation itself; unreadable/oversized/binary content is
        rejected rather than partially trusted.
        """
        if self._repo_config_checked:
            return
        self._repo_config_checked = True
        path = Path(self._repo) / ".git" / "config"
        try:
            data = path.read_bytes()
        except OSError:
            return
        if len(data) > REPO_CONFIG_MAX_BYTES or b"\x00" in data:
            raise DrawbridgeError(
                f"repository local git config {path} is oversized or binary; "
                "refusing to touch the remote until it is cleaned up",
                code=ErrorCode.REPO_CONFIG_REJECTED,
            )
        hits = scan_repo_config_dangerous_keys(
            data.decode("utf-8", errors="replace")
        )
        if hits:
            raise DrawbridgeError(
                "repository local git config contains rejected keys: "
                + ", ".join(hits[:5])
                + f"; remove them from {path} before any fetch or ref listing",
                code=ErrorCode.REPO_CONFIG_REJECTED,
            )

    async def _run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        accepted: frozenset[int] = frozenset({0}),
        max_output_bytes: int = 65536,
        cwd: str | None = None,
    ) -> Any:
        spec = ExecutionSpec(
            operation="git",
            executable=self._git,
            argv=(*_GIT_GLOBAL_OPTIONS, *argv),
            cwd=cwd or self._repo,
            env=self._env,
            profile=ExecutionProfile.SOURCE_MANAGE,
            timeout_seconds=timeout,
            output_policy=OutputPolicyKind.TERMINATE,
            max_output_bytes=max_output_bytes,
            hard_output_limit=max_output_bytes,
            accepted_exit_codes=accepted,
            # Parsing (ls-remote / show-ref / log) must see the complete
            # bounded output, not a head+tail summary ring.
            summary_bytes=max_output_bytes,
        )
        return await self._pm.execute(spec)

    def _require(self, result: Any, expected: str) -> Any:
        if result.termination_reason != "completed":
            raise GitError(f"git {expected}: terminated ({result.termination_reason})")
        if result.exit_code != 0:
            raise GitError(
                f"git {expected}: exit {result.exit_code}: {result.stderr_preview.strip()[:200]}"
            )
        return result

    # -- read helpers ----------------------------------------------------

    async def status_porcelain(self, *, timeout: float = 10.0) -> list[str]:
        result = self._require(
            await self._run(
                ["--no-pager", "status", "--porcelain=v1", "--untracked-files=no"],
                timeout=timeout,
            ),
            "status",
        )
        return [line for line in result.stdout_preview.splitlines() if line.strip()]

    async def log(
        self, sha: str, *, count: int = 20, timeout: float = 10.0
    ) -> list[LogEntry]:
        result = self._require(
            await self._run(
                [
                    "--no-pager",
                    "log",
                    "--no-show-signature",
                    "--format=%H%x09%ct%x09%s",
                    f"--max-count={count}",
                    sha,
                    "--",
                ],
                timeout=timeout,
            ),
            "log",
        )
        entries: list[LogEntry] = []
        for line in result.stdout_preview.splitlines():
            match = _LOG_LINE_RE.match(line)
            if match:
                entries.append(
                    LogEntry(
                        sha=match.group(1),
                        commit_time=int(match.group(2)),
                        subject=match.group(3),
                    )
                )
        return entries

    # -- plan-time resolution ---------------------------------------------

    async def enumerate_remote_refs(self) -> dict[str, str]:
        """``ls-remote --refs origin`` — the only live remote contact."""
        await self._guard_repo_local_config()
        result = self._require(
            await self._run(
                ["--no-pager", "ls-remote", "--refs", "origin"],
                timeout=30.0,
                max_output_bytes=REMOTE_OUTPUT_LIMIT,
            ),
            "ls-remote",
        )
        refs: dict[str, str] = {}
        for line in result.stdout_preview.splitlines():
            parts = line.split()
            if len(parts) != 2 or not _SHA_RE.fullmatch(parts[0]):
                continue
            refs[parts[1]] = parts[0]
        if len(refs) > MAX_REMOTE_REFS:
            raise GitError(
                f"remote advertises {len(refs)} refs; limit is {MAX_REMOTE_REFS}"
            )
        return refs

    async def show_refs(self) -> dict[str, str]:
        """Local refs as ``{refname: sha}`` (heads and tags)."""
        result = self._require(
            await self._run(["--no-pager", "show-ref"], timeout=5.0),
            "show-ref",
        )
        refs: dict[str, str] = {}
        for line in result.stdout_preview.splitlines():
            parts = line.split()
            if len(parts) == 2 and _SHA_RE.fullmatch(parts[0]):
                refs[parts[1]] = parts[0]
        return refs

    async def check_ref_format(self, full_ref: str) -> bool:
        result = await self._run(
            ["--no-pager", "check-ref-format", full_ref], timeout=5.0
        )
        if result.termination_reason != "completed":
            return False
        return bool(result.exit_code == 0)

    async def resolve_commit(self, mapped_ref: str, *, timeout: float = 5.0) -> str:
        result = self._require(
            await self._run(
                [
                    "--no-pager",
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    f"{mapped_ref}^{{commit}}",
                ],
                timeout=timeout,
            ),
            "rev-parse",
        )
        sha: str = result.stdout_preview.strip()
        if not _SHA_RE.fullmatch(sha):
            raise GitError(f"rev-parse returned an unusable result: {sha[:80]!r}")
        return sha

    async def is_ancestor(self, sha: str, tip: str, *, timeout: float = 5.0) -> bool:
        """0 → reachable, 1 → not reachable, anything else → error."""
        result = await self._run(
            ["--no-pager", "merge-base", "--is-ancestor", sha, tip],
            timeout=timeout,
            accepted=frozenset({0, 1}),
        )
        if result.termination_reason != "completed":
            raise GitError("merge-base terminated abnormally")
        if result.exit_code == 0:
            return True
        if result.exit_code == 1:
            return False
        raise GitError(
            f"merge-base failed: {result.stderr_preview.strip()[:200]}"
        )

    async def fetch_refspec(self, refspec: str, *, timeout: float = 120.0) -> None:
        await self._guard_repo_local_config()
        self._require(
            await self._run(
                [
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "origin",
                    refspec,
                ],
                timeout=timeout,
            ),
            "fetch",
        )

    async def archive(self, sha: str, output_path: str | Path, *, timeout: float = 30.0) -> Path:
        self._require(
            await self._run(
                [
                    "archive",
                    "--format=tar",
                    f"--output={output_path}",
                    sha,
                ],
                timeout=timeout,
                max_output_bytes=4096,
            ),
            "archive",
        )
        return Path(output_path)
