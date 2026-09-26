"""Loader tests: YAML safety, duplicate keys, digest stability."""

from __future__ import annotations

from pathlib import Path

import pytest

from drawbridge.config.loader import (
    load_apps_config,
    load_config_bundle,
    load_yaml,
)
from drawbridge.config.yamlstrict import StrictLoader as _StrictLoader
from drawbridge.errors import ConfigInvalidError

MAIN_YAML = """
schema_version: 1
server:
  bind_address: 0.0.0.0
  port: 8787
  allowed_cidrs: [192.168.0.0/16]
  allowed_origins: ["http://localhost:8787"]
  allowed_hosts: ["drawbridge.internal"]
  auth:
    mode: none
"""

APPS_YAML = """
schema_version: 1
apps:
  demo:
    git:
      repo_path: /srv/drawbridge/repos/demo
      origin: git@github.com:acme/demo.git
      allowed_ref_patterns:
        - '^refs/heads/main$'
        - '^refs/heads/agent/[A-Za-z0-9_-]+$'
    environments:
      staging:
        runtime: compose
        project_name: drawbridge-demo-staging
        build_profile: demo
        deploy_root: /srv/drawbridge/apps/demo/staging
        build_output_dir: /srv/drawbridge/build-output/demo/staging
        compose_file: /etc/drawbridge/compose/demo.staging.yaml
        health_checks:
          - type: http
            url: http://127.0.0.1:18080/healthz
        services: [api, worker]
        restartable_services: [api]

build_profiles:
  demo:
    context: .
    dockerfile_basename: Dockerfile
    platform: linux/arm64
    timeout_seconds: 900
"""

OPERATIONS_YAML = """
schema_version: 1
operations:
  git_status:
    executable: git
    argv: [status, "--porcelain=v1", "--untracked-files=no"]
    cwd_from: app.repo_path
    execution_profile: source_manage
    public: true
    access: read
    timeout_seconds: 10
  release_preflight:
    handler: release_preflight
    execution_profile: runtime_manage
    public: false
    access: read
    timeout_seconds: 15
"""

WORKFLOWS_YAML = """
schema_version: 1
workflows:
  deploy_verify:
    requires_plan: true
    timeout_seconds: 1800
    recovery_timeout_seconds: 300
    steps:
      - {id: preflight, operation: release_preflight}
"""


def write_files(tmp_path: Path) -> dict[str, Path]:
    files = {
        "main": tmp_path / "drawbridge.yaml",
        "apps": tmp_path / "apps.yaml",
        "ops": tmp_path / "operations.yaml",
        "wf": tmp_path / "workflows.yaml",
    }
    files["main"].write_text(MAIN_YAML, encoding="utf-8")
    files["apps"].write_text(APPS_YAML, encoding="utf-8")
    files["ops"].write_text(OPERATIONS_YAML, encoding="utf-8")
    files["wf"].write_text(WORKFLOWS_YAML, encoding="utf-8")
    return files


def test_load_bundle_ok(tmp_path: Path) -> None:
    f = write_files(tmp_path)
    bundle = load_config_bundle(f["main"], f["apps"], f["ops"], f["wf"])
    assert "demo" in bundle.apps
    assert "git_status" in bundle.operations
    assert bundle.workflows["deploy_verify"].steps[0].operation == "release_preflight"
    assert len(bundle.digest) == 64


def test_digest_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    f = write_files(tmp_path)
    bundle_a = load_config_bundle(f["main"], f["apps"], f["ops"], f["wf"])
    bundle_b = load_config_bundle(f["main"], f["apps"], f["ops"], f["wf"])
    assert bundle_a.digest == bundle_b.digest

    text = f["ops"].read_text(encoding="utf-8")
    f["ops"].write_text(
        text.replace("timeout_seconds: 10", "timeout_seconds: 11"), encoding="utf-8"
    )
    bundle_c = load_config_bundle(f["main"], f["apps"], f["ops"], f["wf"])
    assert bundle_a.digest != bundle_c.digest


def test_duplicate_yaml_key_rejected(tmp_path: Path) -> None:
    f = write_files(tmp_path)
    f["apps"].write_text(
        APPS_YAML + "    runtime: compose\n", encoding="utf-8"
    )
    with pytest.raises(ConfigInvalidError, match="duplicate|invalid YAML|invalid configuration"):
        load_apps_config(f["apps"])


def test_duplicate_top_level_key_rejected(tmp_path: Path) -> None:
    f = write_files(tmp_path)
    f["main"].write_text(MAIN_YAML + "  port: 9999\n", encoding="utf-8")
    with pytest.raises(ConfigInvalidError, match="duplicate"):
        load_yaml(f["main"])


def test_missing_file_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigInvalidError, match="not found"):
        load_apps_config(tmp_path / "missing.yaml")


def test_workflow_referencing_unknown_operation_rejected(tmp_path: Path) -> None:
    f = write_files(tmp_path)
    f["wf"].write_text(
        WORKFLOWS_YAML.replace(
            "operation: release_preflight", "operation: does_not_exist"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigInvalidError, match="unknown operation"):
        load_config_bundle(f["main"], f["apps"], f["ops"], f["wf"])


def test_workflow_referencing_public_operation_rejected(tmp_path: Path) -> None:
    f = write_files(tmp_path)
    f["wf"].write_text(
        WORKFLOWS_YAML.replace("operation: release_preflight", "operation: git_status"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigInvalidError, match="public"):
        load_config_bundle(f["main"], f["apps"], f["ops"], f["wf"])


def test_yaml_tag_injection_rejected(tmp_path: Path) -> None:
    payload = "schema_version: !!python/object/apply:os.system ['echo hi']"
    p = tmp_path / "evil.yaml"
    p.write_text(payload, encoding="utf-8")
    with pytest.raises(ConfigInvalidError):
        load_yaml(p)


def test_strict_loader_is_safe_loader_subclass() -> None:
    from yaml import SafeLoader

    assert issubclass(_StrictLoader, SafeLoader)
