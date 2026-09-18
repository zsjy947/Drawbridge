"""Runner loop tests: dispatch, maintenance gating, terminal states."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from drawbridge.config.loader import load_config_from_dir
from drawbridge.runner.loop import Runner
from drawbridge.state.db import Database
from drawbridge.state.records import JobKind, JobStatus
from drawbridge.state.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"


@pytest.fixture()
async def setup(tmp_path: Path):
    config = load_config_from_dir(CONFIG_DIR)
    config.main.paths.state_dir = str(tmp_path / "state")
    config.main.paths.lock_dir = str(tmp_path / "locks")
    config.main.paths.log_dir = str(tmp_path / "logs")
    config.main.concurrency.min_deploy_interval_seconds = 0
    database = Database(tmp_path / "state" / "state.db")
    await database.connect()
    await database.initialize()
    store = Store(database)
    yield config, store
    await database.close()


async def admit(store: Store, *, kind: str, action: str, params: dict[str, Any] | None = None):
    return await store.admit_job(
        kind=kind,
        action=action,
        app="demo",
        environment="staging",
        params=params or {},
        idempotency_key=f"test-{action}-{kind}"[:32] + "-xxxx",
        config_digest="d" * 64,
        queue_timeout_seconds=60,
        deadline_seconds=60,
        max_queued=50,
        max_queued_per_target=5,
    )


class TestDiagnosticDispatch:
    async def test_diagnostic_job_executes_and_succeeds(self, setup) -> None:
        config, store = setup

        async def ping_handler(ctx, job):
            return {"pong": True, "job": job.job_id}

        runner = Runner(
            config,
            store,
            handler_registry={"ping": ping_handler},
            diagnostic_actions=frozenset({"ping"}),
        )
        job = await admit(store, kind=JobKind.DIAGNOSTIC, action="ping")
        await runner._tick()
        for _ in range(50):
            current = await store.get_job(job.job_id)
            if current.status == JobStatus.SUCCEEDED:
                break
            await runner._tick()
            import asyncio

            await asyncio.sleep(0.05)
        current = await store.get_job(job.job_id)
        assert current.status == JobStatus.SUCCEEDED
        assert current.result is not None
        assert current.result["pong"] is True
        assert current.owner == runner.instance_id

    async def test_unknown_action_fails_with_unknown_operation(self, setup) -> None:
        config, store = setup
        runner = Runner(config, store, handler_registry={})
        job = await admit(store, kind=JobKind.DIAGNOSTIC, action="ghost_op")
        await runner._tick()
        await runner.drain()
        current = await store.get_job(job.job_id)
        assert current.status == JobStatus.FAILED
        assert current.result is not None
        assert current.result["error"]["code"] == "UNKNOWN_OPERATION"

    async def test_handler_exception_reported_as_internal(self, setup) -> None:
        config, store = setup

        async def boom(ctx, job):
            raise RuntimeError("unexpected")

        runner = Runner(
            config,
            store,
            handler_registry={"boom": boom},
            diagnostic_actions=frozenset({"boom"}),
        )
        job = await admit(store, kind=JobKind.DIAGNOSTIC, action="boom")
        await runner._tick()
        await runner.drain()
        current = await store.get_job(job.job_id)
        assert current.status == JobStatus.FAILED
        assert "unexpected" in (current.result or {}).get("error", {}).get("message", "")


class TestMaintenanceGate:
    async def test_mutations_not_dispatched_but_diagnostics_run(
        self, setup
    ) -> None:
        config, store = setup
        await store.db.set_control("maintenance", "true")

        async def ping_handler(ctx, job):
            return {"pong": True}

        async def fake_restart(ctx, job):
            return {"restarted": job.params["service"]}

        runner = Runner(
            config,
            store,
            handler_registry={
                "ping": ping_handler,
                "service_restart": fake_restart,
            },
            diagnostic_actions=frozenset({"ping"}),
        )
        mutation = await admit(
            store, kind=JobKind.RESTART, action="service_restart",
            params={"service": "api", "reason": "test"},
        )
        diagnostic = await admit(store, kind=JobKind.DIAGNOSTIC, action="ping")
        await runner._tick()
        import asyncio

        for _ in range(50):
            diag_now = await store.get_job(diagnostic.job_id)
            if diag_now.status == JobStatus.SUCCEEDED:
                break
            await runner._tick()
            await asyncio.sleep(0.05)

        mutation_now = await store.get_job(mutation.job_id)
        diag_now = await store.get_job(diagnostic.job_id)
        assert mutation_now.status == JobStatus.QUEUED  # held by maintenance
        assert diag_now.status == JobStatus.SUCCEEDED  # reads still served

        # lifting maintenance lets the mutation dispatch
        await store.db.set_control("maintenance", "false")
        for _ in range(50):
            mutation_now = await store.get_job(mutation.job_id)
            if mutation_now.status == JobStatus.SUCCEEDED:
                break
            await runner._tick()
            await asyncio.sleep(0.05)
        assert mutation_now.status == JobStatus.SUCCEEDED
        assert mutation_now.result == {"restarted": "api"}


class TestMutationSerialization:
    async def test_mutations_run_serially_per_target(self, setup) -> None:
        config, store = setup
        running: list[int] = []
        max_observed = {"value": 0}

        async def slow_handler(ctx, job):
            import asyncio

            running.append(1)
            max_observed["value"] = max(max_observed["value"], len(running))
            await asyncio.sleep(0.05)
            running.pop()
            return {"done": job.action}

        runner = Runner(
            config,
            store,
            handler_registry={
                "service_restart": slow_handler,
            },
            diagnostic_actions=frozenset(),
        )
        jobs = [
            await store.admit_job(
                kind=JobKind.RESTART,
                action="service_restart",
                app="demo",
                environment="staging",
                params={"n": i},
                idempotency_key=f"serial-{i}-xxxxxxxxx",
                config_digest="d" * 64,
                queue_timeout_seconds=60,
                deadline_seconds=60,
                max_queued=50,
                max_queued_per_target=5,
            )
            for i in range(4)
        ]
        for _ in range(100):
            statuses = [await store.get_job(j.job_id) for j in jobs]
            if all(s.status == JobStatus.SUCCEEDED for s in statuses):
                break
            await runner._tick()
            import asyncio

            await asyncio.sleep(0.02)
        assert max_observed["value"] == 1  # max_running_jobs=1 by default
        statuses = [await store.get_job(j.job_id) for j in jobs]
        assert all(s.status == JobStatus.SUCCEEDED for s in statuses)


async def admit_deploy(config, store: Store) -> Any:
    """A deploy job bound to a fresh, valid plan for the demo app."""
    plan = await store.create_plan(
        app="demo",
        environment="staging",
        workflow="deploy_verify",
        source_mode="fetch",
        git_ref="refs/heads/main",
        commit_sha="a" * 40,
        config_digest=config.digest,
        baseline_release_id=None,
        ttl_seconds=900,
        params={},
    )
    return await store.admit_job(
        kind=JobKind.DEPLOY,
        action="deploy_verify",
        app="demo",
        environment="staging",
        params={"plan_id": plan.plan_id},
        idempotency_key=f"deploy-{plan.plan_id[:8]}",
        config_digest=config.digest,
        queue_timeout_seconds=60,
        deadline_seconds=60,
        max_queued=50,
        max_queued_per_target=5,
        plan_id=plan.plan_id,
    )


class TestDeployJobTermination:
    async def test_default_runtime_failure_terminates_job(self, setup) -> None:
        """No mutation_handler injection: the default DeployRuntime refuses
        off-target hosts and the job must land in a terminal state — the
        stuck-in-running bug this replaces lost such failures."""
        config, store = setup
        runner = Runner(config, store)
        job = await admit_deploy(config, store)
        for _ in range(50):
            current = await store.get_job(job.job_id)
            if current.status in JobStatus.TERMINAL:
                break
            await runner._tick()
            import asyncio

            await asyncio.sleep(0.02)
        current = await store.get_job(job.job_id)
        assert current.status == JobStatus.FAILED
        assert current.result is not None
        assert current.result["error"]["code"] == "UNSUPPORTED"

    async def test_unknown_workflow_terminates_job(self, setup) -> None:
        config, store = setup
        runner = Runner(config, store)
        job = await admit(
            store, kind=JobKind.DEPLOY, action="no_such_workflow", params={"plan_id": "x"}
        )
        for _ in range(50):
            current = await store.get_job(job.job_id)
            if current.status in JobStatus.TERMINAL:
                break
            await runner._tick()
            import asyncio

            await asyncio.sleep(0.02)
        current = await store.get_job(job.job_id)
        assert current.status == JobStatus.FAILED
        assert current.result is not None
        assert current.result["error"]["code"] == "CONFIG_INVALID"

    async def test_cancelled_deploy_needs_attention(self, setup) -> None:
        config, store = setup

        async def slow_runtime(operation: str, state: Any, params: Any = None) -> dict:
            import asyncio

            await asyncio.sleep(30)
            return {}

        runner = Runner(config, store, mutation_handler=slow_runtime)
        job = await admit_deploy(config, store)
        await runner._tick()
        import asyncio

        for _ in range(100):
            current = await store.get_job(job.job_id)
            if current.status == JobStatus.RUNNING:
                break
            await asyncio.sleep(0.02)
        assert (await store.get_job(job.job_id)).status == JobStatus.RUNNING

        await runner.stop()
        current = await store.get_job(job.job_id)
        assert current.status == JobStatus.NEEDS_ATTENTION
        assert current.result is not None
        assert current.result["error"]["code"] == "NEEDS_ATTENTION"

    async def test_job_finish_writes_audit_event(self, setup) -> None:
        config, store = setup
        runner = Runner(config, store)
        job = await admit_deploy(config, store)
        for _ in range(50):
            current = await store.get_job(job.job_id)
            if current.status in JobStatus.TERMINAL:
                break
            await runner._tick()
            import asyncio

            await asyncio.sleep(0.02)
        async with store.db.conn.execute(
            "SELECT kind, detail_json FROM events WHERE job_id = ?", (job.job_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        kinds = {row["kind"] for row in rows}
        assert "job_finished" in kinds
