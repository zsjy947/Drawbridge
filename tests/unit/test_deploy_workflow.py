"""DeployWorkflow orchestration tests with a fake step executor.

Covers the recovery contract (MVP spec §8):
* failure before the runtime change → Failed, no recovery, no side effects;
* failure after it with a baseline → RolledBack (release still failed);
* failure after it without a baseline → FailedNoBaseline;
* recovery failure → RollbackFailed / NeedsAttention semantics.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any

import pytest

from drawbridge.config.loader import load_config_from_dir
from drawbridge.errors import DrawbridgeError
from drawbridge.runner.deploy import DeployWorkflow
from drawbridge.state.db import Database
from drawbridge.state.records import JobStatus
from drawbridge.state.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"


class FakeRuntime:
    """Records step invocations; fails at configured steps."""

    def __init__(self, fail_at: set[str], *, fail_recover: bool = False):
        self.calls: list[str] = []
        self.fail_at = fail_at
        self.fail_recover = fail_recover
        self.params_seen: list[dict] = []

    async def __call__(
        self, operation: str, state: Any, params: dict | None = None
    ) -> dict[str, Any]:
        self.params_seen.append(dict(params or {}))
        if operation in ("stop_initial", "restore_previous"):
            self.calls.append(operation)
            if self.fail_recover:
                raise DrawbridgeError("restore failed", code="ROLLBACK_FAILED")
            return {"restored": operation}
        if operation in self.fail_at:
            self.calls.append(f"FAIL:{operation}")
            raise DrawbridgeError(f"{operation} exploded", code="BUILD_FAILED")
        self.calls.append(operation)
        detail: dict[str, Any] = {}
        if operation == "image_build":
            detail["image_tag"] = "demo:candidate"
        if operation == "image_import":
            detail["image_id"] = "sha256:" + "c" * 64
        return detail


@pytest.fixture()
async def setup(tmp_path: Path):
    config = load_config_from_dir(CONFIG_DIR)
    config.main.paths.state_dir = str(tmp_path / "state")
    config.main.paths.lock_dir = str(tmp_path / "locks")
    config.main.paths.log_dir = str(tmp_path / "logs")
    database = Database(tmp_path / "state" / "state.db")
    await database.connect()
    await database.initialize()
    store = Store(database)
    yield config, store
    await database.close()


async def admit_deploy_job(config, store: Store, baseline_id: str | None) -> tuple:
    plan = await store.create_plan(
        app="demo",
        environment="staging",
        workflow="deploy_verify",
        source_mode="fetch",
        git_ref="refs/heads/main",
        commit_sha="a" * 40,
        config_digest=config.digest,
        baseline_release_id=baseline_id,
        ttl_seconds=900,
        params={},
    )
    job = await store.admit_job(
        kind="deploy",
        action="deploy_verify",
        app="demo",
        environment="staging",
        params={"plan_id": plan.plan_id},
        idempotency_key=f"deploy-{plan.plan_id[:8]}",
        config_digest=config.digest,
        queue_timeout_seconds=600,
        deadline_seconds=1800,
        max_queued=50,
        max_queued_per_target=5,
        plan_id=plan.plan_id,
    )
    await store.claim_next_job(owner="runner-test", kinds=["deploy"])
    return plan, job


async def baseline_release(store: Store, release_id: str) -> None:
    import time

    from drawbridge.state.records import ReleaseRecord

    await store.record_release(
        ReleaseRecord(
            release_id=release_id,
            app="demo",
            environment="staging",
            plan_id=None,
            job_id=None,
            commit_sha="b" * 40,
            image_id="sha256:" + "b" * 64,
            image_tag="demo:old",
            config_digest="d" * 64,
            status="succeeded",
            rollback_of=None,
            compose_path="/etc/drawbridge/compose/demo.staging.yaml",
            deploy_dir="/srv/drawbridge/apps/demo/staging",
            evidence={},
            created_at=time.time(),
            verified_at=time.time(),
        )
    )


class TestSuccessPath:
    async def test_all_steps_pass_and_release_recorded(self, setup) -> None:
        config, store = setup
        runtime = FakeRuntime(fail_at=set())
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=runtime
        )
        _plan, job = await admit_deploy_job(config, store, baseline_id=None)
        status, result, recovery = await workflow.run(job)
        assert status == JobStatus.SUCCEEDED
        assert recovery is None
        assert result["commit_sha"] == "a" * 40
        assert result["image_id"] == "sha256:" + "c" * 64
        current = await store.get_current_release("demo", "staging")
        assert current is not None
        assert current.release_id == result["release_id"]
        # all workflow steps ran in order
        expected = [s.operation for s in config.workflows["deploy_verify"].steps]
        assert runtime.calls == expected
        plan_after = await store.get_plan(_plan.plan_id)
        assert plan_after.status == "applied"


class TestFailureBeforeRuntimeChange:
    async def test_fails_without_recovery(self, setup) -> None:
        config, store = setup
        runtime = FakeRuntime(fail_at={"image_build"})
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=runtime
        )
        _plan, job = await admit_deploy_job(config, store, baseline_id=None)
        status, result, recovery = await workflow.run(job)
        assert status == JobStatus.FAILED
        assert recovery is None
        assert result["error"]["code"] == "BUILD_FAILED"
        # stopped right after the failure — no deploy step attempted
        assert "compose_deploy" not in runtime.calls
        assert await store.get_current_release("demo", "staging") is None


class TestFailureAfterRuntimeChange:
    async def test_recovers_previous_release(self, setup) -> None:
        config, store = setup
        await baseline_release(store, "r-baseline")
        runtime = FakeRuntime(fail_at={"health_check"})
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=runtime
        )
        _plan, job = await admit_deploy_job(
            config, store, baseline_id="r-baseline"
        )
        status, result, recovery = await workflow.run(job)
        assert status == JobStatus.ROLLED_BACK
        assert recovery is not None
        assert recovery["status"] == "restored_previous"
        assert recovery["baseline_release_id"] == "r-baseline"
        # result honestly reports failure even though recovery succeeded
        assert result["error"]["code"] == "BUILD_FAILED" or "error" in result
        assert "compose_deploy" in runtime.calls
        assert "restore_previous" in runtime.calls

    async def test_no_baseline_stops_initial_deployment(self, setup) -> None:
        config, store = setup
        runtime = FakeRuntime(fail_at={"health_check"})
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=runtime
        )
        _plan, job = await admit_deploy_job(config, store, baseline_id=None)
        status, _result, recovery = await workflow.run(job)
        assert status == JobStatus.FAILED_NO_BASELINE
        assert recovery is not None
        assert recovery["status"] == "stopped_initial"
        assert "stop_initial" in runtime.calls

    async def test_recovery_failure_blocks_target(self, setup) -> None:
        config, store = setup
        await baseline_release(store, "r-baseline")
        runtime = FakeRuntime(fail_at={"health_check"}, fail_recover=True)
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=runtime
        )
        _plan, job = await admit_deploy_job(
            config, store, baseline_id="r-baseline"
        )
        status, _result, recovery = await workflow.run(job)
        assert status == JobStatus.ROLLBACK_FAILED
        assert recovery is not None
        assert recovery["status"] == "recovery_failed"
        assert recovery["needs_attention"] is True


class TestStepPersistence:
    async def test_steps_recorded_with_details(self, setup) -> None:
        config, store = setup
        runtime = FakeRuntime(fail_at={"image_import"})
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=runtime
        )
        _plan, job = await admit_deploy_job(config, store, baseline_id=None)
        await workflow.run(job)
        steps = await store.list_steps(job.job_id)
        by_name = OrderedDict((s.name, s) for s in steps)
        assert by_name["preflight"].status == "succeeded"
        assert by_name["source"].status == "succeeded"
        assert by_name["import"].status == "failed"
        # failure stops the workflow: no later step rows are created
        assert "deploy" not in by_name
