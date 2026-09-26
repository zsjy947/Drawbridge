"""Production step executor for deploy_verify (MVP spec §5).

Every step builds a fixed :class:`ExecutionSpec` through the shared
ProcessManager — argv comes from validated configuration and typed state
only, never from request strings.  Pure argv/render helpers are kept
free of side effects so they can be unit-tested on development hosts;
everything that touches the host runtime (git archive extraction aside)
requires the Linux target and raises ``UNSUPPORTED`` elsewhere, so an
off-target deploy fails cleanly as ``Failed`` with no runtime change.

Implements exactly the internal operations registered in
``operations.yaml``: release_preflight, source_snapshot, image_build,
image_import, image_identify, compose_deploy, health_check, test_suite,
restore_previous, stop_initial, release_finalize.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from drawbridge.config.models import (
    DrawbridgeConfig,
    EnvironmentConfig,
    ExecutionProfile,
    OutputPolicyKind,
    TestSuiteConfig,
)
from drawbridge.errors import DrawbridgeError, ErrorCode
from drawbridge.executor.process import ProcessManager
from drawbridge.executor.spec import (
    ExecutionResult,
    ExecutionSpec,
    build_environment,
    docker_environment,
    git_environment,
    resolve_toolchain,
)
from drawbridge.fsops import safe_extract_tar
from drawbridge.gitops import GitClient
from drawbridge.runner.deploy import DeployState
from drawbridge.runner.health import probe_health_checks, require_healthy
from drawbridge.state.records import ReleaseRecord
from drawbridge.state.store import Store

_RUNTIME_LINUX = sys.platform == "linux"

#: Placeholder token in the admin compose template replaced by the frozen
#: immutable image ID of the release (apps.yaml documents this contract).
COMPOSE_IMAGE_TOKEN = "REPLACE_BY_DRAWBRIDGE"  # noqa: S105 - template marker, not a secret

_EMPTY_ENV_FILE = "/etc/drawbridge/compose/empty.env"
#: Default bounded preview size for spooled change steps.
_DEFAULT_SUMMARY_BYTES = 65536
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")

#: Dockerfile parser directives (``# key=value`` comment lines understood by
#: the dockerfile.v0 frontend).  ``syntax`` makes buildkitd pull a custom
#: frontend image — impossible on the air-gapped 910B target and an
#: uncontrolled supply-chain entry for the business repository; ``escape`` /
#: ``check`` are built-in frontend behaviour and never trigger a pull.
_DOCKERFILE_DIRECTIVE_RE = re.compile(r"^#\s*([A-Za-z]+)\s*=\s*(\S.*)$")
_DOCKERFILE_SCAN_MAX_BYTES = 1024 * 1024


def scan_dockerfile_directives(text: str) -> dict[str, str]:
    """Collect ``# key=value`` parser directives from Dockerfile text.

    Deliberately scans every comment line rather than only the leading
    directive block: a stray ``# syntax=`` must never reach buildctl at all
    (conservative by design — better a rejected build than a frontend pull
    attempt that fails misleadingly on the offline target).
    """
    directives: dict[str, str] = {}
    for line in text.splitlines():
        match = _DOCKERFILE_DIRECTIVE_RE.match(line)
        if match:
            directives.setdefault(match.group(1).lower(), match.group(2).strip())
    return directives


# ---------------------------------------------------------------------------
# Pure helpers — unit-testable on every platform
# ---------------------------------------------------------------------------


def compose_prefix(
    project_name: str, project_directory: str, compose_file: str
) -> list[str]:
    """The fixed ``C`` prefix from MVP spec §4 (never request-controlled)."""
    return [
        "compose",
        "--ansi",
        "never",
        "--project-name",
        project_name,
        "--project-directory",
        project_directory,
        "--env-file",
        _EMPTY_ENV_FILE,
        "-f",
        compose_file,
    ]


def compose_up_argv(prefix: Sequence[str]) -> list[str]:
    return [
        *prefix,
        "up",
        "--detach",
        "--no-build",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        "90",
    ]


def compose_stop_argv(prefix: Sequence[str]) -> list[str]:
    return [*prefix, "stop", "--timeout", "10"]


def buildctl_argv(
    *,
    buildkit_socket: str,
    context_dir: str,
    dockerfile_dir: str,
    dockerfile_basename: str,
    platform: str,
    image_tag: str,
    image_archive: str,
) -> list[str]:
    """Fixed BuildKit client invocation (MVP spec §5) — no request flags."""
    return [
        "--addr",
        buildkit_socket,
        "build",
        "--frontend",
        "dockerfile.v0",
        "--local",
        f"context={context_dir}",
        "--local",
        f"dockerfile={dockerfile_dir}",
        "--opt",
        f"filename={dockerfile_basename}",
        "--opt",
        f"platform={platform}",
        "--output",
        f"type=docker,name={image_tag},dest={image_archive}",
    ]


def unique_image_tag(app: str, environment: str, job_id: str) -> str:
    """Job-unique tag used only for import identification/retention."""
    return f"drawbridge-{app}-{environment}:job-{job_id[:12]}"


def test_container_create_argv(
    *, container_name: str, job_id: str, suite: TestSuiteConfig
) -> list[str]:
    """Fixed hardened test container creation (MVP spec §5)."""
    return [
        "create",
        "--name",
        container_name,
        "--label",
        f"io.drawbridge.job={job_id}",
        "--network",
        suite.network,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",  # noqa: S108 - docker tmpfs mount spec, not a path read
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "65532:65532",
        "--cpus",
        suite.resources.cpus,
        "--memory",
        suite.resources.memory,
        "--pids-limit",
        str(suite.resources.pids_limit),
        "--entrypoint",
        suite.entrypoint,
        suite.image_id,
    ]


def render_compose_text(template_text: str, image_id: str) -> str:
    """Replace the single image token with the immutable image ID."""
    count = template_text.count(COMPOSE_IMAGE_TOKEN)
    if count != 1:
        raise DrawbridgeError(
            f"compose template must contain exactly one {COMPOSE_IMAGE_TOKEN} "
            f"(found {count})",
            code=ErrorCode.CONFIG_INVALID,
        )
    if not _IMAGE_ID_RE.fullmatch(image_id):
        raise DrawbridgeError(
            f"refusing to render compose with non-immutable image id {image_id!r}",
            code=ErrorCode.INTERNAL,
        )
    return template_text.replace(COMPOSE_IMAGE_TOKEN, image_id)


def parse_wait_exit_code(text: str) -> int:
    """``docker wait`` prints the container exit code — parse it strictly."""
    stripped = text.strip()
    if not re.fullmatch(r"-?\d+", stripped):
        raise DrawbridgeError(
            f"docker wait returned an unusable exit code: {stripped[:40]!r}",
            code=ErrorCode.INTERNAL,
        )
    return int(stripped)


def parse_compose_ps_images(text: str) -> list[str]:
    """Parse ``compose ps --format json`` into the running image names."""
    images: list[str] = []
    stripped = text.strip()
    if not stripped:
        return images
    try:
        payload: Any = json.loads(stripped)
        rows = payload if isinstance(payload, list) else [payload]
    except json.JSONDecodeError:
        rows = []
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for row in rows:
        if isinstance(row, dict):
            image = row.get("Image")
            if isinstance(image, str) and image:
                images.append(image)
    return images


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class DeployRuntime:
    """Executes the internal deploy operations on the target server."""

    def __init__(
        self,
        *,
        config: DrawbridgeConfig,
        store: Store,
        process_manager: ProcessManager,
        log_dir: str,
    ) -> None:
        self.config = config
        self.store = store
        self.pm = process_manager
        self.log_dir = Path(log_dir)

        self._steps: dict[str, Any] = {
            "release_preflight": self.step_release_preflight,
            "source_snapshot": self.step_source_snapshot,
            "image_build": self.step_image_build,
            "image_import": self.step_image_import,
            "image_identify": self.step_image_identify,
            "compose_deploy": self.step_compose_deploy,
            "health_check": self.step_health_check,
            "test_suite": self.step_test_suite,
            "restore_previous": self.step_restore_previous,
            "stop_initial": self.step_stop_initial,
            "release_finalize": self.step_release_finalize,
        }

    async def __call__(
        self, operation: str, state: DeployState, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if not _RUNTIME_LINUX:
            raise DrawbridgeError(
                f"deploy step {operation!r} requires the Linux target host",
                code=ErrorCode.UNSUPPORTED,
            )
        step = self._steps.get(operation)
        if step is None:
            raise DrawbridgeError(
                f"unknown internal deploy operation {operation!r}",
                code=ErrorCode.UNKNOWN_OPERATION,
            )
        result: dict[str, Any] = await step(state, params or {})
        return result

    # -- shared plumbing ---------------------------------------------------

    def _env_cfg(self, state: DeployState) -> EnvironmentConfig:
        return self.config.environment(state.app, state.environment)

    def _jobs_dir(self, state: DeployState) -> Path:
        env = self._env_cfg(state)
        jobs = Path(env.deploy_root) / "jobs" / state.job.job_id
        jobs.mkdir(parents=True, exist_ok=True)
        return jobs

    def _log_path(self, job_id: str, name: str) -> str:
        return str(self.log_dir / job_id / f"{name}.log")

    def _git(self, state: DeployState) -> GitClient:
        app = self.config.apps[state.app]
        return GitClient(
            git_path=resolve_toolchain(self.config.main.toolchain, "git"),
            repo_path=app.git.repo_path,
            env=git_environment(self.config.main.profile_env),
            process_manager=self.pm,
        )

    async def _exec(
        self,
        *,
        operation: str,
        executable: str,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
        profile: ExecutionProfile,
        timeout: float,
        accepted: frozenset[int] = frozenset({0}),
        output_policy: OutputPolicyKind = OutputPolicyKind.TERMINATE,
        max_bytes: int = 65536,
        hard_limit: int | None = None,
        log_path: str | None = None,
        summary_bytes: int = _DEFAULT_SUMMARY_BYTES,
    ) -> ExecutionResult:
        spec = ExecutionSpec(
            operation=operation,
            executable=executable,
            argv=argv,
            cwd=cwd,
            env=env,
            profile=profile,
            timeout_seconds=timeout,
            output_policy=output_policy,
            max_output_bytes=max_bytes,
            hard_output_limit=hard_limit if hard_limit is not None else max_bytes,
            accepted_exit_codes=accepted,
            log_path=log_path,
            summary_bytes=summary_bytes,
        )
        return await self.pm.execute(spec)

    async def _docker(
        self,
        operation: str,
        argv: Sequence[str],
        state: DeployState | None = None,
        *,
        cwd: str | None = None,
        timeout: float = 60.0,
        output_policy: OutputPolicyKind = OutputPolicyKind.TERMINATE,
        max_bytes: int = 65536,
        hard_limit: int | None = None,
        log_path: str | None = None,
    ) -> ExecutionResult:
        env_cfg = self._env_cfg(state) if state is not None else None
        workdir = cwd or (env_cfg.deploy_root if env_cfg else "/")
        return await self._exec(
            operation=operation,
            executable=resolve_toolchain(self.config.main.toolchain, "docker"),
            argv=argv,
            cwd=workdir,
            env=docker_environment(self.config.main.profile_env),
            profile=ExecutionProfile.RUNTIME_MANAGE,
            timeout=timeout,
            output_policy=output_policy,
            max_bytes=max_bytes,
            hard_limit=hard_limit,
            log_path=log_path,
            summary_bytes=(
                max_bytes
                if output_policy == OutputPolicyKind.TERMINATE
                else _DEFAULT_SUMMARY_BYTES
            ),
        )

    def _render_compose(self, env_cfg: EnvironmentConfig, image_id: str) -> Path:
        template = Path(env_cfg.compose_file)
        try:
            template_text = template.read_text(encoding="utf-8")
        except OSError as exc:
            raise DrawbridgeError(
                f"cannot read compose template {template}: {exc}",
                code=ErrorCode.CONFIG_INVALID,
            ) from exc
        rendered = render_compose_text(template_text, image_id)
        output = Path(env_cfg.deploy_root) / "compose.rendered.yaml"
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = output.with_name(output.name + ".tmp")
        tmp.write_text(rendered, encoding="utf-8")
        os.replace(tmp, output)
        return output

    def _compose_prefix_for(
        self, env_cfg: EnvironmentConfig, compose_file: Path
    ) -> list[str]:
        return compose_prefix(
            env_cfg.project_name, env_cfg.deploy_root, str(compose_file)
        )

    def _suite_for(self, env_cfg: EnvironmentConfig, suite: str) -> TestSuiteConfig:
        config = env_cfg.test_runner.get(suite)
        if config is None:
            raise DrawbridgeError(
                f"test suite {suite!r} is not registered for this environment",
                code=ErrorCode.CONFIG_INVALID,
            )
        return config

    # -- workflow steps ------------------------------------------------------

    async def step_release_preflight(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        env_cfg = self._env_cfg(state)
        root = Path(env_cfg.deploy_root)
        root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(root)
        if usage.free < env_cfg.disk_budget_bytes:
            raise DrawbridgeError(
                f"disk budget exceeded: {usage.free} free < "
                f"{env_cfg.disk_budget_bytes} reserved",
                code=ErrorCode.DISK_BUDGET_EXCEEDED,
            )
        return {
            "disk_free_bytes": usage.free,
            "disk_budget_bytes": env_cfg.disk_budget_bytes,
            "baseline_release_id": (
                state.baseline.release_id if state.baseline else None
            ),
        }

    async def step_source_snapshot(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        sha = state.commit_sha
        if not sha:
            raise DrawbridgeError(
                "plan has no frozen commit SHA", code=ErrorCode.INTERNAL
            )
        git = self._git(state)
        resolved = await git.resolve_commit(sha)
        if resolved != sha:
            raise DrawbridgeError(
                f"frozen commit {sha} no longer resolves to itself",
                code=ErrorCode.SOURCE_INVALID,
            )
        jobs_dir = self._jobs_dir(state)
        archive = jobs_dir / "source.tar"
        await git.archive(sha, archive)
        dest = jobs_dir / "source"
        stats = await asyncio.to_thread(safe_extract_tar, archive, dest)
        return {
            "source_dir": str(dest),
            "archive_path": str(archive),
            "members": stats["members"],
            "expanded_bytes": stats["bytes"],
            "commit_sha": sha,
        }

    async def step_image_build(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        env_cfg = self._env_cfg(state)
        profile = self.config.build_profile(env_cfg.build_profile)
        if not state.source_dir:
            raise DrawbridgeError(
                "image_build ran without a source snapshot",
                code=ErrorCode.INTERNAL,
            )
        context_dir = Path(state.source_dir) / profile.context
        dockerfile = context_dir / profile.dockerfile_basename
        if not dockerfile.is_file():
            raise DrawbridgeError(
                f"dockerfile {profile.dockerfile_basename!r} missing in snapshot",
                code=ErrorCode.CONFIG_INVALID,
            )
        if dockerfile.stat().st_size > _DOCKERFILE_SCAN_MAX_BYTES:
            raise DrawbridgeError(
                f"dockerfile {profile.dockerfile_basename!r} exceeds the "
                f"{_DOCKERFILE_SCAN_MAX_BYTES} byte directive-scan budget",
                code=ErrorCode.CONFIG_INVALID,
            )
        try:
            dockerfile_text = dockerfile.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise DrawbridgeError(
                f"cannot read dockerfile {dockerfile}: {exc}",
                code=ErrorCode.CONFIG_INVALID,
            ) from exc
        directives = scan_dockerfile_directives(dockerfile_text)
        frontend = directives.get("syntax")
        if frontend is not None:
            # Rejected BEFORE any buildctl invocation — no side effects, no
            # misleading frontend-pull timeout on the offline target.
            raise DrawbridgeError(
                f"dockerfile declares '# syntax={frontend}': custom build "
                "frontends are not supported (offline target, controlled "
                "build contract); remove the directive so the default "
                "dockerfile.v0 frontend is used",
                code=ErrorCode.BUILD_UNSUPPORTED_FRONTEND,
            )
        tag = unique_image_tag(state.app, state.environment, state.job.job_id)
        archive = self._jobs_dir(state) / "image.tar"
        argv = buildctl_argv(
            buildkit_socket=env_cfg.buildkit_socket,
            context_dir=str(context_dir),
            dockerfile_dir=str(context_dir),
            dockerfile_basename=profile.dockerfile_basename,
            platform=profile.platform,
            image_tag=tag,
            image_archive=str(archive),
        )
        result = await self._exec(
            operation="image_build",
            executable=resolve_toolchain(self.config.main.toolchain, "buildctl"),
            argv=argv,
            cwd=str(context_dir),
            env=build_environment(
                ExecutionProfile.IMAGE_BUILD, self.config.main.profile_env
            ),
            profile=ExecutionProfile.IMAGE_BUILD,
            timeout=min(float(profile.timeout_seconds), 900.0),
            output_policy=OutputPolicyKind.SPOOL,
            max_bytes=self.config.main.output.query_summary_max_bytes,
            hard_limit=self.config.main.output.step_log_hard_limit_bytes,
            log_path=self._log_path(state.job.job_id, "build"),
        )
        if not result.accepted:
            raise DrawbridgeError(
                f"image build failed: {result.stderr_preview.strip()[:300]}",
                code=ErrorCode.BUILD_FAILED,
            )
        if not archive.is_file() or archive.stat().st_size == 0:
            raise DrawbridgeError(
                "buildctl reported success but produced no image archive",
                code=ErrorCode.BUILD_FAILED,
            )
        result: dict[str, Any] = {"image_tag": tag, "image_archive": str(archive)}
        # escape=/check= are built-in frontend behaviour: allowed and recorded.
        allowed_directives = {
            key: value for key, value in directives.items() if key in ("escape", "check")
        }
        if allowed_directives:
            result["dockerfile_directives"] = allowed_directives
        return result

    async def step_image_import(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        archive = self._jobs_dir(state) / "image.tar"
        if not archive.is_file():
            raise DrawbridgeError(
                "image_import ran before image_build produced an archive",
                code=ErrorCode.INTERNAL,
            )
        result = await self._docker(
            "image_import",
            ["image", "load", "--input", str(archive)],
            state,
            timeout=120.0,
            max_bytes=1024 * 1024,
        )
        if not result.accepted:
            raise DrawbridgeError(
                f"image import failed: {result.stderr_preview.strip()[:300]}",
                code=ErrorCode.BUILD_FAILED,
            )
        return {"loaded": True, "image_archive": str(archive)}

    async def step_image_identify(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        tag = state.image_tag
        if not tag:
            raise DrawbridgeError(
                "image_identify ran without a candidate tag",
                code=ErrorCode.INTERNAL,
            )
        result = await self._docker(
            "image_identify",
            ["image", "inspect", "--format", "{{.Id}}", tag],
            state,
            timeout=10.0,
            max_bytes=4096,
        )
        if not result.accepted:
            raise DrawbridgeError(
                f"cannot identify imported image {tag}: "
                f"{result.stderr_preview.strip()[:200]}",
                code=ErrorCode.BUILD_FAILED,
            )
        image_id = result.stdout_preview.strip()
        if not _IMAGE_ID_RE.fullmatch(image_id):
            raise DrawbridgeError(
                f"image inspect returned a non-immutable id: {image_id[:80]!r}",
                code=ErrorCode.BUILD_FAILED,
            )
        return {"image_id": image_id}

    async def step_compose_deploy(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        env_cfg = self._env_cfg(state)
        image_id = state.image_id
        if not image_id:
            raise DrawbridgeError(
                "compose_deploy ran without a verified image id",
                code=ErrorCode.INTERNAL,
            )
        rendered = self._render_compose(env_cfg, image_id)
        prefix = self._compose_prefix_for(env_cfg, rendered)
        result = await self._docker(
            "compose_deploy",
            compose_up_argv(prefix),
            state,
            timeout=120.0,
            output_policy=OutputPolicyKind.SPOOL,
            max_bytes=self.config.main.output.query_summary_max_bytes,
            hard_limit=self.config.main.output.step_log_hard_limit_bytes,
            log_path=self._log_path(state.job.job_id, "deploy"),
        )
        if not result.accepted:
            raise DrawbridgeError(
                f"compose up failed: {result.stderr_preview.strip()[:300]}",
                code=ErrorCode.VERIFY_FAILED,
            )
        return {"compose_file": str(rendered), "image_id": image_id}

    async def step_health_check(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        env_cfg = self._env_cfg(state)
        results = await probe_health_checks(env_cfg.health_checks)
        require_healthy(results)
        return {"checks": results}

    async def step_test_suite(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        env_cfg = self._env_cfg(state)
        suite_name = str(params.get("suite") or "smoke")
        suite = self._suite_for(env_cfg, suite_name)
        result = await self.run_test_container(
            app=state.app, environment=state.environment, suite=suite, job_id=state.job.job_id
        )
        if result["exit_code"] != 0:
            raise DrawbridgeError(
                f"test suite {suite_name!r} failed with exit code "
                f"{result['exit_code']}: {str(result.get('log_excerpt', ''))[:300]}",
                code=ErrorCode.VERIFY_FAILED,
            )
        return {"suite": suite_name, **result}

    async def step_restore_previous(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        baseline = state.baseline
        if baseline is None or not baseline.image_id:
            raise DrawbridgeError(
                "restore_previous ran without a baseline artifact",
                code=ErrorCode.ROLLBACK_FAILED,
            )
        env_cfg = self._env_cfg(state)
        rendered = self._render_compose(env_cfg, baseline.image_id)
        prefix = self._compose_prefix_for(env_cfg, rendered)
        result = await self._docker(
            "restore_previous",
            compose_up_argv(prefix),
            state,
            timeout=120.0,
            output_policy=OutputPolicyKind.SPOOL,
            max_bytes=self.config.main.output.query_summary_max_bytes,
            hard_limit=self.config.main.output.step_log_hard_limit_bytes,
            log_path=self._log_path(state.job.job_id, "restore"),
        )
        if not result.accepted:
            raise DrawbridgeError(
                f"restore compose up failed: {result.stderr_preview.strip()[:300]}",
                code=ErrorCode.ROLLBACK_FAILED,
            )
        checks = await probe_health_checks(env_cfg.health_checks)
        require_healthy(checks)
        suite_name = params.get("suite")
        test_detail: dict[str, Any] | None = None
        if suite_name:
            suite = self._suite_for(env_cfg, str(suite_name))
            test_detail = await self.run_test_container(
                app=state.app,
                environment=state.environment,
                suite=suite,
                job_id=state.job.job_id,
            )
        return {
            "restored_release_id": baseline.release_id,
            "restored_image_id": baseline.image_id,
            "compose_file": str(rendered),
            "checks": checks,
            "test": test_detail,
        }

    async def step_stop_initial(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        env_cfg = self._env_cfg(state)
        current = state.baseline
        if current is not None:
            raise DrawbridgeError(
                "stop_initial is only valid for the first deployment (no baseline)",
                code=ErrorCode.INTERNAL,
            )
        # Stop against the currently rendered compose file if one exists;
        # a fresh host has no rendered file yet — fall back to the template
        # project via an empty render is not possible, so require one.
        rendered = Path(env_cfg.deploy_root) / "compose.rendered.yaml"
        if not rendered.is_file():
            return {"stopped": False, "reason": "no rendered compose file"}
        prefix = self._compose_prefix_for(env_cfg, rendered)
        result = await self._docker(
            "stop_initial",
            compose_stop_argv(prefix),
            state,
            timeout=30.0,
        )
        if not result.accepted:
            raise DrawbridgeError(
                f"stop_initial failed: {result.stderr_preview.strip()[:200]}",
                code=ErrorCode.ROLLBACK_FAILED,
            )
        return {"stopped": True}

    async def step_release_finalize(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        image_id = state.image_id
        if not image_id:
            raise DrawbridgeError(
                "release_finalize ran without an image id", code=ErrorCode.INTERNAL
            )
        env_cfg = self._env_cfg(state)
        rendered = Path(env_cfg.deploy_root) / "compose.rendered.yaml"
        prefix = self._compose_prefix_for(env_cfg, rendered)
        result = await self._docker(
            "release_finalize",
            [*prefix, "ps", "--all", "--format", "json"],
            state,
            timeout=15.0,
            max_bytes=1024 * 1024,
        )
        if not result.accepted:
            raise DrawbridgeError(
                f"cannot verify running containers: {result.stderr_preview[:200]}",
                code=ErrorCode.DRIFT_DETECTED,
            )
        images = parse_compose_ps_images(result.stdout_preview)
        if not any(image == image_id for image in images):
            raise DrawbridgeError(
                f"drift detected: no container runs the release image {image_id} "
                f"(running: {sorted(images)[:5]})",
                code=ErrorCode.DRIFT_DETECTED,
            )
        return {"running_images": sorted(set(images))[:8], "verified_image": image_id}

    # -- reusable flows for job-kind handlers (ops_test / rollback) ---------

    async def run_test_container(
        self,
        *,
        app: str,
        environment: str,
        suite: TestSuiteConfig,
        job_id: str,
    ) -> dict[str, Any]:
        """Fixed create/start/wait/logs/stop/rm lifecycle (MVP spec §5)."""
        if not _RUNTIME_LINUX:
            raise DrawbridgeError(
                "test containers require the Linux target host",
                code=ErrorCode.UNSUPPORTED,
            )
        container = f"drawbridge-test-{job_id[:12]}"
        create = await self._docker(
            "test_create",
            test_container_create_argv(
                container_name=container, job_id=job_id, suite=suite
            ),
            None,
            cwd="/",
            timeout=30.0,
            max_bytes=4096,
        )
        if not create.accepted:
            raise DrawbridgeError(
                f"cannot create test container: {create.stderr_preview.strip()[:200]}",
                code=ErrorCode.VERIFY_FAILED,
            )
        warnings: list[str] = []
        try:
            start = await self._docker(
                "test_start", ["start", container], None, cwd="/", timeout=30.0,
                max_bytes=4096,
            )
            if not start.accepted:
                raise DrawbridgeError(
                    f"cannot start test container: {start.stderr_preview[:200]}",
                    code=ErrorCode.VERIFY_FAILED,
                )
            wait = await self._docker(
                "test_wait", ["wait", container], None, cwd="/",
                timeout=float(suite.timeout_seconds), max_bytes=4096,
            )
            if not wait.accepted:
                raise DrawbridgeError(
                    f"test container wait failed: {wait.stderr_preview[:200]}",
                    code=ErrorCode.VERIFY_FAILED,
                )
            exit_code = parse_wait_exit_code(wait.stdout_preview)
            logs = await self._docker(
                "test_logs",
                ["logs", "--timestamps", container],
                None,
                cwd="/",
                timeout=30.0,
                max_bytes=self.config.main.output.query_summary_max_bytes,
            )
        finally:
            stop = await self._docker(
                "test_stop", ["stop", "--time", "5", container], None, cwd="/",
                timeout=30.0, max_bytes=4096,
            )
            if not stop.accepted:
                warnings.append(f"stop failed: {stop.stderr_preview[:120]}")
            remove = await self._docker(
                "test_rm", ["rm", container], None, cwd="/", timeout=30.0, max_bytes=4096
            )
            if not remove.accepted:
                warnings.append(f"remove failed: {remove.stderr_preview[:120]}")
        return {
            "container": container,
            "exit_code": exit_code,
            "log_excerpt": logs.stdout_preview[:4000],
            "cleanup_warnings": warnings,
            "observed_at": time.time(),
        }

    async def restore_to_release(
        self, app: str, environment: str, release: ReleaseRecord
    ) -> dict[str, Any]:
        """Re-deploy a historical release's frozen artifacts (explicit rollback)."""
        if not _RUNTIME_LINUX:
            raise DrawbridgeError(
                "rollback requires the Linux target host", code=ErrorCode.UNSUPPORTED
            )
        if not release.image_id:
            raise DrawbridgeError(
                f"release {release.release_id} has no retained image artifact",
                code=ErrorCode.ROLLBACK_FAILED,
            )
        env_cfg = self.config.environment(app, environment)
        rendered = self._render_compose(env_cfg, release.image_id)
        prefix = self._compose_prefix_for(env_cfg, rendered)
        up = await self._docker(
            "rollback_compose_up",
            compose_up_argv(prefix),
            None,
            cwd=env_cfg.deploy_root,
            timeout=120.0,
            output_policy=OutputPolicyKind.SPOOL,
            max_bytes=self.config.main.output.query_summary_max_bytes,
            hard_limit=self.config.main.output.step_log_hard_limit_bytes,
        )
        if not up.accepted:
            raise DrawbridgeError(
                f"rollback compose up failed: {up.stderr_preview.strip()[:300]}",
                code=ErrorCode.ROLLBACK_FAILED,
            )
        checks = await probe_health_checks(env_cfg.health_checks)
        require_healthy(checks)
        return {"compose_file": str(rendered), "checks": checks}
