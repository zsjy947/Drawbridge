"""Strict Pydantic models for all Drawbridge YAML configuration.

Configuration is the authority (tech design §12): YAML files under
``/etc/drawbridge`` are maintained by the administrator, loaded once at
startup, and validated with ``model_config = extra="forbid"`` strict
Pydantic models.  Nothing in the request path may widen these models.

Global strictness rules (MVP spec §2):

* no implicit coercion: strings never become ints, bools never become ints;
* no trimming / case folding / URL decoding of any value;
* NUL and other control characters are rejected everywhere, including
  ``reason`` fields (which additionally reject line breaks);
* administrator regexes are ASCII, at most 1024 characters, and are always
  executed with ``regex.fullmatch`` under a 100 ms budget (enforced in
  :mod:`drawbridge.policy.params`, not at load time).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Shared validation primitives
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_SERVICE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")
_ASCII_ONLY = re.compile(r"^[\x20-\x7e]+$")
#: Windows drive-absolute path (development/simulation hosts only; the
#: 910B deployment always uses POSIX absolute paths).
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/].*")


def _is_absolute_path(value: str) -> bool:
    """Accept POSIX absolute paths and, on development hosts, drive letters.

    Production configs on the Linux target are unaffected; the drive-letter
    form exists so simulation bundles can be expressed as YAML on the
    Windows machines used for development and communication testing.
    """
    return value.startswith("/") or _WINDOWS_ABSOLUTE.fullmatch(value) is not None


#: Maximum length of an administrator-supplied regular expression.
MAX_PATTERN_LENGTH = 1024

#: Execution budget for one admin regex match, in seconds.
REGEX_BUDGET_SECONDS = 0.1


def check_no_control_chars(value: str, *, field: str) -> str:
    """Reject NUL and C0/C1 control characters (spec: 拒绝 NUL、控制字符)."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or 0x80 <= ord(ch) <= 0x9F for ch in value):
        raise ValueError(f"{field}: control characters are not allowed")
    return value


