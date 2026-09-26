"""drawbridge-simulate: command-line communication test without systemd.

Runs the complete Gateway↔Runner conversation — the exact validation,
admission, queue, execution and polling path an MCP client drives — inside
one process and without systemd, Docker or BuildKit:

    ops_catalog → ops_status → ops_logs → git_status → config_read
    → ops_release_plan → ops_release_apply → (runner consumes the queue)
    → ops_release_status → second deploy → ops_test → ops_service_restart
    → ops_release_rollback → final status

Two modes:

* ``--fixture`` (default when ``--config-dir`` is absent): builds an
  isolated demo application (git repository) plus a matching simulation
  config bundle under ``--workdir``, then runs the scenario against it.
  Needs only Python and git — designed for the Windows development hosts.
* ``--config-dir DIR``: run against an existing bundle; the selected
  app/environment must declare ``runtime: simulation``.

The Gateway service and the Runner share the same SQLite database exactly
like the two systemd processes do on the 910B server; the runner loop runs
as a concurrent task claiming jobs while the gateway polls.  The final JSON
report goes to stdout, progress logs to stderr.  Exit code 0 means every
critical step reached its expected outcome.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from drawbridge.config.loader import load_config_from_dir
from drawbridge.errors import DrawbridgeError
from drawbridge.gateway.service import GatewayService
from drawbridge.logsetup import configure_logging, get_logger
from drawbridge.runner.loop import Runner
from drawbridge.state.db import Database
from drawbridge.state.records import JobStatus
from drawbridge.state.store import Store

#: Embedded minimal demo application (mirrors examples/demo-app): a tiny
#: healthz HTTP service plus the JSON config used by the config_read step.
_DEMO_APP_PY = """\
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

with open("/app/config/app.json", encoding="utf-8") as fh:
    CONFIG = json.load(fh)

READY = False


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        global READY
        if self.path == "/healthz":
            READY = True
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"name": CONFIG["name"], "ready": READY}).encode()
            )
            return
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""

_DEMO_APP_JSON = """\
{
  "name": "drawbridge-demo",
  "listen_port": 8080,
  "log_level": "info",
  "admin_token": "demo-secret-not-a-real-credential"
}
"""

_DEMO_DOCKERFILE = """\
FROM python:3.12-slim
WORKDIR /app
COPY app.py /app/app.py
COPY config/app.json /app/config/app.json
USER 65532:65532
EXPOSE 8080
CMD ["python3", "/app/app.py"]
"""

#: Compose template with the single image token, mirroring
#: configs/compose/demo.staging.yaml (admin-side contract).
_DEMO_COMPOSE = """\
services:
  api:
    image: REPLACE_BY_DRAWBRIDGE
    ports:
      - "18080:8080"
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    user: "65532:65532"
    restart: unless-stopped
"""


def _posix(value: Path) -> str:
    """YAML-friendly absolute path (forward slashes also on Windows)."""
    return value.resolve().as_posix()


def _write_demo_app(repo_dir: Path) -> None:
    (repo_dir / "config").mkdir(parents=True, exist_ok=True)
    (repo_dir / "app.py").write_text(_DEMO_APP_PY, encoding="utf-8")
    (repo_dir / "config" / "app.json").write_text(_DEMO_APP_JSON, encoding="utf-8")
    (repo_dir / "Dockerfile").write_text(_DEMO_DOCKERFILE, encoding="utf-8")
    # Pin line-ending handling so `git status` stays clean regardless of the
    # developing machine's core.autocrlf setting.
    (repo_dir / ".gitattributes").write_text("* -text\n", encoding="utf-8")


def _git_init_demo(repo_dir: Path, git_path: str) -> None:
    def git(*argv: str) -> None:
        subprocess.run(
            [
                git_path,
                "-c",
                "user.name=drawbridge-simulate",
                "-c",
                "user.email=simulate@drawbridge.local",
                *argv,
            ],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "--initial-branch", "main", "--quiet")
    git("add", "-A")
    git("commit", "--quiet", "-m", "demo application for simulation")
    git("remote", "add", "origin", "https://example.invalid/demo.git")


