"""End-to-end simulation flow: the drawbridge-simulate scenario.

Builds the same isolated fixture the ``drawbridge-simulate --fixture``
command generates (demo git repository + simulation config bundle) and
drives the full gateway↔runner conversation in-process: catalog, logs,
config_read, two deploys, ops_test, restart and rollback — every step over
the shared SQLite queue, exactly like the two systemd processes on the
target server.  Requires only Python and git, so it runs on the Windows
development hosts too.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from drawbridge.entries.simulate_main import create_fixture, run_scenario

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_CONFIG_DIR = REPO_ROOT / "configs"

pytestmark = pytest.mark.skipif(
    not SOURCE_CONFIG_DIR.is_dir(), reason="repository config bundle not available"
)


@pytest.fixture()
def fixture_dir(tmp_path: Path) -> Path:
    return create_fixture(tmp_path, SOURCE_CONFIG_DIR)


async def test_simulation_scenario_full_flow(fixture_dir: Path) -> None:
    report = await run_scenario(fixture_dir, app="demo", environment="staging", job_timeout=120.0)

    assert report["ok"] is True, report["steps"]
    step_names = {s["name"] for s in report["steps"]}
    # The complete conversation happened through the real service layer.
    assert {
        "ops_catalog",
        "ops_logs#before_deploy",
        "ops_operation_run#git_status",
        "ops_operation_run#config_read",
        "ops_release_plan#one",
        "ops_release_apply#one",
        "deploy_job#one",
        "ops_logs#after_deploy",
        "ops_release_plan#two",
        "deploy_job#two",
        "ops_test",
        "ops_service_restart",
        "ops_release_rollback",
        "rollback_job",
    } <= step_names

    # Two deploys produced two release records; the explicit rollback to
    # release one is the current release afterwards.
    assert len(report["releases"]) == 2
    current = report["current_release"]
    assert current is not None
    assert current["status"] == "rollback"
    assert current["release_id"] not in report["releases"]
    assert current["image_id"].startswith("sha256:")

    # Deployed evidence: frozen commit and simulated runtime flag.
    deploy = next(s for s in report["steps"] if s["name"] == "deploy_job#one")
    assert deploy["detail"]["status"] == "succeeded"
    assert deploy["detail"]["runtime_change_started"] is True
    assert deploy["detail"]["result"]["commit_sha"]

    # The simulated application log answered the post-deploy ops_logs page.
    logs = next(s for s in report["steps"] if s["name"] == "ops_logs#after_deploy")
    assert logs["detail"]["lines"]

    if sys.platform != "linux":
        status = next(s for s in report["steps"] if s["name"] == "ops_status")
        assert "UNSUPPORTED envelope verified" in str(status["detail"])


async def test_scenario_config_requires_simulation_runtime(tmp_path: Path) -> None:
    from drawbridge.config.loader import load_config_from_dir
    from drawbridge.errors import DrawbridgeError

    config_dir = create_fixture(tmp_path, SOURCE_CONFIG_DIR)
    config = load_config_from_dir(config_dir)
    config.environment("demo", "staging").runtime = "compose"
    # Persist the flipped runtime so run_scenario re-loads it from disk.
    apps_yaml = config_dir / "apps.yaml"
    text = apps_yaml.read_text(encoding="utf-8")
    apps_yaml.write_text(text.replace("runtime: simulation", "runtime: compose"), encoding="utf-8")
    del config
    with pytest.raises(DrawbridgeError) as excinfo:
        await run_scenario(config_dir, job_timeout=60.0)
    assert excinfo.value.code == "CONFIG_INVALID"
