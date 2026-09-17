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
from datetime import UTC
from typing import Any

from drawbridge.config.models import DrawbridgeConfig
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
from drawbridge.state.records import JobRecord
from drawbridge.state.store import Store

Handler = Callable[["JobContext", JobRecord], Awaitable[dict[str, Any]]]

# Runtime platform, deliberately not an inline sys.platform expression:
# mypy on a dev host must not constant-fold away the Linux-only code paths.
_RUNTIME_LINUX = sys.platform == "linux"


@dataclass
class JobContext:
    config: DrawbridgeConfig
    store: Store
    process_manager: ProcessManager
    log_dir: str

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


def _unsupported(platform_note: str) -> DrawbridgeError:
    return DrawbridgeError(
        platform_note, code=ErrorCode.UNSUPPORTED
    )


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
    baseline = await ctx.store.get_current_release(app_id, job.environment)

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
    )
    return {
        "plan_id": plan.plan_id,
        "commit_sha": resolution.commit_sha,
        "workflow": workflow,
        "resolved_via": resolution.resolved_via,
        "mapped_ref": resolution.mapped_ref,
        "baseline_release_id": baseline.release_id if baseline else None,
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
    entries = await client.log(
        job.params["git_ref"], count=int(job.params.get("count", 20))
    )
    return {
        "commits": [
            {"sha": e.sha, "commit_time": e.commit_time, "subject": e.subject}
            for e in entries
        ],
        "observed_at": time.time(),
    }


async def run_host_metrics(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    return handle_host_metrics()


async def run_config_read(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    return handle_config_read(
        ctx.builtin_context(job.app, job.environment), job.params["file"]
    )


async def run_project_list(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    return handle_project_list(
        ctx.builtin_context(job.app, job.environment),
        job.params.get("subdir", "."),
        cursor=job.params.get("cursor"),
    )


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
        raise DrawbridgeError(
            f"ps failed: {result.stderr_preview[:200]}", code=ErrorCode.INTERNAL
        )
    lines = [line for line in result.stdout_preview.splitlines() if line.strip()]
    return {
        "head": lines[0] if lines else "",
        "processes": lines[1:51],
        "observed_at": time.time(),
    }


async def run_compose_status(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    """Fixed compose ps via the runtime_manage profile (Linux target)."""
    return await _compose_command(
        ctx, job, ["ps", "--all", "--format", "json"], timeout=15
    )


async def run_compose_logs(ctx: JobContext, job: JobRecord) -> dict[str, Any]:
    env_cfg = ctx.config.environment(job.app, job.environment)
    service = job.params.get("service")
    if service is not None and service not in env_cfg.services:
        raise DrawbridgeError(
            f"service {service!r} is not registered", code=ErrorCode.INVALID_PARAMETER
        )
    argv = [
        "logs",
        "--no-color",
        "--timestamps",
        "--tail",
        str(int(job.params.get("tail", 200))),
        "--since",
        _rfc3339(int(job.params.get("since_seconds", 300))),
    ]
    if service:
        argv.append(service)
    return await _compose_command(ctx, job, argv, timeout=15)


def _rfc3339(seconds_ago: int) -> str:
    from datetime import datetime, timedelta

    return (datetime.now(tz=UTC) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


async def _compose_command(
    ctx: JobContext, job: JobRecord, argv: list[str], *, timeout: float
) -> dict[str, Any]:
    if not _RUNTIME_LINUX:
        raise _unsupported("compose operations require the Linux target host")
    from drawbridge.config.models import ExecutionProfile, OutputPolicyKind
    from drawbridge.executor.spec import ExecutionSpec

    app_cfg = ctx.config.environment(job.app, job.environment)
    release = await ctx.store.get_current_release(job.app, job.environment)
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
        "/etc/drawbridge/compose/empty.env",
        "-f",
        compose_file,
    ]
    spec = ExecutionSpec(
        operation="compose",
        executable=resolve_toolchain(ctx.config.main.toolchain, "docker"),
        argv=[*prefix, *argv],
        cwd=release_dir or app_cfg.deploy_root,
        env=ctx.docker_env(),
        profile=ExecutionProfile.RUNTIME_MANAGE,
        timeout_seconds=timeout,
        output_policy=OutputPolicyKind.TERMINATE,
        max_output_bytes=ctx.config.main.output.log_result_max_bytes,
        hard_output_limit=ctx.config.main.output.log_result_max_bytes,
        accepted_exit_codes=frozenset({0}),
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
    if not _RUNTIME_LINUX:
        raise _unsupported("service restart requires the Linux target host")
    from drawbridge.config.models import ExecutionProfile, OutputPolicyKind
    from drawbridge.executor.spec import ExecutionSpec

    env_cfg = ctx.config.environment(job.app, job.environment)
    service = job.params["service"]
    if service not in env_cfg.restartable_services:
        raise DrawbridgeError(
            f"service {service!r} is not restartable", code=ErrorCode.INVALID_PARAMETER
        )
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
    release = await ctx.store.get_current_release(job.app, job.environment)
    return [
        "compose",
        "--ansi",
        "never",
        "--project-name",
        env_cfg.project_name,
        "--project-directory",
        (release.deploy_dir if release else env_cfg.deploy_root) or env_cfg.deploy_root,
        "--env-file",
        "/etc/drawbridge/compose/empty.env",
        "-f",
        env_cfg.compose_file,
    ]


async def _run_health_checks(ctx: JobContext, job: JobRecord) -> list[dict[str, Any]]:
    """Fixed health gates: consecutive successes inside the configured budget."""
    import asyncio
    from http.client import HTTPConnection
    from urllib.parse import urlparse

    env_cfg = ctx.config.environment(job.app, job.environment)
    results: list[dict[str, Any]] = []
    for hc in env_cfg.health_checks:
        parsed = urlparse(hc.url)
        if parsed.scheme != "http":
            raise DrawbridgeError(
                f"health check URL must be http (no TLS in MVP): {hc.url!r}",
                code=ErrorCode.CONFIG_INVALID,
            )

        def probe_once(parsed: Any = parsed, hc: Any = hc) -> int:
            conn = HTTPConnection(
                parsed.hostname, parsed.port or 80, timeout=hc.single_timeout_seconds
            )
            try:
                conn.request("GET", parsed.path or "/", headers={"Host": parsed.netloc})
                response = conn.getresponse()
                response.read()
                return response.status
            finally:
                conn.close()

        deadline = time.monotonic() + hc.timeout_seconds
        consecutive = 0
        last_status: int | None = None
        while time.monotonic() < deadline:
            try:
                last_status = await asyncio.to_thread(probe_once)
            except OSError:
                last_status = None
            if last_status == hc.expected_status:
                consecutive += 1
                if consecutive >= hc.consecutive_successes:
                    break
            else:
                consecutive = 0
            await asyncio.sleep(hc.interval_seconds)
        results.append(
            {
                "url": hc.url,
                "passed": consecutive >= hc.consecutive_successes,
                "last_status": last_status,
                "consecutive_successes": consecutive,
            }
        )
    return results


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
    """Redeploy the target historical release's frozen artifacts."""
    from drawbridge.runner.deploy import run_rollback

    return await run_rollback(ctx, job)


#: Registry: action name → handler.  Fixed in code; configuration selects
#: among these, it never supplies code (MVP spec §6).
HANDLERS: dict[str, Handler] = {
    "release_plan": run_release_plan,
    "git_status": run_git_status,
    "git_log": run_git_log,
    "host_metrics": run_host_metrics,
    "config_read": run_config_read,
    "project_list": run_project_list,
    "process_list": run_process_list,
    "compose_status": run_compose_status,
    "compose_logs": run_compose_logs,
    "npu_status": run_npu_status,
    "service_restart": run_service_restart,
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
        "process_list",
        "compose_status",
        "compose_logs",
        "npu_status",
    }
)

__all__ = ["DIAGNOSTIC_ACTIONS", "HANDLERS", "JobContext"]