def _default_source_config_dir() -> Path | None:
    """Locate the repository's shipped operations/workflows catalogs."""
    candidate = Path(__file__).resolve().parents[3] / "configs"
    if (candidate / "operations.yaml").is_file():
        return candidate
    return None


def create_fixture(workdir: Path, source_config_dir: Path) -> Path:
    """Build the isolated demo repo + simulation config bundle.

    Returns the config directory; raises on any failure (never leaves a
    half-configured bundle behind silently).
    """
    repo_dir = workdir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    _write_demo_app(repo_dir)
    git_path = shutil.which("git")
    if git_path is None:
        raise DrawbridgeError("git is required to build the simulation fixture", code="UNSUPPORTED")
    _git_init_demo(repo_dir, git_path)

    compose_dir = workdir / "compose"
    compose_dir.mkdir(parents=True, exist_ok=True)
    compose_file = compose_dir / "demo.staging.yaml"
    compose_file.write_text(_DEMO_COMPOSE, encoding="utf-8")

    config_dir = workdir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_config_dir / "operations.yaml", config_dir / "operations.yaml")
    shutil.copyfile(source_config_dir / "workflows.yaml", config_dir / "workflows.yaml")

    (config_dir / "drawbridge.yaml").write_text(
        f"""\
# Generated by drawbridge-simulate — simulation communication test bundle.
schema_version: 1
server:
  bind_address: 127.0.0.1
  port: 8787
  base_path: /mcp
  allowed_cidrs:
    - 127.0.0.0/8
  allowed_origins:
    - "http://localhost:8787"
  allowed_hosts:
    - "127.0.0.1:8787"
  auth:
    mode: none
paths:
  state_dir: "{_posix(workdir / "state")}"
  lock_dir: "{_posix(workdir / "locks")}"
  log_dir: "{_posix(workdir / "logs")}"
  config_dir: "{_posix(config_dir)}"
toolchain:
  git: "{Path(git_path).resolve().as_posix()}"
  docker: "/usr/bin/docker"
  ps: "/usr/bin/ps"
  buildctl: "/usr/local/bin/buildctl"
concurrency:
  max_read_requests: 16
  max_running_jobs: 1
  max_queued_jobs: 50
  max_queued_jobs_per_target: 5
  queue_timeout_seconds: 600
  min_deploy_interval_seconds: 0
diagnostics:
  max_concurrent: 16
  retention_seconds: 86400
  wait_budget_seconds: 25
output:
  query_summary_max_bytes: 65536
  log_result_max_lines: 200
  log_result_max_bytes: 262144
  step_log_soft_limit_bytes: 1048576
  step_log_hard_limit_bytes: 20971520
  job_log_hard_limit_bytes: 104857600
maintenance:
  enabled: false
""",
        encoding="utf-8",
    )
    (config_dir / "apps.yaml").write_text(
        f"""\
# Generated by drawbridge-simulate — simulation application registry.
schema_version: 1
apps:
  demo:
    git:
      repo_path: "{_posix(repo_dir)}"
      origin: "https://example.invalid/demo.git"
      allowed_ref_patterns:
        - '^refs/heads/main$'
        - '^refs/heads/agent/[A-Za-z0-9_-]+$'
      tags_enabled: false
    npu:
      enabled: false
    environments:
      staging:
        runtime: simulation
        project_name: drawbridge-demo-sim
        build_profile: demo
        buildkit_socket: ""
        build_output_dir: "{_posix(workdir / "build-output")}"
        deploy_root: "{_posix(workdir / "deploy")}"
        compose_file: "{_posix(compose_file)}"
        health_checks:
          - type: http
            url: http://127.0.0.1:18080/healthz
            expected_status: 200
            timeout_seconds: 30
            single_timeout_seconds: 3
            interval_seconds: 1
            consecutive_successes: 1
        services: [api]
        restartable_services: [api]
        test_suites: [smoke]
        test_runner:
          smoke:
            image_id: sha256:0000000000000000000000000000000000000000000000000000000000000000
            entrypoint: /opt/drawbridge/suites/demo/smoke
            timeout_seconds: 60
            network: drawbridge-demo-test
        diagnostics:
          root: "{_posix(repo_dir)}"
          config_files:
            app_config:
              path: config/app.json
              raw: false
              fields: [name, listen_port, log_level]
              sensitive_fields: [admin_token]
          validators:
            json_syntax:
              executable: /usr/bin/python3
              argv: ["-m", "json.tool"]
              timeout_seconds: 15
        retention:
          successful_releases: 5
          job_logs_days: 7
        disk_budget_bytes: 1073741824

build_profiles:
  demo:
    context: .
    dockerfile_basename: Dockerfile
    platform: linux/arm64
    timeout_seconds: 300
    max_parallel: 1
""",
        encoding="utf-8",
    )
    return config_dir


