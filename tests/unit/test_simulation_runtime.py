"""SimulationRuntime adapter tests.

Covers the contract the communication harness relies on:

* synthetic image ids satisfy the immutable-id shape the compose rendering
  enforces;
* the simulated build/import/identify round-trip produces a manifest and a
  stable image id without any subprocess;
* compose_deploy renders the admin template and writes the synthetic
  application log ops_logs pages from;
* release_finalize drift-checks the rendered file against the release image;
* simulated health/test evidence marks itself as simulation;
* the RuntimeSelector routes each target to its configured adapter.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from drawbridge.config.loader import load_config_from_dir
from drawbridge.executor.process import ProcessManager
from drawbridge.runner.deploy import DeployState
from drawbridge.runner.runtime import DeployRuntime, render_compose_text
from drawbridge.runner.simulation import (
    SIMULATION_LOG_NAME,
    RuntimeSelector,
    SimulationRuntime,
    build_runtime,
    simulation_checks,
    synthetic_image_id,
)
from drawbridge.state.db import Database
from drawbridge.state.records import JobRecord, PlanRecord
from drawbridge.state.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"

_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")


@pytest.fixture()
async def env(tmp_path: Path):
    config = load_config_from_dir(CONFIG_DIR)
    config.main.paths.state_dir = str(tmp_path / "state")
    config.main.paths.lock_dir = str(tmp_path / "locks")
    config.main.paths.log_dir = str(tmp_path / "logs")
    # Point the environment at the simulation adapter and relocate every
    # filesystem anchor into the test directory.
    env_cfg = config.environment("demo", "staging")
    env_cfg.runtime = "simulation"
    env_cfg.deploy_root = str(tmp_path / "deploy")
    env_cfg.compose_file = str(tmp_path / "compose.template.yaml")
    (tmp_path / "compose.template.yaml").write_text(
        "services:\n  api:\n    image: REPLACE_BY_DRAWBRIDGE\n",
        encoding="utf-8",
    )
    database = Database(tmp_path / "state" / "state.db")
    await database.connect()
    await database.initialize()
    store = Store(database)
    runtime = SimulationRuntime(
        config=config,
        store=store,
        process_manager=ProcessManager(),
        log_dir=str(tmp_path / "logs"),
    )
    yield config, store, runtime, env_cfg, tmp_path
    await database.close()


def make_state(config, store: Store) -> DeployState:
    plan = PlanRecord(
        plan_id="p" * 36,
        app="demo",
        environment="staging",
        workflow="deploy_verify",
        source_mode="local",
        git_ref="refs/heads/main",
        commit_sha="a" * 40,
        config_digest=config.digest,
        baseline_release_id=None,
        status="planned",
        created_at=0.0,
        expires_at=1e12,
        params={},
    )
    job = JobRecord(
        job_id="j" * 36,
        kind="deploy",
        action="deploy_verify",
        app="demo",
        environment="staging",
        plan_id=plan.plan_id,
        status="running",
        params={"plan_id": plan.plan_id},
        queued_at=0.0,
    )
    return DeployState(
        job=job,
        plan=plan,
        app="demo",
        environment="staging",
        started=0.0,
        total_budget=600.0,
        recovery_budget=300.0,
        commit_sha="a" * 40,
    )


def test_synthetic_image_id_shape() -> None:
    image_id = synthetic_image_id("a" * 40, "j" * 36)
    assert _IMAGE_ID_RE.fullmatch(image_id)
    # Deterministic per (commit, job) — the id is the release's anchor.
    assert image_id == synthetic_image_id("a" * 40, "j" * 36)
    assert image_id != synthetic_image_id("b" * 40, "j" * 36)


def test_synthetic_id_satisfies_render_contract() -> None:
    rendered = render_compose_text(
        "image: REPLACE_BY_DRAWBRIDGE", synthetic_image_id("a" * 40, "b" * 36)
    )
    assert rendered.startswith("image: sha256:")


async def test_build_import_identify_roundtrip(env) -> None:
    config, store, runtime, _, tmp_path = env
    state = make_state(config, store)
    state.source_dir = str(tmp_path / "source")
    (Path(state.source_dir) / ".").mkdir(parents=True, exist_ok=True)
    (Path(state.source_dir) / "Dockerfile").write_text("FROM scratch\n")

    built = await runtime("image_build", state)
    assert built["simulated"] is True
    manifest = Path(built["image_archive"])
    assert manifest.is_file() and manifest.name == "image.simulation.json"

    imported = await runtime("image_import", state)
    assert imported["loaded"] is True

    state.image_tag = built["image_tag"]
    identified = await runtime("image_identify", state)
    assert _IMAGE_ID_RE.fullmatch(identified["image_id"])


async def test_compose_deploy_writes_rendered_file_and_log(env) -> None:
    config, store, runtime, env_cfg, _ = env
    state = make_state(config, store)
    image_id = synthetic_image_id(state.commit_sha or "", state.job.job_id)
    state.image_id = image_id

    deployed = await runtime("compose_deploy", state)
    rendered = Path(deployed["compose_file"])
    assert rendered.is_file()
    assert image_id in rendered.read_text(encoding="utf-8")
    log_file = Path(env_cfg.deploy_root) / SIMULATION_LOG_NAME
    assert log_file.is_file()
    assert log_file.read_text(encoding="utf-8").count("\n") >= 4


async def test_release_finalize_detects_drift(env) -> None:
    from drawbridge.errors import DrawbridgeError

    config, store, runtime, env_cfg, _ = env
    state = make_state(config, store)
    state.image_id = synthetic_image_id(state.commit_sha or "", state.job.job_id)
    await runtime("compose_deploy", state)

    finalized = await runtime("release_finalize", state)
    assert finalized["verified_image"] == state.image_id

    # Tamper with the rendered file: the drift gate must reject it.
    rendered = Path(env_cfg.deploy_root) / "compose.rendered.yaml"
    rendered.write_text(
        rendered.read_text(encoding="utf-8").replace(state.image_id, "sha256:" + "0" * 64),
        encoding="utf-8",
    )
    with pytest.raises(DrawbridgeError) as excinfo:
        await runtime("release_finalize", state)
    assert excinfo.value.code == "DRIFT_DETECTED"


async def test_health_and_test_evidence_marked_simulation(env) -> None:
    config, store, runtime, _, _ = env
    state = make_state(config, store)
    health = await runtime("health_check", state)
    assert health["simulated"] is True
    assert all(c["passed"] for c in health["checks"])
    assert all(c["validation_level"] == "simulation" for c in health["checks"])

    tested = await runtime("test_suite", state, {"suite": "smoke"})
    assert tested["exit_code"] == 0
    assert tested["simulated"] is True


def test_simulation_checks_mirror_probe_shape() -> None:
    config = load_config_from_dir(CONFIG_DIR)
    checks = simulation_checks(config.environment("demo", "staging"))
    assert checks and set(checks[0]) >= {
        "url",
        "passed",
        "last_status",
        "consecutive_successes",
    }


async def test_runtime_selector_routes_by_config(env) -> None:
    config, store, _, _, tmp_path = env
    selector = build_runtime(
        config=config,
        store=store,
        process_manager=ProcessManager(),
        log_dir=str(tmp_path / "logs"),
    )
    assert isinstance(selector, RuntimeSelector)
    assert selector.adapter_for("demo", "staging") is selector.simulation
    # A compose target keeps the production executor.
    config.environment("demo", "staging").runtime = "compose"
    assert selector.adapter_for("demo", "staging") is selector.compose
    assert isinstance(selector.compose, DeployRuntime)
