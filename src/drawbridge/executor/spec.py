"""ExecutionSpec / ExecutionResult: the executor seam (tech design §13).

``ExecutionSpec`` is built exclusively from validated configuration and
validated parameters — it carries a fixed absolute program, a fixed argv,
a cwd chosen from registered roots, a fully constructed environment and
output budgets.  Nothing in an ExecutionSpec originates from raw request
strings without having passed :mod:`drawbridge.policy.params` first.

``ExecutionResult`` reports the outcome with a ``termination_reason`` that
distinguishes normal exit, timeout, output overrun, startup failure and
cancellation, plus bounded summaries and a reference to spooled logs.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from drawbridge.config.models import (
    FORBIDDEN_PROFILE_ENV_KEYS,
    ExecutionProfile,
    OutputPolicyKind,
    ProfileEnvConfig,
    ToolchainConfig,
)
from drawbridge.errors import DrawbridgeError, ErrorCode

#: Fixed PATH for the target Linux platform.
LINUX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

#: Locale / timezone pinned for every child process.
LANG = "C.UTF-8"
TZ = "UTC"

TERMINATION_COMPLETED = "completed"
TERMINATION_TIMEOUT = "timeout"
TERMINATION_OUTPUT_LIMIT = "output_limit"
TERMINATION_START_ERROR = "start_error"
TERMINATION_CANCELLED = "cancelled"

_TERMINATION_REASONS = frozenset(
    {
        TERMINATION_COMPLETED,
        TERMINATION_TIMEOUT,
        TERMINATION_OUTPUT_LIMIT,
        TERMINATION_START_ERROR,
        TERMINATION_CANCELLED,
    }
)


@dataclass(frozen=True)
class ExecutionSpec:
    """Everything the ProcessManager is allowed to know."""

    operation: str
    executable: str
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    profile: ExecutionProfile
    timeout_seconds: float
    output_policy: OutputPolicyKind
    max_output_bytes: int
    hard_output_limit: int
    accepted_exit_codes: frozenset[int]
    stdin_data: bytes | None = None
    log_path: str | None = None
    job_id: str | None = None
    step_id: str | None = None
    summary_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if not os.path.isabs(self.executable):
            raise DrawbridgeError(
                f"executable must be an absolute path, got {self.executable!r}",
                code=ErrorCode.INTERNAL,
            )
        if not self.argv:
            raise DrawbridgeError("argv must not be empty", code=ErrorCode.INTERNAL)
        for element in self.argv:
            if "\x00" in element:
                raise DrawbridgeError("argv element contains NUL", code=ErrorCode.INTERNAL)
        if self.max_output_bytes < 1:
            raise DrawbridgeError("max_output_bytes must be positive", code=ErrorCode.INTERNAL)
        if self.hard_output_limit < self.max_output_bytes:
            raise DrawbridgeError(
                "hard output limit must not be below max_output_bytes",
                code=ErrorCode.INTERNAL,
            )
        if self.timeout_seconds <= 0:
            raise DrawbridgeError("timeout must be positive", code=ErrorCode.INTERNAL)
        if self.cwd and not os.path.isabs(self.cwd):
            raise DrawbridgeError("cwd must be absolute or empty", code=ErrorCode.INTERNAL)

    def result(
        self,
        *,
        exit_code: int | None,
        termination_reason: str,
        duration_ms: int,
        stdout_bytes: int,
        stderr_bytes: int,
        truncated: bool,
        stdout_preview: str,
        stderr_preview: str,
        log_path: str | None,
        start_error: str | None = None,
    ) -> ExecutionResult:
        if termination_reason not in _TERMINATION_REASONS:
            raise ValueError(f"unknown termination_reason {termination_reason!r}")
        accepted = (
            termination_reason == TERMINATION_COMPLETED
            and exit_code is not None
            and exit_code in self.accepted_exit_codes
        )
        return ExecutionResult(
            operation=self.operation,
            exit_code=exit_code,
            termination_reason=termination_reason,
            duration_ms=duration_ms,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            truncated=truncated,
            stdout_preview=stdout_preview,
            stderr_preview=stderr_preview,
            accepted=accepted,
            log_path=log_path,
            start_error=start_error,
        )


@dataclass(frozen=True)
class ExecutionResult:
    operation: str
    exit_code: int | None
    termination_reason: str
    duration_ms: int
    stdout_bytes: int
    stderr_bytes: int
    truncated: bool
    stdout_preview: str
    stderr_preview: str
    accepted: bool
    log_path: str | None
    start_error: str | None = None

    def to_dict(self, *, redact: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "operation": self.operation,
            "exit_code": self.exit_code,
            "termination_reason": self.termination_reason,
            "duration_ms": self.duration_ms,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "truncated": self.truncated,
            "accepted": self.accepted,
        }
        if self.log_path:
            payload["log_ref"] = os.path.basename(self.log_path)
        if self.start_error:
            payload["start_error"] = self.start_error
        if not redact:
            payload["stdout_preview"] = self.stdout_preview
            payload["stderr_preview"] = self.stderr_preview
        return payload


def build_environment(
    profile: ExecutionProfile,
    profile_env: Mapping[ExecutionProfile, ProfileEnvConfig],
    *,
    extra: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> dict[str, str]:
    """Construct the child environment from an empty set (MVP spec §3).

    Fixed PATH/LANG/TZ, profile HOME and admin-registered extras.  Hazardous
    inheritance variables are cleared by construction — the environment is
    built from ``{}``, never from ``os.environ`` — and profile config may
    never set the forbidden keys (enforced again here, fail closed).
    """
    target = platform or sys.platform
    if target == "win32":
        # Development/test hosts only; production always runs on Linux.
        sysroot = os.environ.get("SystemRoot", "C:/Windows")  # noqa: SIM112
        path = f"{os.path.dirname(sys.executable)}{os.pathsep}{sysroot}/System32"
    else:
        path = LINUX_PATH

    env: dict[str, str] = {
        "PATH": path,
        "LANG": LANG,
        "TZ": TZ,
    }
    registered = profile_env.get(profile)
    if registered is not None:
        if registered.home:
            env["HOME"] = registered.home
        for key, value in registered.extra_env.items():
            if key.upper() in FORBIDDEN_PROFILE_ENV_KEYS or key in ("LANG", "TZ"):
                raise DrawbridgeError(
                    f"profile {profile.value} may not set {key}",
                    code=ErrorCode.CONFIG_INVALID,
                )
            env[key] = value
    else:
        env["HOME"] = "/var/lib/drawbridge/home"
    if extra:
        for key, value in extra.items():
            if key.upper() in FORBIDDEN_PROFILE_ENV_KEYS or key in ("PATH", "LANG", "TZ"):
                raise DrawbridgeError(
                    f"environment override {key} is not permitted", code=ErrorCode.INTERNAL
                )
            if "\x00" in value:
                raise DrawbridgeError(
                    f"environment value for {key} contains NUL", code=ErrorCode.INTERNAL
                )
            env[key] = value
    return env


def git_environment(
    profile_env: Mapping[ExecutionProfile, ProfileEnvConfig],
    *,
    platform: str | None = None,
    config_global: str = "/etc/drawbridge/gitconfig",
    ssh_wrapper: str = "/etc/drawbridge/ssh-wrapper",
    terminal_prompt: str = "0",
) -> dict[str, str]:
    """Git-specific environment: no prompts, no system config, fixed wrapper."""
    env = build_environment(
        ExecutionProfile.SOURCE_MANAGE, profile_env, platform=platform
    )
    env.update(
        {
            "GIT_TERMINAL_PROMPT": terminal_prompt,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": config_global,
            "GIT_SSH": ssh_wrapper,
            "GIT_ASKPASS": "/bin/true",
        }
    )
    return env


def docker_environment(
    profile_env: Mapping[ExecutionProfile, ProfileEnvConfig],
    *,
    docker_host: str = "unix:///var/run/docker.sock",
    docker_config: str = "/etc/drawbridge/docker",
    platform: str | None = None,
) -> dict[str, str]:
    """Docker CLI environment: fixed host/context, no user overrides."""
    env = build_environment(
        ExecutionProfile.RUNTIME_MANAGE, profile_env, platform=platform
    )
    env.update(
        {
            "DOCKER_HOST": docker_host,
            "DOCKER_CONFIG": docker_config,
            "DOCKER_CLI_HINTS": "false",
        }
    )
    return env


def resolve_toolchain(toolchain: ToolchainConfig, executable_name: str) -> str:
    """Map a toolchain name to its registered absolute path."""
    registered = getattr(toolchain, executable_name, None)
    if not isinstance(registered, str) or not registered:
        raise DrawbridgeError(
            f"toolchain does not register {executable_name!r}", code=ErrorCode.CONFIG_INVALID
        )
    return registered


__all__ = [
    "TERMINATION_CANCELLED",
    "TERMINATION_COMPLETED",
    "TERMINATION_OUTPUT_LIMIT",
    "TERMINATION_START_ERROR",
    "TERMINATION_TIMEOUT",
    "ExecutionResult",
    "ExecutionSpec",
    "build_environment",
    "docker_environment",
    "git_environment",
    "resolve_toolchain",
]