def _select_target(config: Any, app: str | None, environment: str | None) -> tuple[str, str]:
    """Pick a simulation target from the loaded bundle."""
    candidates = [
        (app_id, env_name)
        for app_id, app_cfg in sorted(config.apps.items())
        for env_name, env_cfg in sorted(app_cfg.environments.items())
        if env_cfg.runtime == "simulation"
    ]
    if app is not None:
        candidates = [c for c in candidates if c[0] == app]
    if environment is not None:
        candidates = [c for c in candidates if c[1] == environment]
    if not candidates:
        raise DrawbridgeError(
            "no target with runtime: simulation found in the configuration "
            "(simulation mode is required for drawbridge-simulate)",
            code="CONFIG_INVALID",
        )
    return candidates[0]


class _StepRecorder:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    def ok(self, name: str, detail: Any = None) -> None:
        self.steps.append({"name": name, "status": "ok", "detail": _bounded(detail)})

    def error(self, name: str, exc: BaseException) -> None:
        code = getattr(exc, "code", "INTERNAL")
        self.steps.append(
            {
                "name": name,
                "status": "error",
                "code": str(code),
                "message": str(exc)[:300],
            }
        )

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [s for s in self.steps if s["status"] == "error"]


def _bounded(value: Any, limit: int = 4000) -> Any:
    text = json.dumps(value, default=str)
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}


