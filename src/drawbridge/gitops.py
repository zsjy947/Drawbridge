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
from drawbridge.executor.process import ProcessManager
from drawbridge.executor.spec import ExecutionSpec

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_LOG_LINE_RE = re.compile(r"^([0-9a-f]{40})\t([0-9]+)\t(.*)$")

# repo / output budgets for git operations (MVP spec §2)
MAX_REMOTE_REFS = 10_000
REMOTE_OUTPUT_LIMIT = 2 * 1024 * 1024


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
            argv=argv,
            cwd=cwd or self._repo,
            env=self._env,
            profile=ExecutionProfile.SOURCE_MANAGE,
            timeout_seconds=timeout,
            output_policy=OutputPolicyKind.TERMINATE,
            max_output_bytes=max_output_bytes,
            hard_output_limit=max_output_bytes,
            accepted_exit_codes=accepted,
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
