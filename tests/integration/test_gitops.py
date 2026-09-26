"""Real-repository tests for GitClient, RefResolver and safe extraction.

Git is required; a fully isolated origin + working clone is created in a
temporary directory, so no network access is involved.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from drawbridge.config.models import GitConfig, SourceMode
from drawbridge.errors import (
    DrawbridgeError,
    InvalidParameterError,
    UnknownRefError,
    UnreachableRefError,
)
from drawbridge.executor.process import ProcessManager
from drawbridge.executor.spec import git_environment
from drawbridge.fsops import UnsafeArchiveError, safe_extract_tar
from drawbridge.gitops import GitClient
from drawbridge.runner.plan import RefResolver

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")


def run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


@pytest.fixture(scope="module")
def git_path() -> str:
    return shutil.which("git") or "/usr/bin/git"


@pytest.fixture(scope="module")
def repo_env() -> dict[str, str]:
    return git_environment({}, platform=sys.platform)


@pytest.fixture(scope="module")
def repos(tmp_path_factory: pytest.TempPathFactory, git_path: str, repo_env: dict[str, str]):
    """origin (bare) with main + agent/x branches, and a registered clone."""
    root = tmp_path_factory.mktemp("repos")
    work = root / "seed"
    origin = root / "origin.git"
    clone = root / "repos" / "demo"

    work.mkdir(parents=True)
    run_git(work, "init", "-b", "main")
    run_git(work, "config", "user.email", "test@drawbridge.local")
    run_git(work, "config", "user.name", "Drawbridge Tests")
    (work / "app.txt").write_text("v1\n", encoding="utf-8")
    run_git(work, "add", ".")
    run_git(work, "commit", "-m", "initial commit")
    run_git(work, "clone", "--bare", str(work), str(origin))

    clone.parent.mkdir(parents=True, exist_ok=True)
    run_git(root, "clone", str(origin), str(clone))
    run_git(clone, "config", "user.email", "test@drawbridge.local")
    run_git(clone, "config", "user.name", "Drawbridge Tests")
    # The hardened child-git environment runs without the user's global
    # config (autocrlf off), so keep the working tree byte-stable.
    run_git(clone, "config", "core.autocrlf", "false")
    run_git(clone, "checkout", "--", ".")

    run_git(clone, "switch", "-c", "agent/fix-1")
    (clone / "app.txt").write_text("v2-agent\n", encoding="utf-8")
    run_git(clone, "add", ".")
    run_git(clone, "commit", "-m", "agent change")
    run_git(clone, "push", "origin", "agent/fix-1")

    # A local-only branch that never reached origin.
    run_git(clone, "switch", "-c", "secret/local")
    (clone / "app.txt").write_text("v3-local\n", encoding="utf-8")
    run_git(clone, "add", ".")
    run_git(clone, "commit", "-m", "local change")
    run_git(clone, "switch", "main")

    return {"origin": origin, "clone": clone}


@pytest.fixture()
def git_config() -> GitConfig:
    return GitConfig.model_validate(
        {
            "repo_path": "/placeholder/set-per-test",
            "origin": "placeholder",
            "allowed_ref_patterns": [
                r"refs/heads/main",
                r"refs/heads/agent/[A-Za-z0-9_-]+",
            ],
        }
    )


@pytest.fixture()
def resolver(repos, git_path, repo_env, git_config):
    pm = ProcessManager()
    client = GitClient(
        git_path=git_path,
        repo_path=str(repos["clone"]),
        env=repo_env,
        process_manager=pm,
    )
    git_config.repo_path = str(repos["clone"])
    git_config.origin = str(repos["origin"])
    return RefResolver(git=client, git_config=git_config)


class TestGitClient:
    async def test_status_and_log(self, repos, git_path, repo_env) -> None:
        pm = ProcessManager()
        client = GitClient(
            git_path=git_path,
            repo_path=str(repos["clone"]),
            env=repo_env,
            process_manager=pm,
        )
        assert await client.status_porcelain() == []
        entries = await client.log("HEAD", count=10)
        assert entries[0].subject in {"initial commit", "local change"}

    async def test_check_ref_format(self, resolver: RefResolver) -> None:
        assert await resolver.git.check_ref_format("refs/heads/agent/fix-1")
        assert not await resolver.git.check_ref_format("refs/heads/bad name")

    async def test_enumerate_remote_refs(self, resolver: RefResolver) -> None:
        refs = await resolver.git.enumerate_remote_refs()
        assert "refs/heads/main" in refs
        assert "refs/heads/agent/fix-1" in refs


class TestRefResolution:
    async def test_local_branch_resolution(self, resolver: RefResolver) -> None:
        result = await resolver.resolve(
            source_mode=SourceMode.LOCAL, git_ref="refs/heads/main"
        )
        assert result.resolved_via == "branch"
        assert result.mapped_ref == "refs/heads/main"
        assert len(result.commit_sha) == 40

    async def test_fetch_branch_resolution(self, resolver: RefResolver) -> None:
        result = await resolver.resolve(
            source_mode=SourceMode.FETCH, git_ref="refs/heads/agent/fix-1"
        )
        assert result.mapped_ref == "refs/remotes/origin/agent/fix-1"
        assert result.fetched_refspec is not None

    async def test_unregistered_branch_rejected(self, resolver: RefResolver) -> None:
        with pytest.raises(InvalidParameterError, match="not registered"):
            await resolver.resolve(
                source_mode=SourceMode.LOCAL, git_ref="refs/heads/secret/local"
            )

    async def test_missing_branch_rejected_local(self, resolver: RefResolver) -> None:
        with pytest.raises(UnknownRefError):
            await resolver.resolve(
                source_mode=SourceMode.LOCAL, git_ref="refs/heads/agent/nope"
            )

    async def test_missing_branch_rejected_fetch(self, resolver: RefResolver) -> None:
        with pytest.raises(UnknownRefError, match="does not exist on origin"):
            await resolver.resolve(
                source_mode=SourceMode.FETCH, git_ref="refs/heads/agent/nope"
            )

    async def test_short_sha_shape_rejected_by_regex(self, resolver: RefResolver) -> None:
        with pytest.raises(InvalidParameterError, match="no allowed shape"):
            await resolver.resolve(source_mode=SourceMode.LOCAL, git_ref="abcdef1234")

    async def test_sha_reachable_from_local_main(self, repos, resolver: RefResolver) -> None:
        sha = run_git(repos["clone"], "rev-parse", "refs/heads/main").strip()
        result = await resolver.resolve(source_mode=SourceMode.LOCAL, git_ref=sha)
        assert result.commit_sha == sha
        assert result.resolved_via == "sha"

    async def test_sha_unreachable_rejected(self, repos, resolver: RefResolver) -> None:
        # A commit on the local-only branch is not reachable from any
        # registered source (main / agent/*).
        sha = run_git(repos["clone"], "rev-parse", "refs/heads/secret/local").strip()
        with pytest.raises(UnreachableRefError):
            await resolver.resolve(source_mode=SourceMode.LOCAL, git_ref=sha)

    async def test_sha_fetch_mode_uses_fresh_remote_tips(
        self, repos, resolver: RefResolver
    ) -> None:
        sha = run_git(repos["clone"], "rev-parse", "refs/heads/agent/fix-1").strip()
        result = await resolver.resolve(source_mode=SourceMode.FETCH, git_ref=sha)
        assert result.commit_sha == sha

    async def test_tags_disabled(self, resolver: RefResolver) -> None:
        with pytest.raises(InvalidParameterError, match="tags are not enabled"):
            await resolver.resolve(
                source_mode=SourceMode.LOCAL, git_ref="refs/tags/v1.0.0"
            )

    async def test_snapshot_archive_and_extract(self, repos, resolver, tmp_path: Path) -> None:
        sha = run_git(repos["clone"], "rev-parse", "refs/heads/main").strip()
        archive = tmp_path / "snapshot.tar"
        await resolver.git.archive(sha, archive)
        dest = tmp_path / "src"
        stats = safe_extract_tar(archive, dest)
        assert (dest / "app.txt").read_text(encoding="utf-8").startswith("v")
        assert stats["members"] >= 1


class TestSafeExtraction:
    def _tar_with(self, tmp_path: Path, add_members) -> Path:
        import io
        import tarfile

        archive = tmp_path / "m.tar"
        with tarfile.open(archive, "w") as tar:
            add_members(tar, io)
        return archive

    def test_rejects_absolute_path(self, tmp_path: Path) -> None:
        import tarfile

        def add(tar, io):
            data = b"evil"
            info = tarfile.TarInfo(name="/etc/evil.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        archive = self._tar_with(tmp_path, add)
        with pytest.raises(UnsafeArchiveError, match="absolute"):
            safe_extract_tar(archive, tmp_path / "out")

    def test_rejects_traversal(self, tmp_path: Path) -> None:
        import tarfile

        def add(tar, io):
            data = b"evil"
            info = tarfile.TarInfo(name="../evil.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        archive = self._tar_with(tmp_path, add)
        with pytest.raises(UnsafeArchiveError, match="traversal"):
            safe_extract_tar(archive, tmp_path / "out")

    def test_rejects_symlink(self, tmp_path: Path) -> None:
        import tarfile

        def add(tar, io):
            info = tarfile.TarInfo(name="link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)

        archive = self._tar_with(tmp_path, add)
        with pytest.raises(UnsafeArchiveError, match="link member"):
            safe_extract_tar(archive, tmp_path / "out")

    def test_rejects_duplicate_members(self, tmp_path: Path) -> None:
        import tarfile

        def add(tar, io):
            for content in (b"first", b"second"):
                info = tarfile.TarInfo(name="dup.txt")
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))

        archive = self._tar_with(tmp_path, add)
        with pytest.raises(UnsafeArchiveError, match="duplicate"):
            safe_extract_tar(archive, tmp_path / "out")

    def test_rejects_oversized_member(self, tmp_path: Path, monkeypatch) -> None:
        import tarfile

        import drawbridge.fsops as fsops

        monkeypatch.setattr(fsops, "MAX_FILE_BYTES", 16)

        def add(tar, io):
            info = tarfile.TarInfo(name="big.bin")
            info.size = 32
            tar.addfile(info, io.BytesIO(b"x" * 32))

        archive = self._tar_with(tmp_path, add)
        with pytest.raises(UnsafeArchiveError, match="exceeds"):
            safe_extract_tar(archive, tmp_path / "out")


class TestOutputParsingSeesFullBudget:
    async def test_ls_remote_spec_carries_full_summary(
        self, repos, git_path: str, repo_env: dict[str, str]
    ) -> None:
        """Parsing must see the complete bounded output, not a 64 KiB
        head+tail ring (refs silently dropped from the middle)."""
        from drawbridge.gitops import REMOTE_OUTPUT_LIMIT

        seen: dict[str, object] = {}

        class CapturingPM(ProcessManager):
            async def execute(self, spec):  # type: ignore[no-untyped-def]
                seen["spec"] = spec
                return await super().execute(spec)

        client = GitClient(
            git_path=git_path,
            repo_path=str(repos["clone"]),
            env=repo_env,
            process_manager=CapturingPM(),
        )
        refs = await client.enumerate_remote_refs()
        assert refs  # origin advertises main + agent/fix-1
        spec = seen["spec"]
        assert spec.max_output_bytes == REMOTE_OUTPUT_LIMIT
        assert spec.summary_bytes == REMOTE_OUTPUT_LIMIT


class TestGitGlobalOptions:
    async def test_gc_auto_disabled_in_every_argv(
        self, repos, git_path: str, repo_env: dict[str, str]
    ) -> None:
        """D5: fetch on a large repo must never stall in auto-gc."""
        seen: list[tuple[str, ...]] = []

        class CapturingPM(ProcessManager):
            async def execute(self, spec):  # type: ignore[no-untyped-def]
                seen.append(tuple(spec.argv))
                return await super().execute(spec)

        client = GitClient(
            git_path=git_path,
            repo_path=str(repos["clone"]),
            env=repo_env,
            process_manager=CapturingPM(),
        )
        await client.status_porcelain()
        await client.enumerate_remote_refs()
        assert seen, "no git invocations were captured"
        for argv in seen:
            assert "gc.auto=0" in argv
            assert argv[argv.index("gc.auto=0") - 1] == "-c"


class TestRepoLocalConfigGuard:
    """D5: dangerous keys in <repo>/.git/config are rejected before any
    remote contact — no git subprocess, no network."""

    @staticmethod
    def _client_with_config(
        tmp_path: Path, git_path: str, repo_env: dict[str, str], config_text: str
    ) -> tuple[GitClient, list[tuple[str, ...]]]:
        # The guard only reads <repo>/.git/config and never runs git (the
        # recording PM asserts that), so a skeleton directory suffices.
        repo = tmp_path / "guarded-repo"
        (repo / ".git").mkdir(parents=True, exist_ok=True)
        (repo / ".git" / "config").write_text(config_text, encoding="utf-8")
        executed: list[tuple[str, ...]] = []

        class RecordingPM(ProcessManager):
            async def execute(self, spec):  # type: ignore[no-untyped-def]
                executed.append(tuple(spec.argv))
                raise AssertionError("guard must reject before any git run")

        client = GitClient(
            git_path=git_path,
            repo_path=str(repo),
            env=repo_env,
            process_manager=RecordingPM(),
        )
        return client, executed

    async def test_include_directive_rejected(
        self, tmp_path: Path, git_path: str, repo_env: dict[str, str]
    ) -> None:
        client, executed = self._client_with_config(
            tmp_path,
            git_path,
            repo_env,
            "[include]\n\tpath = /etc/evil-gitconfig\n",
        )
        with pytest.raises(DrawbridgeError) as exc:
            await client.enumerate_remote_refs()
        assert exc.value.code == "REPO_CONFIG_REJECTED"
        assert "include" in str(exc.value)
        assert executed == []  # no subprocess ever ran

    async def test_insteadof_rejected(
        self, tmp_path: Path, git_path: str, repo_env: dict[str, str]
    ) -> None:
        client, executed = self._client_with_config(
            tmp_path,
            git_path,
            repo_env,
            '[url "https://evil.example/"]\n\tinsteadOf = https://github.com/\n',
        )
        with pytest.raises(DrawbridgeError, match="insteadof"):
            await client.fetch_refspec("refs/heads/main")
        assert executed == []

    async def test_binary_config_rejected(
        self, tmp_path: Path, git_path: str, repo_env: dict[str, str]
    ) -> None:
        repo = tmp_path / "binary-repo"
        (repo / ".git").mkdir(parents=True, exist_ok=True)
        (repo / ".git" / "config").write_bytes(b"[core]\n\x00\n")
        executed: list[tuple[str, ...]] = []

        class RecordingPM(ProcessManager):
            async def execute(self, spec):  # type: ignore[no-untyped-def]
                executed.append(tuple(spec.argv))
                raise AssertionError("guard must reject before any git run")

        client = GitClient(
            git_path=git_path,
            repo_path=str(repo),
            env=repo_env,
            process_manager=RecordingPM(),
        )
        with pytest.raises(DrawbridgeError, match="binary"):
            await client.enumerate_remote_refs()
        assert executed == []

    async def test_scan_table(self) -> None:
        from drawbridge.gitops import scan_repo_config_dangerous_keys

        clean = (
            '[core]\n\trepositoryformatversion = 0\n'
            '[remote "origin"]\n\turl = /srv/origin/demo.git\n'
            '[branch "main"]\n\tremote = origin\n'
        )
        assert scan_repo_config_dangerous_keys(clean) == []
        assert scan_repo_config_dangerous_keys(
            '[includeIf "gitdir:~/"]\n\tpath = ~/evil\n'
        ) == ["[includeif]"]
        assert scan_repo_config_dangerous_keys(
            '[core]\n\tsshCommand = ssh -i /tmp/evil\n'
        ) == ["core.sshcommand"]
        assert scan_repo_config_dangerous_keys(
            "[credential]\n\thelper = store\n"
        ) == ["credential.helper"]
        assert scan_repo_config_dangerous_keys(
            '[submodule "evil"]\n\tupdate = !rm -rf /\n'
        ) == ["submodule.evil.update"]
        assert scan_repo_config_dangerous_keys(
            '[http]\n\textraHeader = Authorization: evil\n'
        ) == ["http.extraheader"]
        # benign keys under the same sections stay allowed
        assert scan_repo_config_dangerous_keys(
            '[url "https://example.com"]\n\tother = 1\n'
        ) == []