async def _wait_terminal(store: Store, job_id: str, *, timeout: float) -> dict[str, Any]:
    """Poll the shared queue until the job reaches a terminal status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = await store.get_job(job_id)
        if job.status in JobStatus.TERMINAL:
            return {
                **job.to_public_dict(),
                "result": job.result,
                "recovery": job.recovery,
            }
        await asyncio.sleep(0.05)
    raise DrawbridgeError(
        f"job {job_id} did not reach a terminal state within {timeout:.0f}s",
        code="TIMEOUT",
    )


async def run_scenario(
    config_dir: str | Path,
    *,
    app: str | None = None,
    environment: str | None = None,
    git_ref: str = "refs/heads/main",
    job_timeout: float = 240.0,
    log: Any = None,
) -> dict[str, Any]:
    """Drive the full gateway↔runner conversation; returns the report."""
    config = load_config_from_dir(config_dir)
    app_id, env_name = _select_target(config, app, environment)

    database = Database(f"{config.main.paths.state_dir}/state.db")
    await database.connect()
    await database.initialize()
    store = Store(database)
    service = GatewayService(config, store, instance_id="simulate-gateway")
    runner = Runner(config, store, poll_interval=0.05, instance_id="simulate-runner")
    recorder = _StepRecorder()
    releases: list[str] = []

    async def step(name: str, coro: Any, *, expect: Any = None) -> Any:
        try:
            result = await coro
        except DrawbridgeError as exc:
            recorder.error(name, exc)
            return None
        recorder.ok(name, result.get("data", result) if isinstance(result, dict) else result)
        if expect is not None:
            got = result.get("status") if isinstance(result, dict) else None
            if got != expect:
                recorder.error(
                    name,
                    DrawbridgeError(
                        f"unexpected status {got!r}, expected {expect!r}",
                        code="INTERNAL",
                    ),
                )
        return result

    async def deploy_once(label: str) -> str | None:
        planned = await step(
            f"ops_release_plan#{label}",
            service.ops_release_plan(
                app=app_id,
                environment=env_name,
                source_mode="local",
                git_ref=git_ref,
                agent_id="drawbridge-simulate",
            ),
        )
        if planned is None or planned.get("status") != "ok":
            return None
        plan_id = planned["data"]["plan_id"]
        applied = await step(
            f"ops_release_apply#{label}",
            service.ops_release_apply(
                plan_id=plan_id,
                idempotency_key=f"simulate-{label}-{uuid.uuid4().hex[:12]}",
                agent_id="drawbridge-simulate",
            ),
        )
        if applied is None or applied.get("status") != "queued":
            return None
        job_id = applied["job_id"]
        final = await _wait_terminal(store, job_id, timeout=job_timeout)
        if final["status"] == JobStatus.SUCCEEDED:
            recorder.ok(f"deploy_job#{label}", final)
            release_id = (final.get("result") or {}).get("release_id")
            if release_id:
                releases.append(str(release_id))
            return str(release_id) if release_id else None
        recorder.error(
            f"deploy_job#{label}",
            DrawbridgeError(
                f"deploy job ended as {final['status']}",
                code=str((final.get("result") or {}).get("error", {}).get("code", "INTERNAL")),
            ),
        )
        return None

    runner_task = asyncio.create_task(runner.run_forever())
    try:
        await step("ops_catalog", service.ops_catalog())
        # Host metrics are Linux-only by design.  On other development hosts
        # the UNSUPPORTED envelope is itself the expected evidence that the
        # diagnostic channel transported the error correctly.
        try:
            status_result = await service.ops_status(app_id, env_name)
            recorder.ok("ops_status", status_result.get("data"))
        except DrawbridgeError as exc:
            if exc.code == "UNSUPPORTED" and sys.platform != "linux":
                recorder.steps.append(
                    {
                        "name": "ops_status",
                        "status": "ok",
                        "detail": {
                            "host_metrics": (
                                "UNSUPPORTED envelope verified (host metrics "
                                "are Linux-only; this host is a development "
                                "machine)"
                            )
                        },
                    }
                )
            else:
                recorder.error("ops_status", exc)
        await step(
            "ops_logs#before_deploy",
            service.ops_logs(app_id, env_name, service="api", limit=10),
        )
        await step(
            "ops_operation_run#git_status",
            service.ops_operation_run(operation="git_status", app=app_id, environment=env_name),
        )
        await step(
            "ops_operation_run#config_read",
            service.ops_operation_run(
                operation="config_read",
                app=app_id,
                environment=env_name,
                parameters={"file": "app_config"},
            ),
        )

        first = await deploy_once("one")
        if first is not None:
            logs = await step(
                "ops_logs#after_deploy",
                service.ops_logs(app_id, env_name, service="api", limit=5),
            )
            if logs is not None:
                lines = (logs.get("data") or {}).get("lines") or []
                if not lines:
                    recorder.error(
                        "ops_logs#after_deploy",
                        DrawbridgeError(
                            "simulation log page is empty after a successful deploy",
                            code="INTERNAL",
                        ),
                    )
            await step(
                "ops_operation_run#compose_status",
                service.ops_operation_run(
                    operation="compose_status", app=app_id, environment=env_name
                ),
            )
            second = await deploy_once("two")
            if second is not None:
                tested = await step(
                    "ops_test",
                    service.ops_test(
                        release_id=second,
                        suite="smoke",
                        idempotency_key=f"simulate-test-{uuid.uuid4().hex[:12]}",
                        agent_id="drawbridge-simulate",
                    ),
                )
                if tested is not None:
                    final = await _wait_terminal(store, tested["job_id"], timeout=job_timeout)
                    recorder.ok("test_job", final)
                restarted = await step(
                    "ops_service_restart",
                    service.ops_service_restart(
                        app=app_id,
                        environment=env_name,
                        service="api",
                        reason="simulation communication test",
                        idempotency_key=f"simulate-restart-{uuid.uuid4().hex[:12]}",
                        agent_id="drawbridge-simulate",
                    ),
                )
                if restarted is not None:
                    final = await _wait_terminal(store, restarted["job_id"], timeout=job_timeout)
                    recorder.ok("restart_job", final)
                rolled = await step(
                    "ops_release_rollback",
                    service.ops_release_rollback(
                        app=app_id,
                        environment=env_name,
                        release_id=first,
                        reason="simulation communication test",
                        idempotency_key=f"simulate-rollback-{uuid.uuid4().hex[:12]}",
                        agent_id="drawbridge-simulate",
                    ),
                )
                if rolled is not None:
                    final = await _wait_terminal(store, rolled["job_id"], timeout=job_timeout)
                    recorder.ok("rollback_job", final)
        current = await store.get_current_release(app_id, env_name)
        if log is not None:
            for entry in recorder.steps:
                log.info(
                    "scenario step",
                    step=entry["name"],
                    status=entry["status"],
                    code=entry.get("code"),
                )
        return {
            "ok": not recorder.failures,
            "app": app_id,
            "environment": env_name,
            "runtime": "simulation",
            "steps": recorder.steps,
            "releases": releases,
            "current_release": (
                {
                    "release_id": current.release_id,
                    "commit_sha": current.commit_sha,
                    "image_id": current.image_id,
                    "status": current.status,
                }
                if current is not None
                else None
            ),
        }
    finally:
        await runner.stop()
        runner_task.cancel()
        await asyncio.gather(runner_task, return_exceptions=True)
        await database.close()


# ---------------------------------------------------------------------------
# HTTP mode: the same conversation over the real network stack
# ---------------------------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _call_tool_http(session: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call one tool over Streamable HTTP; returns the parsed payload."""
    result = await session.call_tool(name, arguments)
    text = result.content[0].text if result.content else "{}"
    try:
        payload: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError:
        payload = {"raw": text[:500]}
    is_error = getattr(result, "is_error", getattr(result, "isError", False))
    if is_error:
        raise DrawbridgeError(
            str(payload.get("message", f"tool {name} failed")),
            code=str(payload.get("code", "INTERNAL")),
        )
    return payload


