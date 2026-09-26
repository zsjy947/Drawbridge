"""DeployRuntime pure builders and target-host gating tests.

The argv builders are pure functions — verified exactly here on development
hosts; the executing steps themselves require the Linux target (910B) and
are covered by on-target acceptance (MVP spec §10).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from drawbridge.config.loader import load_config_from_dir
from drawbridge.errors import DrawbridgeError
from drawbridge.runner.runtime import (
    DeployRuntime,
    buildctl_argv,
    compose_env_file,
    compose_prefix,
    compose_stop_argv,
    compose_up_argv,
    parse_compose_ps_images,
    parse_wait_exit_code,
    render_compose_text,
    scan_dockerfile_directives,
    unique_image_tag,
)
from drawbridge.runner.runtime import (
    test_container_create_argv as build_test_create_argv,
)
from drawbridge.state.db import Database
from drawbridge.state.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"

_IMAGE = "sha256:" + "a" * 64


class TestArgvBuilders:
    def test_compose_prefix_fixed_shape(self) -> None:
        prefix = compose_prefix(
            "proj",
            "/srv/deploy",
            "/srv/deploy/compose.rendered.yaml",
            "/etc/drawbridge/compose/empty.env",
        )
        assert prefix == [
            "compose", "--ansi", "never",
            "--project-name", "proj",
            "--project-directory", "/srv/deploy",
            "--env-file", "/etc/drawbridge/compose/empty.env",
            "-f", "/srv/deploy/compose.rendered.yaml",
        ]

    def test_compose_env_file_resolves_from_config_dir(self) -> None:
        """D13: the --env-file literal resolves from paths.config_dir —
        standard deployments keep the /etc path, anchored deployments follow
        the config skeleton."""
        assert (
            compose_env_file("/etc/drawbridge") == "/etc/drawbridge/compose/empty.env"
        )
        assert compose_env_file("D:/run/etc") == "D:/run/etc/compose/empty.env"
        assert compose_env_file("/home/ocr/drawbridge-run/etc") == (
            "/home/ocr/drawbridge-run/etc/compose/empty.env"
        )

    def test_compose_up_argv_no_extra_flags(self) -> None:
        argv = compose_up_argv(["prefix"])
        assert argv[-8:] == [
            "up", "--detach", "--no-build", "--pull", "never",
            "--wait", "--wait-timeout", "90",
        ]

    def test_compose_stop_argv(self) -> None:
        assert compose_stop_argv(["p"]) == ["p", "stop", "--timeout", "10"]

    def test_buildctl_argv_is_frozen(self) -> None:
        argv = buildctl_argv(
            buildkit_socket="/run/drawbridge/buildkit/buildkitd.sock",
            context_dir="/src",
            dockerfile_dir="/src",
            dockerfile_basename="Dockerfile",
            platform="linux/arm64",
            image_tag="drawbridge-demo-staging:job-abc123",
            image_archive="/out/image.tar",
        )
        assert argv == [
            "--addr", "/run/drawbridge/buildkit/buildkitd.sock",
            "build",
            "--frontend", "dockerfile.v0",
            "--local", "context=/src",
            "--local", "dockerfile=/src",
            "--opt", "filename=Dockerfile",
            "--opt", "platform=linux/arm64",
            "--output",
            "type=docker,name=drawbridge-demo-staging:job-abc123,dest=/out/image.tar",
        ]

    def test_unique_image_tag_shape(self) -> None:
        tag = unique_image_tag("demo", "staging", "0123456789abcdef" * 2 + "ff")
        assert tag == "drawbridge-demo-staging:job-0123456789ab"
        assert tag == tag.lower()

    def test_test_container_create_argv_hardening(self) -> None:
        from drawbridge.config.models import (
            TestResources,
            TestSuiteConfig,
        )

        suite = TestSuiteConfig(
            image_id=_IMAGE,
            entrypoint="/opt/drawbridge/suites/demo/smoke",
            network="drawbridge-demo-test",
            resources=TestResources(),
        )
        argv = build_test_create_argv(
            container_name="drawbridge-test-abc123", job_id="j" * 36, suite=suite
        )
        assert argv[0] == "create"
        assert argv[-1] == _IMAGE
        from itertools import pairwise

        pairs = list(pairwise(argv))
        for hardening in (
            ("--read-only", "--tmpfs"),
            ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges:true"),
            ("--user", "65532:65532"),
            ("--pids-limit", "128"),
            ("--network", "drawbridge-demo-test"),
            ("--entrypoint", "/opt/drawbridge/suites/demo/smoke"),
        ):
            assert hardening in pairs
        assert ("/tmp:rw,noexec,nosuid,size=64m", "--cap-drop") in pairs


class TestPreflightEnvPin:
    """D13: a missing compose env pin fails the preflight cleanly BEFORE any
    runtime change, instead of failing inside the recovery path."""

    async def test_missing_env_pin_rejected_before_change(self, tmp_path: Path) -> None:
        config = _portable_config(tmp_path)
        config.main.paths.config_dir = str(tmp_path / "etc")
        for app in config.apps.values():
            for env in app.environments.values():
                env.disk_budget_bytes = 1024**3  # keep the disk gate quiet
        (tmp_path / "compose.template.yaml").write_text(
            "services:\n  api:\n    image: REPLACE_BY_DRAWBRIDGE\n", encoding="utf-8"
        )
        for app in config.apps.values():
            for env in app.environments.values():
                env.compose_file = str(tmp_path / "compose.template.yaml")
        database = Database(tmp_path / "state" / "state.db")
        await database.connect()
        await database.initialize()
        store = Store(database)
        runtime = DeployRuntime(
            config=config,
            store=store,
            process_manager=_RecordingProcessManager(),  # type: ignore[arg-type]
            log_dir=str(tmp_path / "logs"),
        )
        try:
            with pytest.raises(DrawbridgeError) as exc:
                await runtime.step_release_preflight(_minimal_state(), {})
            assert exc.value.code == "CONFIG_INVALID"
            assert "empty.env" in str(exc.value)
            # delivering the pin flips the gate to pass
            pin = tmp_path / "etc" / "compose" / "empty.env"
            pin.parent.mkdir(parents=True, exist_ok=True)
            pin.write_text("# pin\n", encoding="utf-8")
            result = await runtime.step_release_preflight(_minimal_state(), {})
            assert result["compose_env_file"] == pin.as_posix()
        finally:
            await database.close()


class TestRenderCompose:
    def test_replaces_single_token(self) -> None:
        template = "services:\n  api:\n    image: REPLACE_BY_DRAWBRIDGE\n"
        assert _IMAGE in render_compose_text(template, _IMAGE)

    def test_missing_token_rejected(self) -> None:
        with pytest.raises(DrawbridgeError) as exc:
            render_compose_text("services: {}\n", _IMAGE)
        assert exc.value.code == "CONFIG_INVALID"

    def test_duplicate_token_rejected(self) -> None:
        with pytest.raises(DrawbridgeError):
            render_compose_text(
                "image: REPLACE_BY_DRAWBRIDGE\nother: REPLACE_BY_DRAWBRIDGE\n", _IMAGE
            )

    def test_non_immutable_image_rejected(self) -> None:
        with pytest.raises(DrawbridgeError) as exc:
            render_compose_text("image: REPLACE_BY_DRAWBRIDGE\n", "demo:latest")
        assert exc.value.code == "INTERNAL"


class TestParsers:
    def test_wait_exit_code(self) -> None:
        assert parse_wait_exit_code("0\n") == 0
        assert parse_wait_exit_code(" 137 ") == 137
        with pytest.raises(DrawbridgeError):
            parse_wait_exit_code("exited(1)")

    def test_compose_ps_images_array_and_lines(self) -> None:
        as_array = '[{"Name":"a","Image":"sha256:' + "b" * 64 + '"}]'
        assert parse_compose_ps_images(as_array) == ["sha256:" + "b" * 64]
        as_lines = (
            '{"Name":"a","Image":"sha256:' + "c" * 64 + '"}\n'
            '{"Name":"b","Image":"sha256:' + "d" * 64 + '"}\n'
        )
        assert len(parse_compose_ps_images(as_lines)) == 2
        assert parse_compose_ps_images("") == []


class _RecordingProcessManager:
    """Fake executor seam: records every spec instead of executing."""

    def __init__(self, accepted: bool = False) -> None:
        self.executed: list[str] = []
        self.accepted = accepted

    async def execute(self, spec: Any) -> Any:
        self.executed.append(spec.operation)

        class _Result:
            pass

        result = _Result()
        result.accepted = self.accepted  # type: ignore[attr-defined]
        result.stderr_preview = ""  # type: ignore[attr-defined]
        return result


class TestDockerfileDirectiveGuard:
    """Plan D2: `# syntax=` must be rejected before buildctl is invoked."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("FROM scratch\n", {}),
            ("# a plain comment\nFROM scratch\n", {}),
            ("# syntax=docker/dockerfile:1\nFROM scratch\n", {"syntax": "docker/dockerfile:1"}),
            ("#syntax=docker/dockerfile:1.7\nFROM scratch\n", {"syntax": "docker/dockerfile:1.7"}),
            # mid-file occurrences are also rejected (conservative scan)
            ("FROM scratch\n# syntax=ghcr.io/evil/frontend\nRUN true\n", {"syntax": "ghcr.io/evil/frontend"}),
            ("# escape=`\nFROM scratch\n", {"escape": "`"}),
            ("# check=skip=true\nFROM scratch\n", {"check": "skip=true"}),
            ("# escape=`\n# check=skip=true\nFROM scratch\n", {"escape": "`", "check": "skip=true"}),
            ("RUN echo '# syntax=x'\n", {}),
        ],
    )
    def test_scan_table(self, text: str, expected: dict[str, str]) -> None:
        assert scan_dockerfile_directives(text) == expected

    async def test_syntax_directive_rejected_before_buildctl(self, tmp_path: Path) -> None:
        config = _portable_config(tmp_path)
        database = Database(tmp_path / "state" / "state.db")
        await database.connect()
        await database.initialize()
        store = Store(database)
        pm = _RecordingProcessManager()
        source = tmp_path / "source"
        source.mkdir()
        (source / "Dockerfile").write_text(
            "# syntax=docker/dockerfile:1\nFROM scratch\n", encoding="utf-8"
        )
        state = _minimal_state()
        state.source_dir = str(source)
        runtime = DeployRuntime(
            config=config,
            store=store,
            process_manager=pm,  # type: ignore[arg-type]
            log_dir=str(tmp_path / "logs"),
        )
        try:
            with pytest.raises(DrawbridgeError) as exc:
                await runtime.step_image_build(state, {})
            assert exc.value.code == "BUILD_UNSUPPORTED_FRONTEND"
            assert "syntax" in str(exc.value)
            assert pm.executed == []  # rejection path never reached buildctl
        finally:
            await database.close()

    async def test_builtin_directives_allowed_and_recorded(self, tmp_path: Path) -> None:
        """escape/check pass the guard; the step result records them."""
        config = _portable_config(tmp_path)
        database = Database(tmp_path / "state" / "state.db")
        await database.connect()
        await database.initialize()
        store = Store(database)
        pm = _RecordingProcessManager(accepted=True)
        source = tmp_path / "source"
        source.mkdir()
        (source / "Dockerfile").write_text(
            "# escape=`\n# check=skip=true\nFROM scratch\n", encoding="utf-8"
        )
        state = _minimal_state()
        state.source_dir = str(source)
        # The fake executor cannot materialize the archive; pre-create it so
        # the post-build artifact check passes and the result is returned.
        job_dir = tmp_path / "deploy" / "jobs" / state.job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "image.tar").write_bytes(b"tar")
        runtime = DeployRuntime(
            config=config,
            store=store,
            process_manager=pm,  # type: ignore[arg-type]
            log_dir=str(tmp_path / "logs"),
        )
        try:
            result = await runtime.step_image_build(state, {})
            assert pm.executed == ["image_build"]
            assert result["dockerfile_directives"] == {"escape": "`", "check": "skip=true"}
        finally:
            await database.close()


