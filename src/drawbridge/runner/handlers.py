"""Runner job handlers: one registry, fixed behavior per action.

Every handler receives a :class:`JobContext` (config, store, per-target
clients) and the job's validated parameter dict, and returns a JSON-safe
result dict.  Handlers never accept raw argv from requests: diagnostics map
to registered operations, mutations to built-in flows (MVP spec §4-§5).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from datetime import UTC
from pathlib import Path
from typing import Any

from drawbridge.config.compose_template import read_compose_template
from drawbridge.config.models import DrawbridgeConfig, excludes_simulated_releases
from drawbridge.errors import DrawbridgeError, ErrorCode
from drawbridge.executor.process import ProcessManager
from drawbridge.executor.spec import (
    build_environment,
    docker_environment,
    git_environment,
    resolve_toolchain,
)
from drawbridge.gitops import GitClient
from drawbridge.runner.builtin import (
    BuiltinContext,
    handle_config_read,
    handle_host_metrics,
    handle_project_list,
)
from drawbridge.runner.health import probe_health_checks
from drawbridge.runner.logpage import (
    LogCursorCache,
    UnknownCursorError,
    paginate_lines,
)
from drawbridge.runner.runtime import compose_env_file
from drawbridge.state.records import JobRecord, ReleaseRecord, StagedRelease
from drawbridge.state.store import Store

Handler = Callable[["JobContext", JobRecord], Awaitable[dict[str, Any]]]

# Runtime platform, deliberately not an inline sys.platform expression:
# mypy on a dev host must not constant-fold away the Linux-only code paths.
_RUNTIME_LINUX = sys.platform == "linux"

#: Process-wide snapshot cache for log cursors (bound to one fetch, 10 min).
_LOG_CACHE = LogCursorCache()


@dataclass
class JobContext:
    config: DrawbridgeConfig
    store: Store
    process_manager: ProcessManager
    log_dir: str
    #: Releases staged by a handler (release_rollback) for the atomic
    #: success completion (plan D4): written together with the job's
    #: terminal transition by the Runner loop; discarded on failure.
    staged_releases: list[StagedRelease] = dataclasses_field(default_factory=list)

    def git_environment(self) -> dict[str, str]:
        return git_environment(
            self.config.main.profile_env,
            platform=None,
        )

    def git_client(self, app_id: str) -> GitClient:
        app = self.config.apps[app_id]
        return GitClient(
            git_path=resolve_toolchain(self.config.main.toolchain, "git"),
            repo_path=app.git.repo_path,
            env=self.git_environment(),
            process_manager=self.process_manager,
        )

    def builtin_context(self, app_id: str, environment: str) -> BuiltinContext:
        env = self.config.environment(app_id, environment)
        diagnostics = env.diagnostics
        return BuiltinContext(
            app_id=app_id,
            environment=environment,
            diagnostics_root=diagnostics.root if diagnostics else None,
            config_files=dict(diagnostics.config_files) if diagnostics else {},
        )

    def docker_env(self) -> dict[str, str]:
        return docker_environment(self.config.main.profile_env, platform=None)

    def host_env(self) -> dict[str, str]:
        from drawbridge.config.models import ExecutionProfile

        return build_environment(
            ExecutionProfile.HOST_OBSERVE,
            self.config.main.profile_env,
            platform=None,
        )

    def runtime(self) -> Any:
        """The runtime adapter stack (compose or simulation per target)."""
        from drawbridge.runner.simulation import build_runtime

        return build_runtime(
            config=self.config,
            store=self.store,
            process_manager=self.process_manager,
            log_dir=self.log_dir,
        )

    def runtime_mode(self, app_id: str, environment: str) -> str:
        """Configured runtime adapter for one target (compose|simulation)."""
        return self.config.environment(app_id, environment).runtime


def _unsupported(platform_note: str) -> DrawbridgeError:
    return DrawbridgeError(platform_note, code=ErrorCode.UNSUPPORTED)


async def run_release_plan(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    from drawbridge.config.models import SourceMode
    from drawbridge.runner.plan import RefResolver

    app_id = job.app
    app_cfg = ctx.config.apps[app_id]
    git_client = ctx.git_client(app_id)
    resolver = RefResolver(git=git_client, git_config=app_cfg.git)
    resolution = await resolver.resolve(
        source_mode=SourceMode(job.params["source_mode"]),
        git_ref=job.params["git_ref"],
    )
    workflow = job.params.get("workflow", "deploy_verify")
    env_cfg = ctx.config.environment(app_id, job.environment)
    baseline = await ctx.store.get_current_release(
        app_id,
        job.environment,
        exclude_simulated=excludes_simulated_releases(env_cfg.runtime),
    )
    # Template fingerprint frozen into the plan (D3): structure is re-validated
    # here and at apply time; NULL-digest (legacy) plans are always stale.
    template = read_compose_template(env_cfg.compose_file, list(env_cfg.services))

    status_summary: dict[str, Any] | None = None
    try:
        status = await git_client.status_porcelain()
        status_summary = {"dirty_entries": len(status)}
    except Exception:
        status_summary = None

    plan = await ctx.store.create_plan(
        app=app_id,
        environment=job.environment,
        workflow=workflow,
        source_mode=job.params["source_mode"],
        git_ref=job.params["git_ref"],
        commit_sha=resolution.commit_sha,
        config_digest=ctx.config.digest,
        baseline_release_id=baseline.release_id if baseline else None,
        ttl_seconds=900,
        params={
            "resolved_via": resolution.resolved_via,
            "mapped_ref": resolution.mapped_ref,
            "workspace_status": status_summary,
        },
        request_id=job.request_id,
        compose_template_digest=template.digest,
    )
    await ctx.store.append_event(
        "plan_created",
        job_id=job.job_id,
        app=app_id,
        environment=job.environment,
        request_id=job.request_id,
        agent_id=job.agent_id,
        detail={
            "plan_id": plan.plan_id,
            "git_ref": job.params["git_ref"],
            "commit_sha": resolution.commit_sha,
            "source_mode": job.params["source_mode"],
            "baseline_release_id": plan.baseline_release_id,
        },
    )
    return {
        "plan_id": plan.plan_id,
        "commit_sha": resolution.commit_sha,
        "workflow": workflow,
        "resolved_via": resolution.resolved_via,
        "mapped_ref": resolution.mapped_ref,
        "baseline_release_id": baseline.release_id if baseline else None,
        "compose_services": template.services,
        "expires_at": plan.expires_at,
        "plan_status": plan.status,
    }


async def run_git_status(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    client = ctx.git_client(job.app)
    entries = await client.status_porcelain()
    return {
        "workspace": "clean" if not entries else "dirty",
        "entries": entries[:50],
        "observed_at": time.time(),
        "note": "this reflects the server workspace, not deployed source",
    }


async def run_git_log(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    client = ctx.git_client(job.app)
    entries = await client.log(job.params["git_ref"], count=int(job.params.get("count", 20)))
    return {
        "commits": [
            {"sha": e.sha, "commit_time": e.commit_time, "subject": e.subject} for e in entries
        ],
        "observed_at": time.time(),
    }


async def run_host_metrics(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    return handle_host_metrics()


async def run_config_read(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    return handle_config_read(ctx.builtin_context(job.app, job.environment), job.params["file"])


async def run_project_list(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    # Defense in depth (invariant 2): the Gateway validated these bounds;
    # the Runner re-checks so a malformed params dict cannot reach the scan.
    limit = _bounded_int(job.params.get("limit", 100), 1, 200, "limit")
    return handle_project_list(
        ctx.builtin_context(job.app, job.environment),
        job.params.get("subdir", "."),
        cursor=job.params.get("cursor"),
        limit=limit,
    )


async def run_config_validate(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    from drawbridge.runner.builtin import handle_config_validate

    return handle_config_validate(ctx.builtin_context(job.app, job.environment), job.params["file"])


async def run_check_project_config(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    """Admin-fixed diagnostic script; bash --noprofile --norc, no request argv."""
    if not _RUNTIME_LINUX:
        raise _unsupported("check_project_config requires the Linux target host")
    from drawbridge.config.models import ExecutionProfile, OutputPolicyKind
    from drawbridge.executor.spec import ExecutionSpec

    env_cfg = ctx.config.environment(job.app, job.environment)
    diagnostics = env_cfg.diagnostics
    cwd = diagnostics.root if diagnostics else env_cfg.deploy_root
    # Resolved from paths.config_dir (plan D13): anchored deployments keep
    # working without an /etc/drawbridge tree; standard deployments resolve
    # to the historical literal.
    script = (
        Path(ctx.config.main.paths.config_dir) / "scripts" / "check_project_config.sh"
    ).as_posix()
    spec = ExecutionSpec(
        operation="check_project_config",
        executable="/bin/bash",
        argv=("--noprofile", "--norc", script),
        cwd=cwd,
        env=build_environment(ExecutionProfile.PROJECT_DIAGNOSTIC, ctx.config.main.profile_env),
        profile=ExecutionProfile.PROJECT_DIAGNOSTIC,
        timeout_seconds=15,
        output_policy=OutputPolicyKind.TERMINATE,
        max_output_bytes=ctx.config.main.output.query_summary_max_bytes,
        hard_output_limit=ctx.config.main.output.query_summary_max_bytes,
        accepted_exit_codes=frozenset({0}),
    )
    result = await ctx.process_manager.execute(spec)
    return {
        "exit_code": result.exit_code,
        "accepted": result.accepted,
        "termination_reason": result.termination_reason,
        "stdout_preview": result.stdout_preview[:4000],
        "stderr_preview": result.stderr_preview[:2000],
        "observed_at": time.time(),
    }


async def run_process_list(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    if not _RUNTIME_LINUX:
        raise _unsupported("process_list requires the Linux target host")
    from drawbridge.config.models import ExecutionProfile, OutputPolicyKind
    from drawbridge.executor.spec import ExecutionSpec

    spec = ExecutionSpec(
        operation="process_list",
        executable=resolve_toolchain(ctx.config.main.toolchain, "ps"),
        argv=("-eo", "pid,ppid,user,comm,pcpu,pmem", "--sort=-pcpu"),
        cwd="/",
        env=build_environment(ExecutionProfile.HOST_OBSERVE, ctx.config.main.profile_env),
        profile=ExecutionProfile.HOST_OBSERVE,
        timeout_seconds=10,
        output_policy=OutputPolicyKind.TERMINATE,
        max_output_bytes=ctx.config.main.output.query_summary_max_bytes,
        hard_output_limit=ctx.config.main.output.query_summary_max_bytes,
        accepted_exit_codes=frozenset({0}),
    )
    result = await ctx.process_manager.execute(spec)
    if not result.accepted:
        raise DrawbridgeError(f"ps failed: {result.stderr_preview[:200]}", code=ErrorCode.INTERNAL)
    lines = [line for line in result.stdout_preview.splitlines() if line.strip()]
    return {
        "head": lines[0] if lines else "",
        "processes": lines[1:51],
        "observed_at": time.time(),
    }


async def run_compose_status(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    """Fixed compose ps via the runtime_manage profile (Linux target)."""
    if ctx.runtime_mode(job.app, job.environment) == "simulation":
        return await _simulation_compose_status(ctx, job)
    return await _compose_command(ctx, job, ["ps", "--all", "--format", "json"], timeout=15)


async def _simulation_compose_status(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    """Bounded service view of a simulated release (whitelisted fields)."""
    env_cfg = ctx.config.environment(job.app, job.environment)
    release = await ctx.store.get_current_release(job.app, job.environment)
    rows = [
        {
            "service": service,
            "state": "running" if release is not None else "created",
            "health": "simulated" if release is not None else None,
            "image": release.image_id if release is not None else None,
        }
        for service in env_cfg.services
    ]
    return {"services": rows, "simulated": True, "observed_at": time.time()}


async def run_compose_logs(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    """Bounded snapshot fetch → literal filter → cursor pagination."""
    env_cfg = ctx.config.environment(job.app, job.environment)
    service = job.params.get("service")
    if service is not None and service not in env_cfg.services:
        raise DrawbridgeError(
            f"service {service!r} is not registered", code=ErrorCode.INVALID_PARAMETER
        )
    # Defense in depth: the Gateway validated these; the Runner re-checks
    # bounds so a malformed params dict can never reach compose argv.
    tail = _bounded_int(job.params.get("tail", 200), 1, 1000, "tail")
    since = _bounded_int(job.params.get("since_seconds", 300), 1, 86400, "since_seconds")
    limit = _bounded_int(job.params.get("limit", 100), 1, 200, "limit")
    query = job.params.get("query", "")
    if not isinstance(query, str) or len(query) > 128 or _has_control_chars(query):
        raise DrawbridgeError("query is invalid", code=ErrorCode.INVALID_PARAMETER)
    cursor = job.params.get("cursor")

    observed_at = time.time()
    if cursor is not None:
        if not isinstance(cursor, str):
            raise DrawbridgeError("cursor must be a string", code=ErrorCode.INVALID_PARAMETER)
        try:
            snapshot_id, snapshot, offset = _LOG_CACHE.resolve(cursor)
        except UnknownCursorError as exc:
            raise DrawbridgeError(str(exc), code=ErrorCode.INVALID_PARAMETER) from exc
        page = paginate_lines(
            snapshot.lines,
            query=query,
            offset=offset,
            limit=limit,
            max_bytes=ctx.config.main.output.log_result_max_bytes,
        )
        return _log_page(page, snapshot_id, False, observed_at)

    if ctx.runtime_mode(job.app, job.environment) == "simulation":
        lines = _simulation_log_lines(ctx, job, tail)
        fetch_truncated = False
    else:
        argv = [
            "logs",
            "--no-color",
            "--timestamps",
            "--tail",
            str(tail),
            "--since",
            _rfc3339(since),
        ]
        if service:
            argv.append(service)
        fetched = await _compose_command(ctx, job, argv, timeout=15, parse_full_output=True)
        lines = [line for line in fetched["stdout_preview"].splitlines() if line.strip()]
        fetch_truncated = bool(fetched["truncated"])
    return _paged_log_result(ctx, lines, query, limit, fetch_truncated, observed_at)


def _simulation_log_lines(ctx: JobContext, job: JobRecord, tail: int) -> list[str]:
    """Bounded read of the synthetic application log of a simulated release."""
    from drawbridge.runner.simulation import SIMULATION_LOG_NAME

    env_cfg = ctx.config.environment(job.app, job.environment)
    log_file = Path(env_cfg.deploy_root) / SIMULATION_LOG_NAME
    if not log_file.is_file():
        return []
    max_bytes = ctx.config.main.output.log_result_max_bytes
    try:
        raw = log_file.read_bytes()
    except OSError:
        return []
    truncated = len(raw) > max_bytes
    text = (
        raw[-max_bytes:].decode("utf-8", errors="replace")
        if truncated
        else raw.decode("utf-8", errors="replace")
    )
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-tail:]


def _paged_log_result(
    ctx: JobContext,
    lines: list[str],
    query: str,
    limit: int,
    fetch_truncated: bool,
    observed_at: float,
) -> dict[str, Any]:
    snapshot_id = _LOG_CACHE.create(lines)
    page = paginate_lines(
        lines,
        query=query,
        offset=0,
        limit=limit,
        max_bytes=ctx.config.main.output.log_result_max_bytes,
    )
    return _log_page(page, snapshot_id, fetch_truncated, observed_at)


def _log_page(
    page: dict[str, object],
    snapshot_id: str,
    fetch_truncated: bool,
    observed_at: float,
) -> dict[str, Any]:
    next_offset = page["next_offset"]
    return {
        "lines": page["lines"],
        "matched_total": page["matched_total"],
        "snapshot_total": page["snapshot_total"],
        "truncated": page["truncated"],
        "fetch_truncated": fetch_truncated,
        "next_cursor": (f"{snapshot_id}:{next_offset}" if next_offset is not None else None),
        "observed_at": observed_at,
    }


def _bounded_int(value: Any, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DrawbridgeError(f"{name} must be an integer", code=ErrorCode.INVALID_PARAMETER)
    if not (minimum <= value <= maximum):
        raise DrawbridgeError(
            f"{name} must be between {minimum} and {maximum}",
            code=ErrorCode.INVALID_PARAMETER,
        )
    return value


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _rfc3339(seconds_ago: int) -> str:
    from datetime import datetime, timedelta

    return (datetime.now(tz=UTC) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _compose_command(
    ctx: JobContext,
    job: JobRecord,
    argv: list[str],
    *,
    timeout: float,
    parse_full_output: bool = False,
) -> dict[str, Any]:
    if not _RUNTIME_LINUX:
        raise _unsupported("compose operations require the Linux target host")
    from drawbridge.config.models import ExecutionProfile, OutputPolicyKind
    from drawbridge.executor.spec import ExecutionSpec

    app_cfg = ctx.config.environment(job.app, job.environment)
    release = await ctx.store.get_current_release(
        job.app,
        job.environment,
        exclude_simulated=excludes_simulated_releases(app_cfg.runtime),
    )
    release_dir = release.deploy_dir if release else app_cfg.deploy_root
    compose_file = app_cfg.compose_file
    prefix = [
        "compose",
        "--ansi",
        "never",
        "--project-name",
        app_cfg.project_name,
        "--project-directory",
        release_dir or app_cfg.deploy_root,
        "--env-file",
        compose_env_file(ctx.config.main.paths.config_dir),
        "-f",
        compose_file,
    ]
    max_bytes = ctx.config.main.output.log_result_max_bytes
    spec = ExecutionSpec(
        operation="compose",
        executable=resolve_toolchain(ctx.config.main.toolchain, "docker"),
        argv=[*prefix, *argv],
        cwd=release_dir or app_cfg.deploy_root,
        env=ctx.docker_env(),
        profile=ExecutionProfile.RUNTIME_MANAGE,
        timeout_seconds=timeout,
        output_policy=OutputPolicyKind.TERMINATE,
        max_output_bytes=max_bytes,
        hard_output_limit=max_bytes,
        accepted_exit_codes=frozenset({0}),
        summary_bytes=max_bytes if parse_full_output else 65536,
    )
    result = await ctx.process_manager.execute(spec)
    if not result.accepted:
        raise DrawbridgeError(
            f"compose {' '.join(argv[:1])} failed: {result.stderr_preview[:200]}",
            code=ErrorCode.INTERNAL,
        )
    return {
        "stdout_preview": result.stdout_preview,
        "truncated": result.truncated,
        "observed_at": time.time(),
    }


async def run_service_restart(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    """Restart one registered service, then run its health gate."""
    env_cfg = ctx.config.environment(job.app, job.environment)
    service = job.params["service"]
    if service not in env_cfg.restartable_services:
        raise DrawbridgeError(
            f"service {service!r} is not restartable", code=ErrorCode.INVALID_PARAMETER
        )
    if ctx.runtime_mode(job.app, job.environment) == "simulation":
        from drawbridge.runner.simulation import simulation_checks

        health = simulation_checks(env_cfg)
        return {
            "restarted": service,
            "health": health,
            "simulated": True,
            "observed_at": time.time(),
        }
    if not _RUNTIME_LINUX:
        raise _unsupported("service restart requires the Linux target host")
    from drawbridge.config.models import ExecutionProfile, OutputPolicyKind
    from drawbridge.executor.spec import ExecutionSpec

    prefix = await _compose_prefix(ctx, job)
    spec = ExecutionSpec(
        operation="service_restart",
        executable=resolve_toolchain(ctx.config.main.toolchain, "docker"),
        argv=[*prefix, "restart", "--timeout", "10", service],
        cwd=env_cfg.deploy_root,
        env=ctx.docker_env(),
        profile=ExecutionProfile.RUNTIME_MANAGE,
        timeout_seconds=60,
        output_policy=OutputPolicyKind.SPOOL,
        max_output_bytes=ctx.config.main.output.query_summary_max_bytes,
        hard_output_limit=ctx.config.main.output.step_log_hard_limit_bytes,
        accepted_exit_codes=frozenset({0}),
        log_path=f"{ctx.log_dir}/{job.job_id}/restart.log",
    )
    result = await ctx.process_manager.execute(spec)
    if not result.accepted:
        raise DrawbridgeError(
            f"restart failed: {result.stderr_preview[:200]}", code=ErrorCode.VERIFY_FAILED
        )
    health = await _run_health_checks(ctx, job)
    return {"restarted": service, "health": health, "observed_at": time.time()}


async def _compose_prefix(ctx: JobContext, job: JobRecord) -> list[str]:
    env_cfg = ctx.config.environment(job.app, job.environment)
    release = await ctx.store.get_current_release(
        job.app,
        job.environment,
        exclude_simulated=excludes_simulated_releases(env_cfg.runtime),
    )
    return [
        "compose",
        "--ansi",
        "never",
        "--project-name",
        env_cfg.project_name,
        "--project-directory",
        (release.deploy_dir if release else env_cfg.deploy_root) or env_cfg.deploy_root,
        "--env-file",
        compose_env_file(ctx.config.main.paths.config_dir),
        "-f",
        env_cfg.compose_file,
    ]


async def _run_health_checks(ctx: JobContext, job: JobRecord) -> list[dict[str, Any]]:
    """Fixed health gates: consecutive successes inside the configured budget."""
    env_cfg = ctx.config.environment(job.app, job.environment)
    return await probe_health_checks(env_cfg.health_checks)


async def run_npu_status(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    app_cfg = ctx.config.apps.get(job.app)
    npu = app_cfg.npu if app_cfg else None
    if npu is None or not npu.enabled:
        raise DrawbridgeError("NPU query is not enabled", code=ErrorCode.UNSUPPORTED)
    if not _RUNTIME_LINUX:
        raise _unsupported("NPU query requires the Linux target host")
    from drawbridge.config.models import ExecutionProfile, OutputPolicyKind
    from drawbridge.executor.spec import ExecutionSpec

    spec = ExecutionSpec(
        operation="npu_status",
        executable=npu.executable,
        argv=tuple(npu.argv),
        cwd="/",
        env=build_environment(ExecutionProfile.HOST_OBSERVE, ctx.config.main.profile_env),
        profile=ExecutionProfile.HOST_OBSERVE,
        timeout_seconds=npu.timeout_seconds,
        output_policy=OutputPolicyKind.TERMINATE,
        max_output_bytes=ctx.config.main.output.query_summary_max_bytes,
        hard_output_limit=ctx.config.main.output.query_summary_max_bytes,
        accepted_exit_codes=frozenset({0}),
    )
    result = await ctx.process_manager.execute(spec)
    if not result.accepted:
        raise DrawbridgeError(
            f"npu query failed: {result.stderr_preview[:200]}", code=ErrorCode.INTERNAL
        )
    return {"preview": result.stdout_preview, "observed_at": time.time()}


async def run_release_rollback(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    """Redeploy a historical release's frozen artifacts as a new release."""
    runtime = ctx.runtime()
    env_runtime = ctx.runtime_mode(job.app, job.environment)
    target = await ctx.store.get_release(str(job.params["release_id"]))
    if target.app != job.app or target.environment != job.environment:
        raise DrawbridgeError(
            "release belongs to a different target", code=ErrorCode.INVALID_PARAMETER
        )
    if target.simulated and env_runtime != "simulation":
        raise DrawbridgeError(
            "release was produced by the simulation runtime and cannot be "
            "restored on a compose target (its image does not exist in the "
            "Engine); pick a compose-era release or re-plan a fresh deploy",
            code=ErrorCode.INVALID_PARAMETER,
        )
    if target.status not in ("succeeded", "rollback", "superseded"):
        raise DrawbridgeError(
            f"release {target.release_id} cannot be rolled back to (status {target.status})",
            code=ErrorCode.INVALID_PARAMETER,
        )
    restore = await runtime.restore_to_release(job.app, job.environment, target)

    from drawbridge.state.store import new_id

    now = time.time()
    release = ReleaseRecord(
        release_id=new_id(),
        app=job.app,
        environment=job.environment,
        plan_id=None,
        job_id=job.job_id,
        commit_sha=target.commit_sha,
        image_id=target.image_id,
        image_tag=target.image_tag,
        config_digest=ctx.config.digest,
        simulated=env_runtime == "simulation",
        status="rollback",
        rollback_of=target.release_id,
        compose_path=restore.get("compose_file"),
        deploy_dir=ctx.config.environment(job.app, job.environment).deploy_root,
        evidence={
            "reason": job.params.get("reason"),
            "restored_from": target.release_id,
            "checks": restore.get("checks"),
            "verified_at": now,
        },
        created_at=now,
        verified_at=now,
    )
    # Staged, not written (plan D4): the release row, its release_recorded
    # event and the job's terminal transition commit together atomically.
    ctx.staged_releases.append(
        StagedRelease(
            release=release,
            artifact=None,
            event_detail={
                "status": "rollback",
                "rollback_of": target.release_id,
                "image_id": target.image_id,
            },
        )
    )
    return {
        "release_id": release.release_id,
        "rollback_of": target.release_id,
        "image_id": target.image_id,
        "commit_sha": target.commit_sha,
        "checks": restore.get("checks"),
        "observed_at": now,
    }