async def run_http_scenario(
    config_dir: str | Path,
    *,
    app: str | None = None,
    environment: str | None = None,
    git_ref: str = "refs/heads/main",
    job_timeout: float = 240.0,
    use_token: bool = True,
    log: Any = None,
) -> dict[str, Any]:
    """Drive the scenario through a real gateway + MCP Streamable HTTP client.

    The full production stack runs in-process: EdgeMiddleware (CIDR, Host,
    Origin, optional bearer token) → MCP protocol app → uvicorn, and the
    client is the official MCP SDK, so protocol serialization is exercised
    end to end.  When ``use_token`` is on, a random bearer token is
    generated and a token-less session is additionally asserted to be
    rejected by the edge middleware.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from drawbridge.gateway.mcp_app import MCPAppFactory
    from drawbridge.gateway.middleware import EdgeMiddleware, load_token_hash

    config = load_config_from_dir(config_dir)
    app_id, env_name = _select_target(config, app, environment)

    port = _free_port()
    config.main.server.bind_address = "127.0.0.1"
    config.main.server.port = port
    config.main.server.allowed_cidrs = ["127.0.0.0/8"]
    config.main.server.allowed_hosts = [f"127.0.0.1:{port}"]
    config.main.server.allowed_origins = [f"http://127.0.0.1:{port}"]

    token: str | None = None
    token_hash: str | None = None
    if use_token:
        token = secrets.token_urlsafe(24)
        token_file = Path(config.main.paths.state_dir) / "simulate.token"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token, encoding="utf-8")
        config.main.server.auth.mode = "token"
        config.main.server.auth.token_file = str(token_file)
        token_hash = load_token_hash(str(token_file))

    database = Database(f"{config.main.paths.state_dir}/state.db")
    await database.connect()
    await database.initialize()
    store = Store(database)
    service = GatewayService(config, store, instance_id="simulate-http-gw")
    edge = EdgeMiddleware(MCPAppFactory(service).build_asgi_app(), config.main.server, token_hash)

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            edge,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            # Production posture: the allowlist judges the socket peer.
            proxy_headers=False,
            lifespan="on",
        )
    )
    runner = Runner(config, store, poll_interval=0.05, instance_id="simulate-http-runner")
    recorder = _StepRecorder()
    releases: list[str] = []
    url = f"http://127.0.0.1:{port}{config.main.server.base_path}"

    async def deploy_over_http(session: Any, label: str) -> str | None:
        planned = await _call_tool_http(
            session,
            "ops_release_plan",
            {
                "app": app_id,
                "environment": env_name,
                "source_mode": "local",
                "git_ref": git_ref,
                "agent_id": "drawbridge-simulate-http",
            },
        )
        recorder.ok(f"ops_release_plan#{label}", planned.get("data"))
        plan_id = planned["data"]["plan_id"]
        applied = await _call_tool_http(
            session,
            "ops_release_apply",
            {
                "plan_id": plan_id,
                "idempotency_key": f"http-{label}-{uuid.uuid4().hex[:12]}",
                "agent_id": "drawbridge-simulate-http",
            },
        )
        recorder.ok(f"ops_release_apply#{label}", applied.get("data"))
        job_id = applied["job_id"]
        final = await _wait_terminal(store, job_id, timeout=job_timeout)
        if final["status"] == JobStatus.SUCCEEDED:
            recorder.ok(f"deploy_job#{label}", final)
            release_id = (final.get("result") or {}).get("release_id")
            if release_id:
                releases.append(str(release_id))
            return str(release_id) if release_id else None
        recorder.error(
            f"deploy_job#{label}",
            DrawbridgeError(
                f"deploy job ended as {final['status']}",
                code=str((final.get("result") or {}).get("error", {}).get("code", "INTERNAL")),
            ),
        )
        return None

    runner_task = asyncio.create_task(runner.run_forever())
    server_task = asyncio.create_task(server.serve())
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if not server.started:
            raise DrawbridgeError("gateway did not start in time", code="INTERNAL")

        if token is not None:
            # Negative check first: a session without credentials must be
            # rejected by the edge middleware itself, proving the guard is
            # active on this stack.
            try:
                # The SDK yields (read, write) or (read, write, get_session_id)
                # depending on the version — index defensively.
                async with (
                    streamable_http_client(url) as streams,
                    ClientSession(streams[0], streams[1]) as anon,
                ):
                    await anon.initialize()
                recorder.error(
                    "edge_auth#anonymous",
                    DrawbridgeError(
                        "anonymous session was accepted although token auth is on",
                        code="INTERNAL",
                    ),
                )
            except BaseException:
                # The SDK client surfaces the middleware's 403 as a
                # BaseExceptionGroup (HTTPStatusError + transport
                # cancellations) — the rejection itself is the expected
                # outcome here.
                recorder.ok(
                    "edge_auth#anonymous",
                    {"rejected": True, "detail": "request without bearer token refused"},
                )

        import httpx

        # The SDK types its http_client against its vendored httpx2 build;
        # plain httpx.AsyncClient is runtime-compatible (verified by the
        # HTTP scenario), so the difference is typing-only.
        http_client: Any = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"} if token else None,
            timeout=httpx.Timeout(60.0),
        )
        async with (
            streamable_http_client(url, http_client=http_client) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            recorder.ok("mcp_initialize", {"protocol": "streamable-http"})

            listed = await session.list_tools()
            names = {t.name for t in listed.tools}
            if "ops_catalog" not in names or "ops_history" not in names:
                recorder.error(
                    "tools_list",
                    DrawbridgeError(
                        "tool inventory incomplete",
                        code="INTERNAL",
                    ),
                )
            else:
                recorder.ok("tools_list", {"tools": sorted(names)})

            catalog = await _call_tool_http(session, "ops_catalog", {})
            recorder.ok("ops_catalog", catalog.get("data"))

            history = await _call_tool_http(
                session,
                "ops_history",
                {
                    "app": app_id,
                    "environment": env_name,
                    "what": "releases",
                    "limit": 5,
                },
            )
            recorder.ok("ops_history", history.get("data"))

            first = await deploy_over_http(session, "one")
            if first is not None:
                logs = await _call_tool_http(
                    session,
                    "ops_logs",
                    {
                        "app": app_id,
                        "environment": env_name,
                        "service": "api",
                        "limit": 5,
                    },
                )
                recorder.ok("ops_logs#after_deploy", logs.get("data"))
                second = await deploy_over_http(session, "two")
                if second is not None:
                    rolled = await _call_tool_http(
                        session,
                        "ops_release_rollback",
                        {
                            "app": app_id,
                            "environment": env_name,
                            "release_id": first,
                            "reason": "http simulation communication test",
                            "idempotency_key": (f"http-rollback-{uuid.uuid4().hex[:12]}"),
                        },
                    )
                    recorder.ok("ops_release_rollback", rolled.get("data"))
                    final = await _wait_terminal(store, rolled["job_id"], timeout=job_timeout)
                    recorder.ok("rollback_job", final)
        if log is not None:
            for entry in recorder.steps:
                log.info(
                    "http scenario step",
                    step=entry["name"],
                    status=entry["status"],
                    code=entry.get("code"),
                )
        current = await store.get_current_release(app_id, env_name)
        return {
            "ok": not recorder.failures,
            "mode": "http",
            "app": app_id,
            "environment": env_name,
            "runtime": "simulation",
            "auth": "token" if token else "none",
            "url": url,
            "steps": recorder.steps,
            "releases": releases,
            "current_release": (
                {
                    "release_id": current.release_id,
                    "commit_sha": current.commit_sha,
                    "image_id": current.image_id,
                    "status": current.status,
                }
                if current is not None
                else None
            ),
        }
    finally:
        server.should_exit = True
        await runner.stop()
        runner_task.cancel()
        await asyncio.gather(runner_task, server_task, return_exceptions=True)
        await database.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drawbridge-simulate",
        description=(
            "Run the full gateway↔runner communication scenario without "
            "systemd or containers (simulation runtime)."
        ),
    )
    parser.add_argument(
        "--config-dir",
        default=None,
        help="existing configuration bundle (target must declare runtime: simulation)",
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="build an isolated demo repo + config bundle and run against it "
        "(default when --config-dir is not given)",
    )
    parser.add_argument(
        "--source-config-dir",
        default=None,
        help="bundle to copy operations/workflows from in fixture mode "
        "(default: the repository's configs/ directory)",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="directory for the fixture (default: a fresh temporary directory)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the fixture directory")
    parser.add_argument("--app", default=None, help="app id (default: first simulation app)")
    parser.add_argument(
        "--environment", default=None, help="environment (default: first simulation environment)"
    )
    parser.add_argument("--ref", default="refs/heads/main", help="git ref to deploy")
    parser.add_argument(
        "--job-timeout", type=float, default=240.0, help="per-job wait budget in seconds"
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help=(
            "drive the scenario through a real gateway (uvicorn + edge "
            "middleware) and an MCP Streamable HTTP client instead of "
            "in-process service calls"
        ),
    )
    parser.add_argument(
        "--no-token",
        action="store_true",
        help="http mode only: disable the bearer-token check (auth: none)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    configure_logging(dev_mode=True, stream=sys.stderr)
    log = get_logger("drawbridge.simulate")

    workdir: Path | None = None
    cleanup = False
    try:
        if args.config_dir is not None:
            if args.fixture:
                print("--fixture and --config-dir are mutually exclusive", file=sys.stderr)
                return 2
            config_dir = Path(args.config_dir)
        else:
            source = (
                Path(args.source_config_dir)
                if args.source_config_dir
                else _default_source_config_dir()
            )
            if source is None or not (source / "operations.yaml").is_file():
                print(
                    "cannot locate a source config bundle for the fixture; pass "
                    "--source-config-dir or run from the repository",
                    file=sys.stderr,
                )
                return 2
            workdir = (
                Path(args.workdir)
                if args.workdir
                else Path(tempfile.mkdtemp(prefix="drawbridge-simulate-"))
            )
            cleanup = args.workdir is None and not args.keep
            config_dir = create_fixture(workdir, source)

        common = {
            "app": args.app,
            "environment": args.environment,
            "git_ref": args.ref,
            "job_timeout": args.job_timeout,
            "log": log,
        }
        if args.http:
            report = asyncio.run(
                run_http_scenario(config_dir, use_token=not args.no_token, **common)
            )
        else:
            report = asyncio.run(run_scenario(config_dir, **common))
        report["config_dir"] = str(config_dir)
        report["workdir"] = str(workdir) if workdir else None
        report["workdir_kept"] = bool(workdir is not None and not cleanup)
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["ok"] else 1
    except DrawbridgeError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, indent=2))
        return 1
    except Exception as exc:  # pragma: no cover - CLI guard
        print(f"drawbridge-simulate failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if cleanup and workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