@pytest.mark.skipif(sys.platform == "linux", reason="gating probe for non-target hosts")
class TestTargetHostGating:
    async def test_steps_refuse_off_target_hosts(self, tmp_path: Path) -> None:
        config = load_config_from_dir(CONFIG_DIR)
        config.main.paths.state_dir = str(tmp_path / "state")
        database = Database(tmp_path / "state" / "state.db")
        await database.connect()
        await database.initialize()
        store = Store(database)
        try:
            runtime = DeployRuntime(
                config=config,
                store=store,
                process_manager=object(),  # type: ignore[arg-type]
                log_dir=str(tmp_path / "logs"),
            )
            state = _minimal_state()
            for operation in (
                "release_preflight",
                "source_snapshot",
                "image_build",
                "compose_deploy",
                "test_suite",
                "restore_previous",
            ):
                with pytest.raises(DrawbridgeError) as exc:
                    await runtime(operation, state, {})
                assert exc.value.code == "UNSUPPORTED", operation
        finally:
            await database.close()


def _portable_config(tmp_path: Path) -> Any:
    """Repo config with state/log/deploy paths anchored inside tmp_path.

    Keeps step-level tests portable: `_jobs_dir` otherwise materializes the
    registered Linux deploy_root on whatever drive the test host runs on.
    """
    config = load_config_from_dir(CONFIG_DIR)
    config.main.paths.state_dir = str(tmp_path / "state")
    for app in config.apps.values():
        for env in app.environments.values():
            env.deploy_root = str(tmp_path / "deploy")
    return config


def _minimal_state() -> Any:
    from drawbridge.runner.deploy import DeployState
    from drawbridge.state.records import JobRecord

    job = JobRecord(
        job_id="0123456789abcdef" * 2 + "ab",
        kind="deploy",
        action="deploy_verify",
        app="demo",
        environment="staging",
        plan_id=None,
        status="running",
        params={"plan_id": "p"},
        queued_at=0.0,
    )
    return DeployState(
        job=job,
        plan=None,
        app="demo",
        environment="staging",
        started=0.0,
        total_budget=100.0,
        recovery_budget=10.0,
    )
