"""Strict configuration model tests (MVP spec §2, §6)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from drawbridge.config.models import (
    AppConfig,
    EnvironmentConfig,
    HealthCheckConfig,
    MainConfig,
    OperationConfig,
    OperationsConfigFile,
    OutputBudget,
    ParameterSpec,
    WorkflowsConfigFile,
)


def operation_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "executable": "git",
        "argv": ["status", "--porcelain=v1"],
        "execution_profile": "source_manage",
        "public": True,
        "access": "read",
        "timeout_seconds": 10,
    }
    base.update(overrides)
    return base


class TestOperationConfig:
    def test_minimal_executable_operation(self) -> None:
        op = OperationConfig.model_validate(operation_kwargs())
        assert op.executable == "git"
        assert op.accepted_exit_codes == [0]
        assert op.output.max_bytes == 65536

    def test_handler_and_executable_mutually_exclusive(self) -> None:
        with pytest.raises(ValidationError):
            OperationConfig.model_validate(operation_kwargs(handler="compose_status"))

    def test_nothing_at_all_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperationConfig.model_validate(operation_kwargs(executable=None))

    def test_unknown_handler_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperationConfig.model_validate(
                operation_kwargs(executable=None, handler="do_anything")
            )

    def test_handler_may_not_declare_argv(self) -> None:
        with pytest.raises(ValidationError):
            OperationConfig.model_validate(
                operation_kwargs(
                    executable=None,
                    handler="compose_status",
                    argv=["ps"],
                    cwd_from="release.dir",
                )
            )

    def test_unknown_argv_prefix_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperationConfig.model_validate(operation_kwargs(argv_prefix="docker_anything"))

    def test_git_safe_prefix_accepted(self) -> None:
        op = OperationConfig.model_validate(operation_kwargs(argv_prefix="git_safe"))
        assert op.argv_prefix == "git_safe"

    def test_prepare_must_be_preset(self) -> None:
        with pytest.raises(ValidationError):
            OperationConfig.model_validate(
                operation_kwargs(prepare="import_module(os)")
            )

    def test_argv_placeholder_contexts(self) -> None:
        ok = OperationConfig.model_validate(
            operation_kwargs(
                argv=[
                    "log",
                    "--max-count={param.count}",
                    "{job.resolved_sha}",
                    "{app.repo_path}",
                    "{release.dir}",
                ]
            )
        )
        assert len(ok.argv) == 5

    def test_argv_multiple_placeholders_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperationConfig.model_validate(
                operation_kwargs(argv=["{param.a}{param.b}"])
            )

    def test_argv_unknown_context_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperationConfig.model_validate(
                operation_kwargs(argv=["{request.argv}"])
            )

    def test_argv_expression_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperationConfig.model_validate(
                operation_kwargs(argv=["--max-count={param.count + 1}"])
            )

    def test_argv_bare_braces_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperationConfig.model_validate(operation_kwargs(argv=["{}"]))

    def test_empty_argv_element_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperationConfig.model_validate(operation_kwargs(argv=[""]))

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperationConfig.model_validate(operation_kwargs(shell="/bin/bash"))

    def test_unknown_semantic_validator_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperationConfig.model_validate(
                operation_kwargs(
                    parameters={
                        "ref": ParameterSpec.model_validate(
                            {
                                "type": "string",
                                "validators": ["become_root"],
                            }
                        )
                    }
                )
            )


class TestParameterSpec:
    def test_integer_defaults(self) -> None:
        spec = ParameterSpec.model_validate(
            {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}
        )
        assert spec.default == 20

    def test_bool_default_rejected_for_integer(self) -> None:
        with pytest.raises(ValidationError):
            ParameterSpec.model_validate(
                {"type": "integer", "default": True, "minimum": 1, "maximum": 2}
            )

    def test_string_default_with_control_char_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ParameterSpec.model_validate(
                {"type": "string", "default": "a\nb", "max_length": 10}
            )

    def test_mismatched_bounds_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ParameterSpec.model_validate({"type": "integer", "minimum": 10, "maximum": 1})

    def test_non_ascii_pattern_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ParameterSpec.model_validate(
                {"type": "string", "pattern": r"[ä-z]+", "max_length": 10}
            )

    def test_oversized_pattern_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ParameterSpec.model_validate(
                {"type": "string", "pattern": "a" * 2000, "max_length": 10}
            )


class TestAppConfig:
    def environment_kwargs(self) -> dict[str, object]:
        return {
            "runtime": "compose",
            "project_name": "demo-staging",
            "build_profile": "demo",
            "deploy_root": "/srv/drawbridge/apps/demo/staging",
            "compose_file": "/etc/drawbridge/compose/demo.staging.yaml",
            "health_checks": [
                HealthCheckConfig.model_validate(
                    {
                        "type": "http",
                        "url": "http://127.0.0.1:18080/healthz",
                    }
                )
            ],
            "services": ["api"],
            "restartable_services": ["api"],
            "test_suites": ["smoke"],
            "test_runner": {
                "smoke": {
                    "image_id": "sha256:aaa",
                    "entrypoint": "/opt/smoke",
                    "network": "staging-app-only",
                }
            },
        }

    def app_kwargs(self) -> dict[str, object]:
        return {
            "git": {
                "repo_path": "/srv/drawbridge/repos/demo",
                "origin": "git@github.com:acme/demo.git",
                "allowed_ref_patterns": ["^refs/heads/main$"],
            },
            "environments": {"staging": self.environment_kwargs()},
        }

    def test_valid_app(self) -> None:
        app = AppConfig.model_validate(self.app_kwargs())
        assert "staging" in app.environments
        assert app.npu.enabled is False

    def test_restartable_outside_services_rejected(self) -> None:
        kwargs = self.app_kwargs()
        kwargs["environments"] = {
            "staging": {**self.environment_kwargs(), "restartable_services": ["worker"]}
        }
        with pytest.raises(ValidationError):
            AppConfig.model_validate(kwargs)

    def test_unknown_suite_in_test_runner_rejected(self) -> None:
        env = self.environment_kwargs()
        env["test_suites"] = []
        kwargs = self.app_kwargs()
        kwargs["environments"] = {"staging": env}
        with pytest.raises(ValidationError):
            AppConfig.model_validate(kwargs)

    def test_npu_enabled_requires_fields(self) -> None:
        kwargs = self.app_kwargs()
        kwargs["npu"] = {"enabled": True, "vendor": "huawei"}
        with pytest.raises(ValidationError):
            AppConfig.model_validate(kwargs)

    def test_npu_wildcard_argv_rejected(self) -> None:
        kwargs = self.app_kwargs()
        kwargs["npu"] = {
            "enabled": True,
            "vendor": "huawei",
            "model": "910B",
            "executable": "/usr/local/bin/npu-smi",
            "argv": ["info", ".*"],
        }
        with pytest.raises(ValidationError):
            AppConfig.model_validate(kwargs)


class TestFileModels:
    def test_operations_file_duplicate_app_ids_caught_by_loader(self) -> None:
        ops = OperationsConfigFile.model_validate(
            {
                "schema_version": 1,
                "operations": {"git_status": operation_kwargs()},
            }
        )
        assert "git_status" in ops.operations

    def test_invalid_operation_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperationsConfigFile.model_validate(
                {
                    "schema_version": 1,
                    "operations": {"Git Status!": operation_kwargs()},
                }
            )

    def test_schema_version_literal(self) -> None:
        with pytest.raises(ValidationError):
            OperationsConfigFile.model_validate(
                {"schema_version": 2, "operations": {}}
            )

    def test_workflow_may_be_defined(self) -> None:
        wf = WorkflowsConfigFile.model_validate(
            {
                "schema_version": 1,
                "workflows": {
                    "deploy_verify": {
                        "timeout_seconds": 1800,
                        "steps": [
                            {"id": "preflight", "operation": "release_preflight"}
                        ],
                    }
                },
            }
        )
        assert wf.workflows["deploy_verify"].recovery_timeout_seconds == 300

    def test_main_config_requires_origins_and_hosts(self) -> None:
        with pytest.raises(ValidationError):
            MainConfig.model_validate(
                {
                    "schema_version": 1,
                    "server": {
                        "allowed_cidrs": ["127.0.0.0/8"],
                        "allowed_origins": [],
                        "allowed_hosts": ["drawbridge.internal"],
                    },
                }
            )

    def test_main_config_defaults(self) -> None:
        cfg = MainConfig.model_validate(
            {
                "schema_version": 1,
                "server": {
                    "allowed_cidrs": ["192.168.0.0/16"],
                    "allowed_origins": ["http://localhost:8787"],
                    "allowed_hosts": ["drawbridge.internal"],
                },
            }
        )
        assert cfg.server.port == 8787
        assert cfg.concurrency.max_running_jobs == 1
        assert cfg.output.log_result_max_lines == 200

    def test_invalid_cidr_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MainConfig.model_validate(
                {
                    "schema_version": 1,
                    "server": {
                        "allowed_cidrs": ["not-a-cidr"],
                        "allowed_origins": ["http://localhost:8787"],
                        "allowed_hosts": ["drawbridge.internal"],
                    },
                }
            )

    def test_profile_env_forbidden_keys_rejected(self) -> None:
        from drawbridge.config.models import ProfileEnvConfig

        with pytest.raises(ValidationError):
            ProfileEnvConfig.model_validate(
                {"extra_env": {"DOCKER_HOST": "unix:///tmp/evil.sock"}}
            )

    def test_output_budget_configurable(self) -> None:
        budget = OutputBudget.model_validate({"policy": "spool", "max_bytes": 1000})
        assert budget.policy.value == "spool"


class TestEnvironmentBudget:
    def test_job_budget_must_cover_step_budget(self) -> None:
        from drawbridge.config.models import OutputLimitsConfig

        with pytest.raises(ValidationError):
            OutputLimitsConfig.model_validate(
                {"step_log_hard_limit_bytes": 1024 * 1024 * 1024}
            )

    def test_reserved_budgets_stay_declared_and_ordered(self) -> None:
        """Plan D9 declared downgrade: step soft / job hard limits keep their
        schema slots and sanity ordering even though no code consumes them
        (frozen deploy_verify spools at most 3 steps x 20 MiB = 60 MiB)."""
        from drawbridge.config.models import OutputLimitsConfig

        config = OutputLimitsConfig.model_validate(
            {
                "step_log_soft_limit_bytes": 2048,
                "step_log_hard_limit_bytes": 4096,
                "job_log_hard_limit_bytes": 8192,
            }
        )
        assert config.step_log_soft_limit_bytes == 2048
        assert config.job_log_hard_limit_bytes == 8192
        assert "NOT enforced by code" in OutputLimitsConfig.__doc__


class TestEnvConfigTypes:
    def test_environment_requires_compose(self) -> None:
        base = {
            "runtime": "systemd",
            "project_name": "x",
            "build_profile": "x",
            "deploy_root": "/srv",
            "compose_file": "/etc/x.yaml",
            "health_checks": [],
            "services": ["api"],
        }
        with pytest.raises(ValidationError):
            EnvironmentConfig.model_validate(base)
