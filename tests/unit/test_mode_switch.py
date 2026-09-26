"""Cross-runtime baseline identity tests (plan D12).

Switching ``environments.<env>.runtime`` from simulation to compose must not
carry simulation-era releases into the compose target's current/baseline
computations: their synthetic image ids do not exist in the Engine, so
restore paths would fail misleadingly and block the target behind
ROLLBACK_FAILED.  Simulation targets keep the unfiltered view.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from drawbridge.config.loader import load_config_from_dir
from drawbridge.errors import DrawbridgeError
from drawbridge.gateway.service import GatewayService
from drawbridge.runner.deploy import DeployWorkflow
from drawbridge.state.db import Database
from drawbridge.state.records import JobStatus, ReleaseRecord
from drawbridge.state.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"

_SHA = "sha256:" + "b" * 64


class FakeRuntime:
    def __init__(self, fail_at: set[str]) -> None:
        self.calls: list[str] = []
        self.fail_at = fail_at

    async def __call__(
        self, operation: str, state: Any, params: dict | None = None
    ) -> dict[str, Any]:
        self.calls.append(operation)
        if operation in self.fail_at:
            raise DrawbridgeError(f"{operation} failed", code="VERIFY_FAILED")
        if operation == "image_import":
            return {"image_id": _SHA}
        return {}


@pytest.fixture()
async def env(tmp_path: Path):
    from tests.conftest import install_compose_template

    config = load_config_from_dir(CONFIG_DIR)
    config.main.paths.state_dir = str(tmp_path / "state")
    config.main.paths.lock_dir = str(tmp_path / "locks")
    config.main.paths.log_dir = str(tmp_path / "logs")
    install_compose_template(config, tmp_path)
    database = Database(tmp_path / "state" / "state.db")
    await database.connect()
    await database.initialize()
    store = Store(database)
    service = GatewayService(config, store)
    yield config, store, service
    await database.close()


async def seed_release(
    store: Store, release_id: str, *, simulated: bool, status: str = "succeeded"
) -> None:
    await store.record_release(
        ReleaseRecord(
            release_id=release_id,
            app="demo",
            environment="staging",
            plan_id=None,
            job_id=None,
            commit_sha="b" * 40,
            image_id=_SHA,
            image_tag=None,
            config_digest="d" * 64,
            simulated=simulated,
            status=status,
            rollback_of=None,
            compose_path=None,
            deploy_dir=None,
            evidence={} if not simulated else {"validation_level": "simulation"},
            created_at=time.time(),
            verified_at=time.time(),
        )
    )


async def _compose_plan(store: Store, config, baseline: str | None) -> Any:
    from drawbridge.config.compose_template import read_compose_template

    env_cfg = config.environment("demo", "staging")
    digest = read_compose_template(env_cfg.compose_file, list(env_cfg.services)).digest
    return await store.create_plan(
        app="demo",
        environment="staging",
        workflow="deploy_verify",
        source_mode="fetch",
        git_ref="refs/heads/main",
        commit_sha="a" * 40,
        config_digest=config.digest,
        baseline_release_id=baseline,
        ttl_seconds=900,
        params={},
        compose_template_digest=digest,
    )


class TestBaselineFiltering:
    async def test_compose_target_ignores_simulated_current(self, env) -> None:
        config, store, _ = env
        config.environment("demo", "staging").runtime = "compose"
        await seed_release(store, "r-sim", simulated=True)
        # Compose view: no current release (fresh-host semantics)...
        assert await store.get_current_release(
            "demo", "staging", exclude_simulated=True
        ) is None
        # ...while the unfiltered view (simulation target) still sees it.
        assert (
            await store.get_current_release("demo", "staging")
        ).release_id == "r-sim"

    async def test_switch_recovery_goes_stop_initial(self, env) -> None:
        """Failure after the runtime change with only a simulated baseline
        must recover via stop_initial (FAILED_NO_BASELINE), never render the
        synthetic image into a compose up."""
        config, store, _ = env
        config.environment("demo", "staging").runtime = "compose"
        await seed_release(store, "r-sim", simulated=True)
        plan = await _compose_plan(store, config, baseline=None)
        job = await store.admit_job(
            kind="deploy",
            action="deploy_verify",
            app="demo",
            environment="staging",
            params={"plan_id": plan.plan_id},
            idempotency_key=f"switch-{plan.plan_id[:8]}",
            config_digest=config.digest,
            queue_timeout_seconds=600,
            deadline_seconds=1800,
            max_queued=50,
            max_queued_per_target=5,
            plan_id=plan.plan_id,
        )
        await store.claim_next_job(owner="runner-test", kinds=["deploy"])
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=FakeRuntime({"health_check"})
        )
        status, _result, recovery, _staged = await workflow.run(job)
        assert status == JobStatus.FAILED_NO_BASELINE
        assert recovery is not None
        assert recovery["status"] == "stopped_initial"

    async def test_explicit_rollback_to_simulated_rejected(self, env) -> None:
        config, store, service = env
        config.environment("demo", "staging").runtime = "compose"
        await seed_release(store, "r-sim", simulated=True)
        with pytest.raises(DrawbridgeError) as exc:
            await service.ops_release_rollback(
                app="demo",
                environment="staging",
                release_id="r-sim",
                reason="mode switch test",
                idempotency_key="rollback-sim-1",
            )
        assert exc.value.code == "INVALID_PARAMETER"
        assert "simulation" in str(exc.value)

    async def test_history_hides_simulated_eligibility_on_compose(self, env) -> None:
        config, store, service = env
        config.environment("demo", "staging").runtime = "compose"
        await seed_release(store, "r-sim", simulated=True)
        time.sleep(0.002)
        await seed_release(store, "r-compose", simulated=False)
        result = await service.ops_history("demo", "staging", what="releases", limit=10)
        by_id = {r["release_id"]: r for r in result["data"]["releases"]}
        assert by_id["r-compose"]["is_current"] is True
        assert by_id["r-compose"]["rollback_eligible"] is False
        assert by_id["r-sim"]["is_current"] is False
        assert by_id["r-sim"]["rollback_eligible"] is False
        assert by_id["r-sim"]["simulated"] is True

    async def test_simulation_target_keeps_simulated_baseline(self, env) -> None:
        config, store, _ = env
        config.environment("demo", "staging").runtime = "simulation"
        await seed_release(store, "r-sim", simulated=True)
        current = await store.get_current_release(
            "demo",
            "staging",
            exclude_simulated=False,  # simulation target: unfiltered
        )
        assert current is not None and current.release_id == "r-sim"


class TestBaselineImageFastFail:
    async def test_restore_previous_missing_image_fails_clean(self, tmp_path: Path) -> None:
        """The Engine probe runs BEFORE compose up — a missing baseline image
        (simulated-era or hand-pruned) produces a structured ROLLBACK_FAILED
        instead of a misleading --pull never compose error inside recovery."""
        from tests.conftest import install_compose_template
        from tests.unit.test_deploy_runtime import (
            DeployRuntime,
            _minimal_state,
            _portable_config,
            _RecordingProcessManager,
        )

        config = _portable_config(tmp_path)
        config.environment("demo", "staging").runtime = "compose"
        install_compose_template(config, tmp_path)
        database = Database(tmp_path / "state" / "state.db")
        await database.connect()
        await database.initialize()
        store = Store(database)
        pm = _RecordingProcessManager(accepted=False)  # probe fails
        runtime = DeployRuntime(
            config=config,
            store=store,
            process_manager=pm,  # type: ignore[arg-type]
            log_dir=str(tmp_path / "logs"),
        )
        state = _minimal_state()
        state.baseline = ReleaseRecord(
            release_id="r-gone",
            app="demo",
            environment="staging",
            plan_id=None,
            job_id=None,
            commit_sha="b" * 40,
            image_id=_SHA,
            image_tag=None,
            config_digest="d" * 64,
            status="succeeded",
            rollback_of=None,
            compose_path=None,
            deploy_dir=None,
            evidence={},
            created_at=1.0,
            verified_at=1.0,
        )
        try:
            with pytest.raises(DrawbridgeError) as exc:
                await runtime.step_restore_previous(state, {})
            assert exc.value.code == "ROLLBACK_FAILED"
            assert "not present in the Docker Engine" in str(exc.value)
            # only the probe ran — no compose up was attempted
            assert list(pm.executed) == ["baseline_image_check"]
        finally:
            await database.close()