async def run_test_suite(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    """Run one registered suite against the currently deployed release."""
    runtime = ctx.runtime()
    release = await ctx.store.get_release(str(job.params["release_id"]))
    current = await ctx.store.get_current_release(
        job.app,
        job.environment,
        exclude_simulated=excludes_simulated_releases(
            ctx.runtime_mode(job.app, job.environment)
        ),
    )
    if current is None or current.release_id != release.release_id:
        raise DrawbridgeError(
            "tests must target the currently deployed release",
            code=ErrorCode.STALE_PLAN,
        )
    env_cfg = ctx.config.environment(job.app, job.environment)
    suite_name = str(job.params["suite"])
    suite = env_cfg.test_runner.get(suite_name)
    if suite is None:
        raise DrawbridgeError(
            f"test suite {suite_name!r} is not registered",
            code=ErrorCode.CONFIG_INVALID,
        )
    result = await runtime.run_test_container(
        app=job.app, environment=job.environment, suite=suite, job_id=job.job_id
    )
    passed = result["exit_code"] == 0
    payload: dict[str, Any] = {
        "release_id": release.release_id,
        "image_id": release.image_id,
        "suite": suite_name,
        "passed": passed,
        **result,
    }
    if not passed:
        raise DrawbridgeError(
            "test suite failed: " + str(result.get("log_excerpt", ""))[:300],
            code=ErrorCode.VERIFY_FAILED,
            details=payload,
        )
    return payload


#: Registry: action name → handler.  Fixed in code; configuration selects
#: among these, it never supplies code (MVP spec §6).
HANDLERS: dict[str, Handler] = {
    "release_plan": run_release_plan,
    "git_status": run_git_status,
    "git_log": run_git_log,
    "host_metrics": run_host_metrics,
    "config_read": run_config_read,
    "project_list": run_project_list,
    "config_validate": run_config_validate,
    "check_project_config": run_check_project_config,
    "process_list": run_process_list,
    "compose_status": run_compose_status,
    "compose_logs": run_compose_logs,
    "npu_status": run_npu_status,
    "service_restart": run_service_restart,
    "test_suite": run_test_suite,
    "release_rollback": run_release_rollback,
}

#: Actions that must never be dispatched to the diagnostic channel.
DIAGNOSTIC_ACTIONS = frozenset(
    {
        "release_plan",
        "git_status",
        "git_log",
        "host_metrics",
        "config_read",
        "project_list",
        "config_validate",
        "check_project_config",
        "process_list",
        "compose_status",
        "compose_logs",
        "npu_status",
    }
)

__all__ = ["DIAGNOSTIC_ACTIONS", "HANDLERS", "JobContext"]
