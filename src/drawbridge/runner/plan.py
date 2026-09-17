"""Plan-time source resolution (MVP spec §2, §5).

Implements the frozen ref resolution rules:

* only three shapes pass: ``refs/heads/...``, ``refs/tags/...``, 40-hex SHA;
* a branch/tag must additionally pass ``git check-ref-format`` and match one
  of the app's registered allow patterns; tags only when enabled;
* fetch mode maps a branch to ``refs/remotes/origin/<branch>`` after a
  fresh, targeted fetch; local mode maps to ``refs/heads/<branch>``;
* a SHA must resolve to a commit reachable from a currently allowed tip of
  the active mode (remote-tracking tips for fetch, local heads/tags for
  local) — modes are never mixed;
* remote ref enumeration is bounded (≤ 10 000 refs / 2 MiB output) and only
  tips refreshed during THIS plan are trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import regex as regex_module

from drawbridge.config.models import GitConfig, SourceMode
from drawbridge.errors import (
    DrawbridgeError,
    ErrorCode,
    InvalidParameterError,
    UnknownRefError,
    UnreachableRefError,
)
from drawbridge.gitops import MAX_REMOTE_REFS, GitClient, GitError
from drawbridge.policy.params import REGEX_TIMEOUT_SECONDS

_BRANCH_RE = re.compile(r"refs/heads/([A-Za-z0-9][A-Za-z0-9._/-]*)")
_TAG_RE = re.compile(r"refs/tags/([A-Za-z0-9][A-Za-z0-9._/-]*)")
_SHA_RE = re.compile(r"[0-9a-f]{40}")

#: Maximum number of allowed source tips considered for reachability.
MAX_ALLOWED_SOURCES = 100


@dataclass(frozen=True)
class PlanResolution:
    commit_sha: str
    mapped_ref: str
    resolved_via: str  # branch | tag | sha
    fetched_refspec: str | None


def _pattern_fullmatch(pattern: str, value: str) -> bool:
    try:
        return regex_module.fullmatch(pattern, value, timeout=REGEX_TIMEOUT_SECONDS) is not None
    except TimeoutError:
        return False
    except regex_module.error:
        return False


class RefResolver:
    def __init__(self, git: GitClient, git_config: GitConfig) -> None:
        self.git = git
        self.git_config = git_config

    async def resolve(self, *, source_mode: SourceMode, git_ref: str) -> PlanResolution:
        branch = _BRANCH_RE.fullmatch(git_ref)
        tag = _TAG_RE.fullmatch(git_ref)
        sha = _SHA_RE.fullmatch(git_ref)

        if branch:
            return await self._resolve_branch(source_mode, branch.group(1))
        if tag:
            return await self._resolve_tag(source_mode, tag.group(1))
        if sha:
            return await self._resolve_sha(source_mode, git_ref)
        raise InvalidParameterError(f"git_ref {git_ref!r} has no allowed shape")

    # -- shared checks ------------------------------------------------------

    def _matches_allowed(self, full_ref: str) -> bool:
        patterns = self.git_config.allowed_ref_patterns
        if len(patterns) > MAX_ALLOWED_SOURCES:  # pragma: no cover - load-time bound
            raise DrawbridgeError(
                "too many allowed ref patterns", code=ErrorCode.CONFIG_INVALID
            )
        return any(_pattern_fullmatch(p, full_ref) for p in patterns)

    async def _registered(self, full_ref: str, kind: str, name: str) -> None:
        if not await self.git.check_ref_format(full_ref):
            raise InvalidParameterError(f"{kind} {name!r} fails git check-ref-format")
        if not self._matches_allowed(full_ref):
            raise InvalidParameterError(
                f"ref {full_ref!r} is not registered for this app"
            )

    async def _resolve_or_unknown(self, mapped_ref: str) -> str:
        try:
            return await self.git.resolve_commit(mapped_ref)
        except GitError as exc:
            raise UnknownRefError(f"cannot resolve {mapped_ref!r}: {exc}") from exc

    async def _require_on_origin(self, full_ref: str, remote_refs: dict[str, str]) -> None:
        if full_ref not in remote_refs:
            raise UnknownRefError(f"ref {full_ref!r} does not exist on origin")
        if len(remote_refs) > MAX_REMOTE_REFS:  # pragma: no cover - gitops also guards
            raise DrawbridgeError("remote advertises too many refs", code=ErrorCode.SOURCE_INVALID)

    # -- branches -------------------------------------------------------------

    async def _resolve_branch(self, source_mode: SourceMode, name: str) -> PlanResolution:
        full_ref = f"refs/heads/{name}"
        await self._registered(full_ref, "branch", name)

        if source_mode is SourceMode.LOCAL:
            sha = await self._resolve_or_unknown(full_ref)
            return PlanResolution(sha, full_ref, "branch", None)

        remote_refs = await self.git.enumerate_remote_refs()
        await self._require_on_origin(full_ref, remote_refs)
        mapped = f"refs/remotes/origin/{name}"
        refspec = f"+{full_ref}:{mapped}"
        await self.git.fetch_refspec(refspec)
        sha = await self._resolve_or_unknown(mapped)
        return PlanResolution(sha, mapped, "branch", refspec)

    # -- tags -----------------------------------------------------------------

    async def _resolve_tag(self, source_mode: SourceMode, name: str) -> PlanResolution:
        if not self.git_config.tags_enabled:
            raise InvalidParameterError("tags are not enabled for this app")
        full_ref = f"refs/tags/{name}"
        await self._registered(full_ref, "tag", name)

        if source_mode is SourceMode.LOCAL:
            sha = await self._resolve_or_unknown(full_ref)
            return PlanResolution(sha, full_ref, "tag", None)

        remote_refs = await self.git.enumerate_remote_refs()
        await self._require_on_origin(full_ref, remote_refs)
        refspec = f"+{full_ref}:{full_ref}"
        await self.git.fetch_refspec(refspec)
        sha = await self._resolve_or_unknown(full_ref)
        return PlanResolution(sha, full_ref, "tag", refspec)

    # -- raw SHAs ---------------------------------------------------------------

    async def _resolve_sha(self, source_mode: SourceMode, sha: str) -> PlanResolution:
        commit = await self._resolve_or_unknown(sha)
        if commit != sha:
            raise InvalidParameterError("git_ref must be the exact commit it resolves to")
        if not await self._reachable_from_allowed(source_mode, commit):
            raise UnreachableRefError(
                "commit is not reachable from any currently allowed source"
            )
        return PlanResolution(commit, commit, "sha", None)

    async def _allowed_tips(self, source_mode: SourceMode) -> list[str]:
        """Currently allowed tips, freshly refreshed, for the active mode."""
        if source_mode is SourceMode.LOCAL:
            refs = await self.git.show_refs()
            local_tips = [
                name
                for name in refs
                if name.startswith("refs/heads/")
                or (self.git_config.tags_enabled and name.startswith("refs/tags/"))
            ]
            return [name for name in sorted(local_tips) if self._matches_allowed(name)]

        remote_refs = await self.git.enumerate_remote_refs()
        allowed_heads = sorted(
            name
            for name in remote_refs
            if name.startswith("refs/heads/") and self._matches_allowed(name)
        )
        tips: list[str] = []
        for ref in allowed_heads:
            name = ref.removeprefix("refs/heads/")
            mapped = f"refs/remotes/origin/{name}"
            try:
                await self.git.fetch_refspec(f"+{ref}:{mapped}")
            except GitError:
                continue  # a vanished branch simply does not authorize
            tips.append(mapped)
        return tips

    async def _reachable_from_allowed(self, source_mode: SourceMode, commit: str) -> bool:
        tips = await self._allowed_tips(source_mode)
        if not tips:
            return False
        if len(tips) > MAX_ALLOWED_SOURCES:
            raise DrawbridgeError(
                "too many allowed source tips", code=ErrorCode.CONFIG_INVALID
            )
        for tip in tips:
            try:
                if await self.git.is_ancestor(commit, tip):
                    return True
            except GitError:
                continue
        return False
