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

import yaml
from pydantic import BaseModel

from drawbridge.config.models import (
    AppsConfigFile,
    DrawbridgeConfig,
    MainConfig,
    OperationsConfigFile,
    WorkflowsConfigFile,
)
from drawbridge.errors import ConfigInvalidError, DrawbridgeError


class _StrictLoader(yaml.SafeLoader):  # type: ignore[misc]  # yaml is untyped
    """SafeLoader that rejects duplicate mapping keys."""


def _no_duplicates_constructor(
    loader: yaml.Loader, node: yaml.Node, deep: bool = False
) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError:
            raise yaml.constructor.ConstructorError(
                None, None, f"unhashable mapping key: {key!r}", key_node.start_mark
            ) from None
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate mapping key: {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


BaseModelT = TypeVar("BaseModelT", bound="BaseModel")


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates_constructor
)


def _load_yaml_file(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.load(fh, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass, dup-key guard
    except FileNotFoundError:
        raise ConfigInvalidError(f"configuration file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigInvalidError(f"invalid YAML in {path}: {exc}") from None
    except OSError as exc:
        raise ConfigInvalidError(f"cannot read {path}: {exc}") from None


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

    _cross_validate(apps_file.apps, operations_file.operations, workflows_file.workflows)

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
        operations=operations_file.operations,
        workflows=workflows_file.workflows,
        digest=digest,
    )


def _cross_validate(
    apps: dict[str, Any], operations: dict[str, Any], workflows: dict[str, Any]
) -> None:
    """Reference checks that span several files."""
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
