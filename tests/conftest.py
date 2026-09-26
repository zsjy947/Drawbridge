"""Shared test fixtures and helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from drawbridge.config.models import ExecutionProfile, OutputPolicyKind, ToolchainConfig
from drawbridge.executor.spec import ExecutionSpec, build_environment

REPO_ROOT = Path(__file__).resolve().parents[1]


def install_compose_template(config: Any, tmp_path: Path) -> str:
    """Materialize the repo sample template inside tmp_path, point every
    environment at it and return its fingerprint (plan D3 test helper).

    Plan/apply paths read and re-validate the admin compose template, so any
    test that creates plans against the sample config bundle needs a real,
    structurally valid template file and its digest.
    """
    from drawbridge.config.compose_template import read_compose_template

    source = REPO_ROOT / "configs" / "compose" / "demo.staging.yaml"
    target = tmp_path / "compose" / "demo.staging.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    digest = ""
    for app in config.apps.values():
        for env in app.environments.values():
            info = read_compose_template(target, list(env.services))
            env.compose_file = str(target)
            digest = info.digest
    return digest


@pytest.fixture()
def profile_env():
    from drawbridge.config.models import ProfileEnvConfig

    return {p: ProfileEnvConfig() for p in ExecutionProfile}


def make_env(profile: ExecutionProfile = ExecutionProfile.SOURCE_MANAGE) -> dict[str, str]:
    return build_environment(profile, {})


def make_spec(
    argv: tuple[str, ...] = (),
    *,
    executable: str | None = None,
    timeout: float = 10.0,
    policy: OutputPolicyKind = OutputPolicyKind.TERMINATE,
    max_output_bytes: int = 65536,
    hard_limit: int | None = None,
    accepted: frozenset[int] = frozenset({0}),
    stdin_data: bytes | None = None,
    log_path: str | None = None,
    cwd: str | None = None,
    operation: str = "test_op",
) -> ExecutionSpec:
    if executable is None:
        executable = sys.executable
    return ExecutionSpec(
        operation=operation,
        executable=executable,
        argv=argv,
        cwd=cwd or os.getcwd(),
        env=make_env(),
        profile=ExecutionProfile.SOURCE_MANAGE,
        timeout_seconds=timeout,
        output_policy=policy,
        max_output_bytes=max_output_bytes,
        hard_output_limit=hard_limit if hard_limit is not None else max_output_bytes,
        accepted_exit_codes=accepted,
        stdin_data=stdin_data,
        log_path=log_path,
    )


@pytest.fixture()
def toolchain() -> ToolchainConfig:
    return ToolchainConfig()


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            check=False,
        )
        # tasklist output is locale-encoded (GBK on zh-CN Windows); decode
        # leniently — we only need the ASCII digits of the PID column.
        stdout = result.stdout.decode("utf-8", errors="ignore")
        return f" {pid} " in stdout.replace("\n", " ") + " "
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
