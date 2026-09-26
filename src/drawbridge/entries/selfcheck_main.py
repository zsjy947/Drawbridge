"""drawbridge-selfcheck: real installation verification (MVP spec §9).

Checks that must pass before the target server accepts traffic.  Missing
NPU tooling only disables one operation; missing isolation/build capability
blocks deployment features entirely.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from drawbridge.config.loader import load_config_from_dir
from drawbridge.logsetup import configure_logging


class CheckResult:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []
        self.warnings: list[str] = []
        self.skipped: list[str] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.passed += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))

    def fail(self, name: str, detail: str) -> None:
        self.failed.append(name)
        print(f"  [FAIL] {name} — {detail}")

    def warn(self, name: str, detail: str) -> None:
        self.warnings.append(name)
        print(f"  [WARN] {name} — {detail}")

    def skip(self, name: str, detail: str) -> None:
        """Role-scoped opt-out (plan D6): not applicable to this role, so it
        neither passes nor fails — a WARN/FAIL here only misled simulation
        deployments."""
        self.skipped.append(name)
        print(f"  [SKIP] {name} — {detail}")


#: Roles whose checks intentionally omit the container/build toolchain.
_BUILDLESS_ROLES = frozenset({"gateway", "simulation"})


def run_checks(config_dir: str, role: str = "all") -> CheckResult:
    configure_logging(dev_mode=True)
    results = CheckResult()

    # 1. Python hard gate (plan D6): the package requires 3.12+ — report the
    # baseline explicitly instead of merely printing the version.
    python_ok = sys.version_info >= (3, 12)
    detail = (
        f"{sys.version_info.major}.{sys.version_info.minor} "
        f"(runtime_baseline: Python >=3.12)"
    )
    if python_ok:
        results.ok("python runtime", detail)
    else:
        results.fail("python runtime", f"{detail} NOT satisfied")

    # 2. Configuration loads
    try:
        config = load_config_from_dir(config_dir)
        results.ok("config loads", f"digest {config.digest[:12]}")
    except Exception as exc:
        results.fail("config loads", str(exc)[:300])
        return results

    # 3. State database on a local filesystem (WAL requirement)
    state_dir = Path(config.main.paths.state_dir)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / ".drawbridge-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        results.ok("state dir writable", str(state_dir))
    except OSError as exc:
        results.fail("state dir writable", str(exc))

    # 4. Toolchain binaries exist and report versions.  Docker is a Runner
    # concern: gateway/simulation roles skip it instead of warning.
    buildless = role in _BUILDLESS_ROLES
    for tool_name, version_args in (
        ("git", ["--version"]),
        ("docker", ["version", "--format", "{{.Server.Version}}"]),
    ):
        tool_path = getattr(config.main.toolchain, tool_name, "")
        if not tool_path or not Path(tool_path).exists():
            if tool_name == "docker" and buildless:
                results.skip(
                    "toolchain docker", f"not required for role {role!r}"
                )
                continue
            severity = results.fail if tool_name == "git" else results.warn
            severity(f"toolchain {tool_name}", f"{tool_path!r} not found")
            continue
        try:
            proc = subprocess.run(
                [tool_path, *version_args],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            results.ok(f"toolchain {tool_name}", proc.stdout.strip()[:80])
        except (OSError, subprocess.SubprocessError) as exc:
            if tool_name == "docker" and buildless:
                results.skip(f"toolchain {tool_name}", f"role {role!r}: {exc}")
            else:
                results.warn(f"toolchain {tool_name}", f"present but unusable: {exc}")

    # 5. Compose --wait / --pull never support (best-effort probe; Runner only)
    docker_path = config.main.toolchain.docker
    if buildless:
        results.skip("docker compose", f"not required for role {role!r}")
    elif Path(docker_path).exists():
        try:
            proc = subprocess.run(
                [docker_path, "compose", "version", "--short"],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            results.ok("docker compose", proc.stdout.strip()[:60])
        except (OSError, subprocess.SubprocessError) as exc:
            results.warn("docker compose", str(exc))

    # 6. Rootless BuildKit socket (Runner concern; gateway/simulation skip)
    if buildless:
        results.skip("rootless buildkit socket present", f"not required for role {role!r}")
    else:
        buildkit_ok = False
        missing_sockets: list[str] = []
        for app_id, app in config.apps.items():
            for env_name, env in app.environments.items():
                socket_path = env.buildkit_socket
                if not socket_path:
                    missing_sockets.append(f"{app_id}/{env_name}")
                    continue
                if Path(socket_path).exists():
                    buildkit_ok = True
                else:
                    missing_sockets.append(f"{app_id}/{env_name} ({socket_path})")
        if buildkit_ok:
            results.ok("rootless buildkit socket present")
        else:
            results.warn(
                "rootless buildkit socket present",
                f"deployment features blocked; missing for {missing_sockets or 'all apps'}",
            )

    # 7. Registered repos exist with expected origin config
    for app_id, app in config.apps.items():
        repo = Path(app.git.repo_path)
        if not repo.exists():
            results.warn(f"repo {app_id}", f"{app.git.repo_path} not cloned yet")
        else:
            results.ok(f"repo {app_id}", str(repo))

    # 7b. Every registered handler operation has a code implementation and
    # every executable operation maps to a registered toolchain path —
    # a YAML/catalog drift must be caught before serving traffic.
    from drawbridge.executor.spec import resolve_toolchain
    from drawbridge.runner.handlers import DIAGNOSTIC_ACTIONS, HANDLERS
    from drawbridge.runner.runtime import DeployRuntime

    runtime_steps = DeployRuntime.__dict__
    drift: list[str] = []
    for op_name, op in config.operations.items():
        if op.handler is not None:
            implemented = op_name in HANDLERS or f"step_{op.handler}" in runtime_steps
            if not implemented:
                drift.append(f"{op_name}: handler {op.handler!r} not implemented")
            if op.public and op.access.value == "read" and op_name not in DIAGNOSTIC_ACTIONS:
                drift.append(f"{op_name}: public read not in diagnostic channel")
        if op.executable is not None:
            try:
                resolve_toolchain(config.main.toolchain, op.executable)
            except Exception:
                drift.append(f"{op_name}: toolchain {op.executable!r} not registered")
    # workflow steps must all be executable by the production runtime
    for wf_name, wf in config.workflows.items():
        for step in wf.steps:
            if f"step_{step.operation}" not in runtime_steps:
                drift.append(
                    f"workflow {wf_name!r} step {step.id!r}: "
                    f"{step.operation!r} not implemented in the deploy runtime"
                )
    if drift:
        results.fail("operation catalog consistency", "; ".join(drift[:6]))
    else:
        results.ok(
            "operation catalog consistency",
            f"{len(config.operations)} operations, "
            f"{len(config.workflows)} workflows",
        )

    # 8. Disk budget headroom
    usage = shutil.disk_usage(config.main.paths.state_dir)
    budgets = [
        env.disk_budget_bytes
        for app in config.apps.values()
        for env in app.environments.values()
    ]
    min_budget = min(budgets, default=1024**3)
    if usage.free >= min_budget:
        results.ok("disk budget headroom", f"{usage.free // (1024**2)} MiB free")
    else:
        results.fail(
            "disk budget headroom",
            f"{usage.free // (1024**2)} MiB free < {min_budget // (1024**2)} MiB required",
        )

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="drawbridge-selfcheck")
    parser.add_argument("--config-dir", default="/etc/drawbridge")
    parser.add_argument(
        "--role",
        choices=["gateway", "runner", "simulation", "all"],
        default="all",
        help=(
            "scope the checks to one deployment role (plan D6): gateway and"
            " simulation skip the docker/compose/buildkit checks (SKIP lines,"
            " never warnings or failures); runner keeps the full set; all"
            " preserves the historical behaviour"
        ),
    )
    args = parser.parse_args(argv)
    print(f"Drawbridge installation self-check (role: {args.role})")
    results = run_checks(args.config_dir, role=args.role)
    print(
        f"\n{results.passed} passed, {len(results.failed)} failed, "
        f"{len(results.warnings)} warnings, {len(results.skipped)} skipped"
    )
    if results.warnings:
        print("warnings disable features but do not block startup")
    return 0 if not results.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
