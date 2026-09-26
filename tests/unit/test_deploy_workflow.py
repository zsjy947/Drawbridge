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
    from tests.conftest import install_compose_template

    template_digest = install_compose_template(config, tmp_path)
    database = Database(tmp_path / "state" / "state.db")
    await database.connect()
    await database.initialize()
    store = Store(database)
    yield config, store, template_digest
    await database.close()


async def admit_deploy_job(
    config, store: Store, baseline_id: str | None, template_digest: str
) -> tuple:
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
        compose_template_digest=template_digest,
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
        config, store, digest = setup
        runtime = FakeRuntime(fail_at=set())
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=runtime
        )
        _plan, job = await admit_deploy_job(config, store, baseline_id=None, template_digest=digest)
        status, result, recovery, staged = await workflow.run(job)
        assert status == JobStatus.SUCCEEDED
        assert recovery is None
        assert result["commit_sha"] == "a" * 40
        assert result["image_id"] == "sha256:" + "c" * 64
        # D4: nothing is written until the caller commits the staged release
        assert await store.get_current_release("demo", "staging") is None
        assert staged is not None and staged.release.release_id == result["release_id"]
        await store.complete_job_with_release(
            job=job,
            terminal_status=JobStatus.SUCCEEDED,
            result=result,
            recovery=None,
            owner="runner-test",
            staged=[staged],
        )
        current = await store.get_current_release("demo", "staging")
        assert current is not None
        assert current.release_id == result["release_id"]
        finished = await store.get_job(job.job_id)
        assert finished.status == JobStatus.SUCCEEDED
        # all workflow steps ran in order
        expected = [s.operation for s in config.workflows["deploy_verify"].steps]
        assert runtime.calls == expected
        plan_after = await store.get_plan(_plan.plan_id)
        assert plan_after.status == "applied"

    async def test_completion_transaction_rolls_back_on_error(self, setup) -> None:
        """D4 injection test: a failure inside the completion transaction
        leaves release/events/job terminal state ALL absent."""
        config, store, digest = setup
        runtime = FakeRuntime(fail_at=set())
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=runtime
        )
        _plan, job = await admit_deploy_job(config, store, baseline_id=None, template_digest=digest)
        status, result, _recovery, staged = await workflow.run(job)
        assert status == JobStatus.SUCCEEDED and staged is not None

        original = store.db.conn.execute

        async def exploding_execute(sql: str, *args: object) -> object:
            if "INSERT INTO events" in sql and "job_finished" not in sql:
                raise RuntimeError("injected completion failure")
            return await original(sql, *args)  # type: ignore[misc]

        store.db.conn.execute = exploding_execute  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="injected"):
                await store.complete_job_with_release(
                    job=job,
                    terminal_status=JobStatus.SUCCEEDED,
                    result=result,
                    recovery=None,
                    owner="runner-test",
                    staged=[staged],
                )
        finally:
            store.db.conn.execute = original  # type: ignore[method-assign]
        assert await store.get_current_release("demo", "staging") is None
        still_running = await store.get_job(job.job_id)
        assert still_running.status == JobStatus.RUNNING
        async with store.db.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind = 'release_recorded'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row["n"] == 0


class TestFailureEvidence:
    async def test_build_failure_carries_full_stderr_details(self, setup) -> None:
        """Plan D14: the error message keeps its short preview while the
        bounded FULL stderr ring travels in details (step record + job
        result), so a remote operator can locate the root cause."""
        config, store, digest = setup
        long_stderr = "E" * 5000
        evidence = {"stderr": long_stderr, "termination_reason": "completed"}

        class ExplodingRuntime:
            async def __call__(
                self, operation: str, state: Any, params: dict | None = None
            ) -> dict[str, Any]:
                if operation == "image_build":
                    raise DrawbridgeError(
                        f"image build failed: {long_stderr[:300]}",
                        code="BUILD_FAILED",
                        details=evidence,
                    )
                return {}

        _plan, job = await admit_deploy_job(
            config, store, baseline_id=None, template_digest=digest
        )
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=ExplodingRuntime()
        )
        status, result, _recovery, _staged = await workflow.run(job)
        assert status == JobStatus.FAILED
        assert result["error"]["code"] == "BUILD_FAILED"
        assert len(result["error"]["message"]) <= 500
        assert result["error"]["details"]["stderr"] == long_stderr
        assert result["error"]["details"]["termination_reason"] == "completed"
        steps = await store.list_steps(job.job_id)
        failed = next(s for s in steps if s.status == "failed")
        assert failed.detail["evidence"]["stderr"] == long_stderr
        assert failed.detail["error"].startswith("image build failed")


