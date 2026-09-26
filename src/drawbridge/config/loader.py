"""YAML loading for Drawbridge configuration.

Rules (MVP spec §6): YAML ``safe_load`` only, duplicate keys are a hard
error, custom objects/tags are rejected, and the aggregate configuration is
bound to a SHA-256 digest so plans/jobs can freeze the exact configuration
they were validated against.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, TextIO, TypeVar

from pydantic import BaseModel

from drawbridge.config.compose_template import read_compose_template
from drawbridge.config.models import (
    AppsConfigFile,
    DrawbridgeConfig,
    MainConfig,
    OperationsConfigFile,
    WorkflowsConfigFile,
)
from drawbridge.config.yamlstrict import load_yaml_file
from drawbridge.errors import ConfigInvalidError, DrawbridgeError

BaseModelT = TypeVar("BaseModelT", bound="BaseModel")


def _load_yaml_file(path: Path) -> Any:
    return load_yaml_file(path)


def _parse_model[BaseModelT: BaseModel](
    model_class: type[BaseModelT], data: Any, source: str
) -> BaseModelT:
    import pydantic

    try:
        return model_class.model_validate(data)
    except pydantic.ValidationError as exc:
        raise ConfigInvalidError(f"invalid configuration in {source}: {exc}") from exc


def load_yaml(path: str | Path) -> Any:
    return _load_yaml_file(Path(path))


def load_main_config(path: str | Path) -> MainConfig:
    return _parse_model(MainConfig, _load_yaml_file(Path(path)), str(path))


def load_apps_config(path: str | Path) -> AppsConfigFile:
    return _parse_model(AppsConfigFile, _load_yaml_file(Path(path)), str(path))


def load_operations_config(path: str | Path) -> OperationsConfigFile:
    return _parse_model(OperationsConfigFile, _load_yaml_file(Path(path)), str(path))


def load_workflows_config(path: str | Path) -> WorkflowsConfigFile:
    return _parse_model(WorkflowsConfigFile, _load_yaml_file(Path(path)), str(path))


def load_config_bundle(
    main_path: str | Path,
    apps_path: str | Path,
    operations_path: str | Path,
    workflows_path: str | Path,
) -> DrawbridgeConfig:
    """Load all four configuration files and cross-validate references."""
    main = load_main_config(main_path)
    apps_file = load_apps_config(apps_path)
    operations_file = load_operations_config(operations_path)
    workflows_file = load_workflows_config(workflows_path)

    _cross_validate(
        apps_file.apps,
        operations_file.operations,
        workflows_file.workflows,
        build_profiles=apps_file.build_profiles,
    )
    _validate_compose_templates(apps_file.apps)

    digest_source = json.dumps(
        {
            "main": main.model_dump(mode="json"),
            "apps": apps_file.model_dump(mode="json"),
            "operations": operations_file.model_dump(mode="json"),
            "workflows": workflows_file.model_dump(mode="json"),
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()

    return DrawbridgeConfig(
        main=main,
        apps=apps_file.apps,
        build_profiles=apps_file.build_profiles,
        operations=operations_file.operations,
        workflows=workflows_file.workflows,
        digest=digest,
    )


def _cross_validate(
    apps: dict[str, Any],
    operations: dict[str, Any],
    workflows: dict[str, Any],
    *,
    build_profiles: dict[str, Any] | None = None,
) -> None:
    """Reference checks that span several files."""
    for app_id, app in apps.items():
        for env_name, env in app.environments.items():
            profile = getattr(env, "build_profile", None)
            if profile is not None and profile not in (build_profiles or {}):
                raise ConfigInvalidError(
                    f"app {app_id!r} environment {env_name!r} references unknown "
                    f"build profile {profile!r}"
                )
    for workflow_name, workflow in workflows.items():
        for step in workflow.steps:
            if step.operation not in operations:
                raise ConfigInvalidError(
                    f"workflow {workflow_name!r} step {step.id!r} references "
                    f"unknown operation {step.operation!r}"
                )
    # workflow steps must only reference internal (public=false) operations:
    # they are engine-internal and bypass the public gate.
    for workflow_name, workflow in workflows.items():
        for step in workflow.steps:
            operation = operations[step.operation]
            if operation.public:
                raise ConfigInvalidError(
                    f"workflow {workflow_name!r} step {step.id!r} references public "
                    f"operation {step.operation!r}; workflow steps must be internal"
                )


def _validate_compose_templates(apps: dict[str, Any]) -> None:
    """Load-time structural validation of every registered compose template.

    On the deployment target (gateway/runner) the template always exists and
    a corrupt one fails startup — the fail-fast contract of plan D3.  A
    MISSING file is deliberately deferred to plan/preflight time: development
    hosts routinely load the repository's sample ``configs/`` bundle whose
    ``compose_file`` points at the production ``/etc/drawbridge`` path, and
    ``read_compose_template`` rejects a missing file wherever a plan is
    created or applied (runner release_plan handler, gateway ops_release_apply,
    deploy re-validation).
    """
    for _app_id, app in apps.items():
        for _env_name, env in app.environments.items():
            if not Path(env.compose_file).exists():
                continue
            read_compose_template(env.compose_file, list(env.services))


def load_config_from_dir(config_dir: str | Path) -> DrawbridgeConfig:
    d = Path(config_dir)
    return load_config_bundle(
        d / "drawbridge.yaml",
        d / "apps.yaml",
        d / "operations.yaml",
        d / "workflows.yaml",
    )


def dump_normalized(fh: TextIO, data: Any) -> None:
    """Helper for admin tooling: normalized JSON dump of parsed config."""
    json.dump(data, fh, sort_keys=True, ensure_ascii=True, indent=2)


__all__ = [
    "DrawbridgeConfig",
    "DrawbridgeError",
    "dump_normalized",
    "load_apps_config",
    "load_config_bundle",
    "load_config_from_dir",
    "load_main_config",
    "load_operations_config",
    "load_workflows_config",
    "load_yaml",
]
