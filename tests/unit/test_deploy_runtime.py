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
    compose_prefix,
    compose_stop_argv,
    compose_up_argv,
    parse_compose_ps_images,
    parse_wait_exit_code,
    render_compose_text,
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
        prefix = compose_prefix("proj", "/srv/deploy", "/srv/deploy/compose.rendered.yaml")
        assert prefix == [
            "compose", "--ansi", "never",
            "--project-name", "proj",
            "--project-directory", "/srv/deploy",
            "--env-file", "/etc/drawbridge/compose/empty.env",
            "-f", "/srv/deploy/compose.rendered.yaml",
        ]

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