class TestFailureBeforeRuntimeChange:
    async def test_fails_without_recovery(self, setup) -> None:
        config, store, digest = setup
        runtime = FakeRuntime(fail_at={"image_build"})
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=runtime
        )
        _plan, job = await admit_deploy_job(config, store, baseline_id=None, template_digest=digest)
        status, result, recovery, _staged = await workflow.run(job)
        assert status == JobStatus.FAILED
        assert recovery is None
        assert result["error"]["code"] == "BUILD_FAILED"
        # stopped right after the failure — no deploy step attempted
        assert "compose_deploy" not in runtime.calls
        assert await store.get_current_release("demo", "staging") is None


class TestFailureAfterRuntimeChange:
    async def test_recovers_previous_release(self, setup) -> None:
        config, store, digest = setup
        await baseline_release(store, "r-baseline")
        runtime = FakeRuntime(fail_at={"health_check"})
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=runtime
        )
        _plan, job = await admit_deploy_job(
            config, store, baseline_id="r-baseline", template_digest=digest
        )
        status, result, recovery, _staged = await workflow.run(job)
        assert status == JobStatus.ROLLED_BACK
        assert recovery is not None
        assert recovery["status"] == "restored_previous"
        assert recovery["baseline_release_id"] == "r-baseline"
        # result honestly reports failure even though recovery succeeded
        assert result["error"]["code"] == "BUILD_FAILED" or "error" in result
        assert "compose_deploy" in runtime.calls
        assert "restore_previous" in runtime.calls

    async def test_no_baseline_stops_initial_deployment(self, setup) -> None:
        config, store, digest = setup
        runtime = FakeRuntime(fail_at={"health_check"})
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=runtime
        )
        _plan, job = await admit_deploy_job(config, store, baseline_id=None, template_digest=digest)
        status, _result, recovery, _staged = await workflow.run(job)
        assert status == JobStatus.FAILED_NO_BASELINE
        assert recovery is not None
        assert recovery["status"] == "stopped_initial"
        assert "stop_initial" in runtime.calls

    async def test_recovery_failure_blocks_target(self, setup) -> None:
        config, store, digest = setup
        await baseline_release(store, "r-baseline")
        runtime = FakeRuntime(fail_at={"health_check"}, fail_recover=True)
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=runtime
        )
        _plan, job = await admit_deploy_job(
            config, store, baseline_id="r-baseline", template_digest=digest
        )
        status, _result, recovery, _staged = await workflow.run(job)
        assert status == JobStatus.ROLLBACK_FAILED
        assert recovery is not None
        assert recovery["status"] == "recovery_failed"
        assert recovery["needs_attention"] is True


class TestStepPersistence:
    async def test_steps_recorded_with_details(self, setup) -> None:
        config, store, digest = setup
        runtime = FakeRuntime(fail_at={"image_import"})
        workflow = DeployWorkflow(
            config=config, store=store, step_executor=runtime
        )
        _plan, job = await admit_deploy_job(config, store, baseline_id=None, template_digest=digest)
        await workflow.run(job)
        steps = await store.list_steps(job.job_id)
        by_name = OrderedDict((s.name, s) for s in steps)
        assert by_name["preflight"].status == "succeeded"
        assert by_name["source"].status == "succeeded"
        assert by_name["import"].status == "failed"
        # failure stops the workflow: no later step rows are created
        assert "deploy" not in by_name