class StrictModel(BaseModel):
    """Base model: forbid extra fields, disable all implicit coercion."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=False,
        revalidate_instances="never",
        validate_default=True,
    )


class StrEnum_(StrEnum):
    """StrEnum whose values are validated without case folding."""


# ---------------------------------------------------------------------------
# Enumerations fixed by the MVP spec
# ---------------------------------------------------------------------------


class ExecutionProfile(StrEnum_):
    HOST_OBSERVE = "host_observe"
    SOURCE_MANAGE = "source_manage"
    PROJECT_DIAGNOSTIC = "project_diagnostic"
    IMAGE_BUILD = "image_build"
    RUNTIME_MANAGE = "runtime_manage"
    ISOLATED_TEST = "isolated_test"


class Access(StrEnum_):
    READ = "read"
    RUNTIME_WRITE = "runtime_write"


class OutputPolicyKind(StrEnum_):
    TERMINATE = "terminate"
    SPOOL = "spool"


class CwdFrom(StrEnum_):
    APP_REPO = "app.repo_path"
    RELEASE_DIR = "release.dir"
    ROOT = "root"


class SourceMode(StrEnum_):
    FETCH = "fetch"
    LOCAL = "local"


def _enum_coercer(enum_class: type[StrEnum_]) -> Any:
    """Accept plain YAML strings for enum fields under strict mode."""

    def _coerce(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return enum_class(value)
            except ValueError as exc:
                raise ValueError(f"{value!r} is not a valid {enum_class.__name__}") from exc
        return value

    return _coerce


ExecutionProfileField = Annotated[
    ExecutionProfile, BeforeValidator(_enum_coercer(ExecutionProfile))
]
AccessField = Annotated[Access, BeforeValidator(_enum_coercer(Access))]
CwdFromField = Annotated[CwdFrom, BeforeValidator(_enum_coercer(CwdFrom))]
OutputPolicyField = Annotated[OutputPolicyKind, BeforeValidator(_enum_coercer(OutputPolicyKind))]


# Named argv prefixes fixed in code.  ``git_safe`` implements the unified Git
# prefix mandated by MVP spec §3 (empty hooks dir, fsmonitor off, restricted
# protocols, cleared credential helper).
ARGV_PREFIX_PRESETS: dict[str, tuple[str, ...]] = {
    "git_safe": (
        "--no-pager",
        "-c",
        "core.hooksPath=/etc/drawbridge/empty-hooks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.ssh.allow=always",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "credential.helper=",
        "-c",
        "gc.auto=0",
    ),
}

# Fixed prepare hooks: purely internal, referenced by name, never code.
PREPARE_PRESETS = frozenset(
    {
        "resolve_allowed_local_ref",
        "resolve_allowed_remote_ref",
        "none",
    }
)

# Fixed built-in handlers.  A handler is a code-level enum entry, never a
# Python module path (MVP spec §6).
BUILTIN_HANDLERS = frozenset(
    {
        "config_read",
        "project_list",
        "config_validate",
        "check_project_config",
        "host_metrics",
        "npu_status",
        "compose_status",
        "compose_logs",
        "service_restart_and_verify",
        "source_fetch",
        "enumerate_remote_refs",
        "ref_format",
        "resolve_commit",
        "check_reachable",
        "source_snapshot",
        "image_build",
        "image_import",
        "image_identify",
        "compose_deploy",
        "health_check",
        "test_suite",
        "restore_previous",
        "stop_initial",
        "release_preflight",
        "release_finalize",
        "fixed_script",
    }
)

# Semantic parameter validators referenced by name from operations.yaml.
SEMANTIC_VALIDATORS = frozenset(
    {
        "allowed_app_ref",
        "git_ref_or_commit",
        "registered_service",
        "registered_restartable_service",
        "registered_file_alias",
        "registered_validator",
        "environment_of_app",
        "registered_subdir",
        "opaque_cursor",
    }
)

_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\}")
_BRACE_FREE_RE = re.compile(r"^[^{}]*(\{[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\}[^{}]*)?$")

_ALLOWED_CONTEXTS = frozenset({"param", "app", "job", "release"})


def _validate_template_element(element: str) -> str:
    """Validate one argv template element (MVP spec §3).

    Each element yields exactly one argv slot.  A single placeholder with a
    literal prefix/suffix is allowed; list splices, multiple placeholders,
    bare expressions and stray braces are rejected.
    """
    check_no_control_chars(element, field="argv element")
    if not element:
        raise ValueError("argv element must not be empty")
    placeholders = _PLACEHOLDER_RE.findall(element)
    if placeholders:
        if len(placeholders) > 1:
            raise ValueError(f"argv element has multiple placeholders: {element!r}")
        context = placeholders[0][0]
        if context not in _ALLOWED_CONTEXTS:
            raise ValueError(f"argv placeholder context {context!r} is not allowed")
    if not _BRACE_FREE_RE.fullmatch(element):
        raise ValueError(f"argv element is not a literal-with-single-placeholder: {element!r}")
    return element


# ---------------------------------------------------------------------------
# Parameter specification (operations.yaml)
# ---------------------------------------------------------------------------


class ParameterSpec(StrictModel):
    """Declarative parameter schema for one operation parameter."""

    type: Literal["string", "integer"]
    default: int | str | None = None
    min_length: int | None = Field(default=None, ge=1)
    max_length: int | None = Field(default=None, ge=1, le=4096)
    minimum: int | None = None
    maximum: int | None = None
    pattern: str | None = None
    validators: list[str] = Field(default_factory=list)
    sensitive: bool = False
    description: str = ""

    @field_validator("pattern")
    @classmethod
    def _check_pattern(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > MAX_PATTERN_LENGTH:
            raise ValueError(f"pattern longer than {MAX_PATTERN_LENGTH} characters")
        if not _ASCII_ONLY.fullmatch(value):
            raise ValueError("pattern must be ASCII")
        return value

    @field_validator("validators")
    @classmethod
    def _check_validators(cls, value: list[str]) -> list[str]:
        unknown = [v for v in value if v not in SEMANTIC_VALIDATORS]
        if unknown:
            raise ValueError(f"unknown semantic validators: {unknown}")
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> ParameterSpec:
        if self.type == "string":
            if self.minimum is not None or self.maximum is not None:
                raise ValueError("string parameter must not declare minimum/maximum")
        else:
            if self.min_length is not None or self.max_length is not None:
                raise ValueError("integer parameter must not declare min_length/max_length")
            if (
                self.minimum is not None
                and self.maximum is not None
                and self.minimum > self.maximum
            ):
                raise ValueError("minimum must not exceed maximum")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length must not exceed max_length")
        if self.default is not None:
            if self.type == "integer":
                if not isinstance(self.default, int) or isinstance(self.default, bool):
                    raise ValueError("integer default must be an integer")
                if self.minimum is not None and self.default < self.minimum:
                    raise ValueError("default below minimum")
                if self.maximum is not None and self.default > self.maximum:
                    raise ValueError("default above maximum")
            else:
                if not isinstance(self.default, str):
                    raise ValueError("string default must be a string")
                check_no_control_chars(self.default, field="default")
                if self.min_length is not None and len(self.default) < self.min_length:
                    raise ValueError("default shorter than min_length")
                if self.max_length is not None and len(self.default) > self.max_length:
                    raise ValueError("default longer than max_length")
        return self


class OutputBudget(StrictModel):
    policy: OutputPolicyField = OutputPolicyKind.TERMINATE
    max_bytes: int = Field(default=65536, ge=1, le=64 * 1024 * 1024)


class OperationConfig(StrictModel):
    """One entry of operations.yaml (MVP spec §4-§6).

    Exactly one of ``executable`` (a toolchain name) or ``handler`` (a fixed
    code-level enum entry) must be present.
    """

    executable: str | None = None
    argv_prefix: str | None = None
    argv: list[str] | None = None
    handler: str | None = None
    prepare: str = "none"
    parameters: dict[str, ParameterSpec] = Field(default_factory=dict)
    cwd_from: CwdFromField | None = None
    execution_profile: ExecutionProfileField
    public: bool
    access: AccessField
    timeout_seconds: int = Field(ge=1, le=3600)
    accepted_exit_codes: list[int] = Field(default_factory=lambda: [0])
    output: OutputBudget = Field(default_factory=OutputBudget)
    lock: Literal["app_environment"] | None = None

    @field_validator("argv")
    @classmethod
    def _check_argv(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [_validate_template_element(element) for element in value]

    @field_validator("argv_prefix")
    @classmethod
    def _check_prefix(cls, value: str | None) -> str | None:
        if value is not None and value not in ARGV_PREFIX_PRESETS:
            raise ValueError(f"unknown argv_prefix preset {value!r}")
        return value

    @field_validator("prepare")
    @classmethod
    def _check_prepare(cls, value: str) -> str:
        if value not in PREPARE_PRESETS:
            raise ValueError(f"unknown prepare preset {value!r}")
        return value

    @field_validator("executable")
    @classmethod
    def _check_executable(cls, value: str | None) -> str | None:
        if value is not None and not _ID_RE.fullmatch(value):
            raise ValueError("executable must reference a toolchain name")
        return value

    @model_validator(mode="after")
    def _check_operation_shape(self) -> OperationConfig:
        if (self.executable is None) == (self.handler is None):
            raise ValueError("operation must declare exactly one of executable/handler")
        if self.handler is not None and self.handler not in BUILTIN_HANDLERS:
            raise ValueError(f"unknown handler {self.handler!r}")
        if self.handler is not None:
            if self.argv is not None or self.argv_prefix is not None or self.cwd_from is not None:
                raise ValueError("handler operations must not declare argv/argv_prefix/cwd_from")
        elif not self.argv:
            raise ValueError("executable operations must declare a non-empty argv")
        return self


# ---------------------------------------------------------------------------
# Workflow configuration (workflows.yaml)
# ---------------------------------------------------------------------------


class WorkflowStepConfig(StrictModel):
    id: str = Field(pattern=r"[a-z][a-z0-9_]{0,63}")
    operation: str = Field(pattern=r"[a-z][a-z0-9_]{0,63}")
    parameters: dict[str, int | str] = Field(default_factory=dict)


class WorkflowFailurePolicy(StrictModel):
    before_runtime_change: Literal["stop"] = "stop"
    after_runtime_change: Literal["restore_previous_release"] = "restore_previous_release"


class WorkflowConfig(StrictModel):
    requires_plan: bool = True
    lock: Literal["app_environment"] = "app_environment"
    timeout_seconds: int = Field(ge=1, le=7200)
    recovery_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    steps: list[WorkflowStepConfig] = Field(min_length=1)
    on_failure: WorkflowFailurePolicy = Field(default_factory=WorkflowFailurePolicy)


# ---------------------------------------------------------------------------
# Application registry (apps.yaml)
# ---------------------------------------------------------------------------


class GitConfig(StrictModel):
    repo_path: str = Field(min_length=1, max_length=1024)
    origin: str = Field(min_length=1, max_length=1024)
    allowed_ref_patterns: list[str] = Field(min_length=1, max_length=100)
    tags_enabled: bool = False

    @field_validator("allowed_ref_patterns")
    @classmethod
    def _check_patterns(cls, value: list[str]) -> list[str]:
        if len(value) > 100:
            raise ValueError("at most 100 allowed ref patterns")
        for pattern in value:
            if len(pattern) > MAX_PATTERN_LENGTH or not _ASCII_ONLY.fullmatch(pattern):
                raise ValueError("allowed_ref_patterns must be ASCII and bounded")
        return value

    @field_validator("repo_path", "origin")
    @classmethod
    def _check_paths(cls, value: str) -> str:
        return check_no_control_chars(value, field="path")


class HealthCheckConfig(StrictModel):
    type: Literal["http"]
    url: str = Field(min_length=1, max_length=2048)
    expected_status: int = Field(default=200, ge=200, le=299)
    timeout_seconds: int = Field(default=90, ge=1, le=3600)
    single_timeout_seconds: int = Field(default=3, ge=1, le=30)
    interval_seconds: int = Field(default=2, ge=1, le=30)
    consecutive_successes: int = Field(default=3, ge=1, le=30)
    follow_redirects: bool = False


class TestResources(StrictModel):
    cpus: str = Field(default="1", pattern=r"[0-9]+(\.[0-9]+)?")
    memory: str = Field(default="512m", pattern=r"[0-9]+[mkgb]")
    pids_limit: int = Field(default=128, ge=1, le=4096)


class TestSuiteConfig(StrictModel):
    image_id: str = Field(min_length=1, max_length=256)
    entrypoint: str = Field(min_length=1, max_length=1024)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    network: str = Field(pattern=_SERVICE_RE.pattern)
    resources: TestResources = Field(default_factory=TestResources)


class ConfigFileAlias(StrictModel):
    path: str = Field(min_length=1, max_length=512)
    raw: bool = False
    fields: list[str] = Field(default_factory=list)
    sensitive_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> ConfigFileAlias:
        if self.raw and self.fields:
            raise ValueError("raw config alias must not declare a field whitelist")
        if not self.raw and not self.fields:
            raise ValueError("non-raw config alias must declare a field whitelist")
        return self


class ValidatorConfig(StrictModel):
    """Registered plugin validator (MVP spec §4: isolated_test, post-MVP).

    NOTE (plan D11): registered validators are NOT executed by the current
    code — ``config_validate`` only runs the built-in JSON/TOML parsers.
    The registry is the reserved plugin path; do not assume a listed
    validator has run when reading diagnostic output.
    """

    executable: str
    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=15, ge=1, le=300)

    @field_validator("argv")
    @classmethod
    def _check_argv(cls, value: list[str]) -> list[str]:
        return [_validate_template_element(element) for element in value]


class DiagnosticsConfig(StrictModel):
    root: str
    config_files: dict[str, ConfigFileAlias] = Field(default_factory=dict)
    validators: dict[str, ValidatorConfig] = Field(default_factory=dict)

    @field_validator("root")
    @classmethod
    def _check_root(cls, value: str) -> str:
        return check_no_control_chars(value, field="root")


class RetentionConfig(StrictModel):
    """Per-environment retention knobs (apps.yaml).

    ``successful_releases`` is a MANUAL cleanup reference only: no code
    executes it — the administrator keeps that many historical successful
    releases' artifacts and reclaims older ones by hand via the target's
    Docker workflow (OPERATIONS.md §5.3).  ``job_logs_days`` drives the
    automated spool log cleanup.
    """

    successful_releases: int = Field(default=5, ge=1, le=100)
    job_logs_days: int = Field(default=7, ge=1, le=365)


class NpuConfig(StrictModel):
    enabled: bool = False
    vendor: str = ""
    model: str = ""
    driver: str = ""
    executable: str = ""
    argv: list[str] = Field(default_factory=list)
    devices: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=10, ge=1, le=60)

    @model_validator(mode="after")
    def _check(self) -> NpuConfig:
        if self.enabled:
            if not (self.vendor and self.model and self.executable and self.argv):
                raise ValueError("enabled NPU requires vendor/model/executable/argv")
            if any(not element.startswith("-") and "*" in element for element in self.argv):
                raise ValueError("NPU argv must not contain wildcard patterns")
        return self


class EnvironmentConfig(StrictModel):
    runtime: Literal["compose", "simulation"]
    project_name: str = Field(pattern=_SERVICE_RE.pattern)
    build_profile: str = Field(pattern=_ID_RE.pattern)
    buildkit_socket: str = ""
    deploy_root: str
    compose_file: str
    health_checks: list[HealthCheckConfig] = Field(min_length=1)
    test_suites: list[str] = Field(default_factory=list)
    test_runner: dict[str, TestSuiteConfig] = Field(default_factory=dict)
    restartable_services: list[str] = Field(default_factory=list)
    services: list[str] = Field(min_length=1)
    diagnostics: DiagnosticsConfig | None = None
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    disk_budget_bytes: int = Field(default=10 * 1024**3, ge=1024**3)

    @model_validator(mode="after")
    def _check(self) -> EnvironmentConfig:
        unknown_suites = set(self.test_runner) - set(self.test_suites)
        if unknown_suites:
            declared = sorted(unknown_suites)
            raise ValueError(f"test_runner suites not declared in test_suites: {declared}")
        if len(set(self.services)) != len(self.services):
            raise ValueError("duplicate service names")
        outside = set(self.restartable_services) - set(self.services)
        if outside:
            raise ValueError(f"restartable_services outside registered services: {sorted(outside)}")
        return self


class BuildProfileConfig(StrictModel):
    context: str = Field(default=".", pattern=r"\.|[A-Za-z0-9_][A-Za-z0-9_.-]*")
    dockerfile_basename: str = Field(default="Dockerfile", pattern=r"[A-Za-z0-9_.-]{1,128}")
    platform: str = Field(pattern=r"linux/[a-z0-9_/]+")
    timeout_seconds: int = Field(default=900, ge=1, le=3600)
    max_parallel: int = Field(default=1, ge=1, le=4)


class AppConfig(StrictModel):
    git: GitConfig
    environments: dict[str, EnvironmentConfig]
    npu: NpuConfig = Field(default_factory=NpuConfig)

    @model_validator(mode="after")
    def _check(self) -> AppConfig:
        if not self.environments:
            raise ValueError("app must register at least one environment")
        return self


class AppsConfigFile(StrictModel):
    schema_version: Literal[1]
    apps: dict[str, AppConfig]
    build_profiles: dict[str, BuildProfileConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> AppsConfigFile:
        bad = [name for name in self.apps if not _ID_RE.fullmatch(name)]
        if bad:
            raise ValueError(f"invalid app ids: {bad}")
        return self


class OperationsConfigFile(StrictModel):
    schema_version: Literal[1]
    operations: dict[str, OperationConfig]

    @model_validator(mode="after")
    def _check(self) -> OperationsConfigFile:
        bad = [name for name in self.operations if not _ID_RE.fullmatch(name)]
        if bad:
            raise ValueError(f"invalid operation ids: {bad}")
        return self


class WorkflowsConfigFile(StrictModel):
    schema_version: Literal[1]
    workflows: dict[str, WorkflowConfig]

    @model_validator(mode="after")
    def _check(self) -> WorkflowsConfigFile:
        bad = [name for name in self.workflows if not _ID_RE.fullmatch(name)]
        if bad:
            raise ValueError(f"invalid workflow ids: {bad}")
        return self


# ---------------------------------------------------------------------------
# Server configuration (drawbridge.yaml)
# ---------------------------------------------------------------------------


class AuthConfig(StrictModel):
    mode: Literal["none", "token"] = "none"
    token_file: str = ""


class ConcurrencyConfig(StrictModel):
    """Admission and execution concurrency knobs.

    ``max_read_requests`` caps the diagnostic channel: the Gateway rejects a
    new read request with BUSY while ``max_read_requests`` admitted-but-not-
    terminal diagnostic jobs exist (queued or running, counted from the
    store — quota recycles on terminal states and survives gateway
    restarts).  Diagnostic jobs never consume the mutation capacity below.
    """

    max_read_requests: int = Field(default=16, ge=1, le=256)
    max_running_jobs: int = Field(default=1, ge=1, le=8)
    max_queued_jobs: int = Field(default=50, ge=1, le=1000)
    max_queued_jobs_per_target: int = Field(default=5, ge=1, le=100)
    queue_timeout_seconds: int = Field(default=600, ge=1, le=86400)
    min_deploy_interval_seconds: int = Field(default=60, ge=0, le=86400)


class DiagnosticsRuntimeConfig(StrictModel):
    max_concurrent: int = Field(default=16, ge=1, le=64)
    retention_seconds: int = Field(default=86400, ge=3600, le=7 * 86400)
    wait_budget_seconds: int = Field(default=25, ge=1, le=120)


class OutputLimitsConfig(StrictModel):
    """Output budget knobs (MVP spec §3).

    Enforced per step: ``query_summary_max_bytes`` bounds result summaries,
    ``log_result_*`` bound paged log fetches, ``step_log_hard_limit_bytes``
    terminates a step whose output exceeds it.

    Reserved, NOT enforced by code (plan D9, declared downgrade):
    ``step_log_soft_limit_bytes`` and ``job_log_hard_limit_bytes`` have no
    consumer — the frozen deploy_verify workflow spools at most 3 steps
    (build / deploy / restore), each already capped by the per-step hard
    limit, so a 3 x 20 MiB = 60 MiB worst case never reaches the 100 MiB
    job ceiling and per-step soft-warning surface is unreachable.  The
    fields stay for schema stability and sanity ordering (job >= step);
    if a future workflow widens the spool-step count, wire them into the
    step executor before relying on them.  Deviation recorded in
    UPGRADED_ARCHITECTURE §4.
    """

    query_summary_max_bytes: int = Field(default=65536, ge=1024, le=1024 * 1024)
    log_result_max_lines: int = Field(default=200, ge=1, le=1000)
    log_result_max_bytes: int = Field(default=262144, ge=1024, le=4 * 1024 * 1024)
    step_log_soft_limit_bytes: int = Field(default=1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    step_log_hard_limit_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    job_log_hard_limit_bytes: int = Field(
        default=100 * 1024 * 1024, ge=1024, le=8 * 1024 * 1024 * 1024
    )

    @model_validator(mode="after")
    def _check(self) -> OutputLimitsConfig:
        if self.job_log_hard_limit_bytes < self.step_log_hard_limit_bytes:
            raise ValueError("job log budget must not be below the per-step hard budget")
        return self


class ToolchainConfig(StrictModel):
    git: str = "/usr/bin/git"
    docker: str = "/usr/bin/docker"
    ps: str = "/usr/bin/ps"
    buildctl: str = "/usr/local/bin/buildctl"

    @field_validator("git", "docker", "ps", "buildctl")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not _is_absolute_path(value):
            raise ValueError("toolchain paths must be absolute")
        return check_no_control_chars(value, field="toolchain path")


#: Environment variable names a profile may never inject.
FORBIDDEN_PROFILE_ENV_KEYS = frozenset(
    {
        "BASH_ENV",
        "ENV",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "GIT_CONFIG_COUNT",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "PATH",
        "HOME",
    }
)

_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ProfileEnvConfig(StrictModel):
    """Per-profile registered environment (MVP spec §3: HOME/socket 等)."""

    home: str = ""
    extra_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("extra_env")
    @classmethod
    def _check_env(cls, value: dict[str, str]) -> dict[str, str]:
        for key, env_value in value.items():
            if not _ENV_KEY_RE.fullmatch(key):
                raise ValueError(f"invalid environment variable name {key!r}")
            if key.upper() in FORBIDDEN_PROFILE_ENV_KEYS:
                raise ValueError(f"environment variable {key!r} may not be set by profile config")
            check_no_control_chars(env_value, field=f"environment value for {key}")
        return value

    @field_validator("home")
    @classmethod
    def _check_home(cls, value: str) -> str:
        if not value:
            return value
        return check_no_control_chars(value, field="profile home")


class ServerConfig(StrictModel):
    bind_address: str = "0.0.0.0"  # noqa: S104 - config value, not a literal bind
    port: int = Field(default=8787, ge=1, le=65535)
    base_path: str = Field(default="/mcp", pattern=r"/[a-zA-Z0-9_./-]{0,64}")
    allowed_cidrs: list[str] = Field(default_factory=lambda: ["192.168.0.0/16"])
    allowed_origins: list[str] = Field(min_length=1)
    allowed_hosts: list[str] = Field(min_length=1)
    auth: AuthConfig = Field(default_factory=AuthConfig)

    @field_validator("allowed_cidrs")
    @classmethod
    def _check_cidrs(cls, value: list[str]) -> list[str]:
        import ipaddress

        for cidr in value:
            ipaddress.ip_network(cidr, strict=False)
        return value


class PathsConfig(StrictModel):
    state_dir: str = "/var/lib/drawbridge"
    lock_dir: str = "/run/drawbridge/locks"
    log_dir: str = "/var/log/drawbridge"
    config_dir: str = "/etc/drawbridge"

    @field_validator("state_dir", "lock_dir", "log_dir", "config_dir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not _is_absolute_path(value):
            raise ValueError("paths must be absolute")
        return check_no_control_chars(value, field="path")


class MaintenanceConfig(StrictModel):
    enabled: bool = False


class RetentionPolicyConfig(StrictModel):
    """Retention enforcement knobs (MVP spec §8 / OPERATIONS.md §5).

    Diagnostic jobs keep using ``diagnostics.retention_seconds``; spooled
    job log directories keep using each app's ``retention.job_logs_days``.
    Release and artifact rows are audit history and are never deleted by
    the automated cleanup.
    """

    cleanup_interval_seconds: int = Field(default=3600, ge=60, le=86400)
    idempotency_key_days: int = Field(default=7, ge=1, le=365)
    plan_days: int = Field(default=7, ge=1, le=365)
    job_record_days: int = Field(default=7, ge=1, le=365)


class RecoveryConfig(StrictModel):
    """Startup reconciliation knobs (MVP spec §8: 不明现场进入 NeedsAttention).

    A running job whose heartbeat is older than ``stale_running_job_seconds``
    is flipped to ``needs_attention`` at Runner startup (never re-run and
    never silently taken over).  The default leaves a 180x margin over the
    5-second heartbeat cadence.  Single-Runner deployment assumed (the
    instance flock enforces it on one host).
    """

    stale_running_job_seconds: int = Field(default=900, ge=60, le=86400)


class MainConfig(StrictModel):
    schema_version: Literal[1]
    server: ServerConfig
    paths: PathsConfig = Field(default_factory=PathsConfig)
    toolchain: ToolchainConfig = Field(default_factory=ToolchainConfig)
    profile_env: dict[ExecutionProfile, ProfileEnvConfig] = Field(default_factory=dict)

    @field_validator("profile_env", mode="before")
    @classmethod
    def _coerce_profile_keys(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                (ExecutionProfile(k) if isinstance(k, str) else k): v
                for k, v in value.items()
            }
        return value

    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    diagnostics: DiagnosticsRuntimeConfig = Field(default_factory=DiagnosticsRuntimeConfig)
    output: OutputLimitsConfig = Field(default_factory=OutputLimitsConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    retention: RetentionPolicyConfig = Field(default_factory=RetentionPolicyConfig)
    recovery: RecoveryConfig = Field(default_factory=RecoveryConfig)


# ---------------------------------------------------------------------------
# Aggregate with digest support
# ---------------------------------------------------------------------------


class DrawbridgeConfig(StrictModel):
    """Fully loaded and cross-validated configuration bundle."""

    main: MainConfig
    apps: dict[str, AppConfig]
    build_profiles: dict[str, BuildProfileConfig] = Field(default_factory=dict)
    operations: dict[str, OperationConfig]
    workflows: dict[str, WorkflowConfig]
    digest: str

    def build_profile(self, name: str) -> BuildProfileConfig:
        try:
            return self.build_profiles[name]
        except KeyError:
            from drawbridge.errors import ConfigInvalidError

            raise ConfigInvalidError(
                f"build profile {name!r} is not registered in build_profiles"
            ) from None

    def app(self, app_id: str) -> AppConfig:
        try:
            return self.apps[app_id]
        except KeyError:
            from drawbridge.errors import DrawbridgeError, ErrorCode

            raise DrawbridgeError(f"unknown app {app_id!r}", code=ErrorCode.UNKNOWN_APP) from None

    def environment(self, app_id: str, environment: str) -> EnvironmentConfig:
        app = self.app(app_id)
        try:
            return app.environments[environment]
        except KeyError:
            from drawbridge.errors import DrawbridgeError, ErrorCode

            raise DrawbridgeError(
                f"unknown environment {environment!r} for app {app_id!r}",
                code=ErrorCode.UNKNOWN_ENVIRONMENT,
            ) from None

    def to_static_dict(self) -> dict[str, Any]:
        """Configuration snapshot used for digests/auditing (no secrets)."""
        return {
            "digest": self.digest,
            "apps": sorted(self.apps),
            "operations": sorted(self.operations),
            "workflows": sorted(self.workflows),
        }
