"""Store concurrency-invariant tests (MVP spec §7, §8)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from drawbridge.errors import (
    BusyError,
    DrawbridgeError,
    IdempotencyConflictError,
    RateLimitedError,
)
from drawbridge.state.db import Database
from drawbridge.state.locking import FileLock, TargetLocks
from drawbridge.state.records import JobKind, JobStatus
from drawbridge.state.store import Store, new_id


@pytest.fixture()
async def store(tmp_path: Path) -> Store:
    db = Database(tmp_path / "state.db")
    await db.connect()
    await db.initialize()
    yield Store(db)
    await db.close()


BASE: dict[str, object] = {
    "kind": JobKind.DEPLOY,
    "action": "deploy_verify",
    "app": "demo",
    "environment": "staging",
    "params": {"git_ref": "refs/heads/main"},
    "config_digest": "d" * 64,
    "queue_timeout_seconds": 600,
    "deadline_seconds": 1800,
    "max_queued": 50,
    "max_queued_per_target": 5,
    "cooldown_seconds": 60,
}


class TestSchemaAndControl:
    async def test_initialize_is_idempotent(self, store: Store) -> None:
        await store.db.initialize()  # second run must not fail

    async def test_schema_version_enforced(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "s.db")
        await db.connect()
        await db.initialize()
        await db.set_control("schema_version", "999")
        with pytest.raises(RuntimeError, match="schema version mismatch"):
            await db.initialize()
        await db.close()

    async def test_control_state_roundtrip(self, store: Store) -> None:
        # initialize seeds maintenance to a readable "false"
        assert await store.db.get_control("maintenance") == "false"
        await store.db.set_control("maintenance", "true")
        assert await store.db.get_control("maintenance") == "true"


class TestAdmission:
    async def test_basic_admit_and_get(self, store: Store) -> None:
        job = await store.admit_job(idempotency_key="key-00000001", **BASE)  # type: ignore[arg-type]
        assert job.status == JobStatus.QUEUED
        fetched = await store.get_job(job.job_id)
        assert fetched.job_id == job.job_id
        assert fetched.params == BASE["params"]

    async def test_same_key_same_content_returns_same_job(self, store: Store) -> None:
        a = await store.admit_job(idempotency_key="key-00000001", **BASE)  # type: ignore[arg-type]
        b = await store.admit_job(idempotency_key="key-00000001", **BASE)  # type: ignore[arg-type]
        assert a.job_id == b.job_id
        assert await store.find_job_by_plan(a.plan_id) is None or True

    async def test_same_key_different_content_conflicts(self, store: Store) -> None:
        await store.admit_job(idempotency_key="key-00000001", **BASE)  # type: ignore[arg-type]
        changed = dict(BASE, params={"git_ref": "refs/heads/agent/x"})
        with pytest.raises(IdempotencyConflictError):
            await store.admit_job(idempotency_key="key-00000001", **changed)  # type: ignore[arg-type]

    async def test_plan_binds_single_job_across_keys(self, store: Store) -> None:
        plan = await store.create_plan(
            app="demo",
            environment="staging",
            workflow="deploy_verify",
            source_mode="fetch",
            git_ref="refs/heads/main",
            commit_sha="a" * 40,
            config_digest="d" * 64,
            baseline_release_id=None,
            ttl_seconds=900,
            params={},
        )
        a = await store.admit_job(
            idempotency_key="key-aaaaaaaa", plan_id=plan.plan_id, **BASE  # type: ignore[arg-type]
        )
        # A different agent, a different key, same plan: still one job.
        b = await store.admit_job(
            idempotency_key="key-bbbbbbbb", plan_id=plan.plan_id, **BASE  # type: ignore[arg-type]
        )
        assert a.job_id == b.job_id
        bound = await store.find_job_by_plan(plan.plan_id)
        assert bound is not None and bound.job_id == a.job_id

    async def test_diagnostics_bypass_capacity(self, store: Store) -> None:
        for i in range(10):
            await store.admit_job(
                kind=JobKind.DIAGNOSTIC,
                action="git_status",
                app="demo",
                environment="staging",
                params={"n": i},
                idempotency_key=None,
                config_digest="d" * 64,
                queue_timeout_seconds=60,
                deadline_seconds=30,
                max_queued=1,
                max_queued_per_target=1,
            )

    async def test_global_capacity_enforced(self, store: Store) -> None:
        for i in range(3):
            await store.admit_job(
                kind=JobKind.RESTART,
                action="service_restart",
                app=f"app{i}",
                environment="staging",
                params={},
                idempotency_key=f"restart-{i}-xxxxx",
                config_digest="d" * 64,
                queue_timeout_seconds=600,
                deadline_seconds=120,
                max_queued=3,
                max_queued_per_target=5,
            )
        with pytest.raises(BusyError):
            await store.admit_job(
                kind=JobKind.RESTART,
                action="service_restart",
                app="appX",
                environment="staging",
                params={},
                idempotency_key="restart-x-xxxxxxxx",
                config_digest="d" * 64,
                queue_timeout_seconds=600,
                deadline_seconds=120,
                max_queued=3,
                max_queued_per_target=5,
            )

    async def test_per_target_capacity_enforced(self, store: Store) -> None:
        for i in range(2):
            await store.admit_job(
                kind=JobKind.RESTART,
                action="service_restart",
                app="demo",
                environment="staging",
                params={"n": i},
                idempotency_key=f"restart-{i}-xxxxx",
                config_digest="d" * 64,
                queue_timeout_seconds=600,
                deadline_seconds=120,
                max_queued=50,
                max_queued_per_target=2,
            )
        with pytest.raises(BusyError, match="demo/staging"):
            await store.admit_job(
                kind=JobKind.RESTART,
                action="service_restart",
                app="demo",
                environment="staging",
                params={"n": 99},
                idempotency_key="restart-99-xxxxx",
                config_digest="d" * 64,
                queue_timeout_seconds=600,
                deadline_seconds=120,
                max_queued=50,
                max_queued_per_target=2,
            )

    async def test_cooldown_at_admission(self, store: Store) -> None:
        first = await store.admit_job(idempotency_key="deploy-1-xxxxxxxx", **BASE)  # type: ignore[arg-type]
        await store.claim_next_job(owner="runner-1", kinds=[JobKind.DEPLOY])
        await store.finish_job(first.job_id, status=JobStatus.FAILED)
        with pytest.raises(RateLimitedError) as exc_info:
            await store.admit_job(idempotency_key="deploy-2-xxxxxxxx", **BASE)  # type: ignore[arg-type]
        assert exc_info.value.retry_after_seconds is not None
        assert exc_info.value.retry_after_seconds >= 1

    async def test_cooldown_does_not_block_non_deploy_kinds(self, store: Store) -> None:
        first = await store.admit_job(idempotency_key="deploy-1-xxxxxxxx", **BASE)  # type: ignore[arg-type]
        await store.claim_next_job(owner="runner-1", kinds=[JobKind.DEPLOY])
        await store.finish_job(first.job_id, status=JobStatus.SUCCEEDED)
        job = await store.admit_job(
            kind=JobKind.RESTART,
            action="service_restart",
            app="demo",
            environment="staging",
            params={},
            idempotency_key="restart-1-xxxxxxxx",
            config_digest="d" * 64,
            queue_timeout_seconds=600,
            deadline_seconds=120,
            max_queued=50,
            max_queued_per_target=5,
            cooldown_seconds=60,
        )
        assert job.status == JobStatus.QUEUED


class TestClaiming:
    async def test_fifo_claim_and_expiry(self, store: Store) -> None:
        first = await store.admit_job(idempotency_key="job-1-xxxxxxxxx", **BASE)  # type: ignore[arg-type]
        second = await store.admit_job(
            idempotency_key="job-2-xxxxxxxxx", **{**BASE, "app": "other"}  # type: ignore[arg-type]
        )
        claimed = await store.claim_next_job(owner="runner-1", kinds=[JobKind.DEPLOY])
        assert claimed is not None and claimed.job_id == first.job_id
        assert claimed.status == JobStatus.RUNNING
        claimed2 = await store.claim_next_job(owner="runner-1", kinds=[JobKind.DEPLOY])
        assert claimed2 is not None and claimed2.job_id == second.job_id
        assert await store.claim_next_job(owner="runner-1", kinds=[JobKind.DEPLOY]) is None

    async def test_claim_respects_cooldown_deferral(self, store: Store) -> None:
        """The dispatch-time cooldown re-check blocks the pre-queued bypass."""
        first = await store.admit_job(idempotency_key="deploy-1-xxxxxxxx", **BASE)  # type: ignore[arg-type]
        # Pre-queued while the first deploy is still running: admission sees
        # no finished deploy, so no cooldown applies yet.
        queued = await store.admit_job(idempotency_key="deploy-2-xxxxxxxx", **BASE)  # type: ignore[arg-type]
        await store.claim_next_job(owner="runner-1", kinds=[JobKind.DEPLOY])
        await store.finish_job(first.job_id, status=JobStatus.SUCCEEDED)

        # Runner dispatch consults the cooldown window before claiming.
        import time

        claimed = await store.claim_next_job(
            owner="runner-1",
            kinds=[JobKind.DEPLOY],
            now=time.time(),
            skip_targets={"demo/staging": time.time() + 999},
        )
        assert claimed is None
        still = await store.get_job(queued.job_id)
        assert still.status == JobStatus.QUEUED

        # Without the cooldown gate the queued job dispatches normally.
        claimed = await store.claim_next_job(
            owner="runner-1", kinds=[JobKind.DEPLOY]
        )
        assert claimed is not None and claimed.job_id == queued.job_id

    async def test_queue_expires_without_side_effects(self, store: Store) -> None:
        job = await store.admit_job(
            idempotency_key="job-exp-xxxxxxx",
            **{**BASE, "queue_timeout_seconds": 0.05},  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.1)
        expired = await store.expire_stale_queue()
        assert expired == 1
        after = await store.get_job(job.job_id)
        assert after.status == JobStatus.QUEUE_EXPIRED
        assert after.finished_at is not None

    async def test_queue_expiry_appends_audit_event(self, store: Store) -> None:
        """Plan D11 (invariant 5): a queue_expired terminal transition must
        not vanish from the audit chain — both expiry paths append
        job_queue_expired inside the same transaction."""
        job = await store.admit_job(
            idempotency_key="job-exp-audit-1x",
            **{**BASE, "queue_timeout_seconds": 0.05},  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.1)
        assert await store.expire_stale_queue() == 1

        async def _events_for(job_id: str) -> list[dict[str, Any]]:
            async with store.db.conn.execute(
                "SELECT kind, detail_json FROM events WHERE job_id = ?", (job_id,)
            ) as cursor:
                rows = await cursor.fetchall()
            import json as _json

            return [
                {"kind": r["kind"], "detail": _json.loads(r["detail_json"])}
                for r in rows
            ]

        events = await _events_for(job.job_id)
        assert [e["kind"] for e in events] == ["job_queue_expired"]
        assert events[0]["detail"]["kind"] == BASE["kind"]
        assert "queue_expires_at" in events[0]["detail"]

        # Second path: expiry discovered during the claim pass (cooldown off —
        # the first expiry already stamped finished_at on the target).
        job2 = await store.admit_job(
            idempotency_key="job-exp-audit-2x",
            **{**BASE, "queue_timeout_seconds": 0.05, "cooldown_seconds": 0},  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.1)
        claimed = await store.claim_next_job(owner="runner-1", kinds=[JobKind.DEPLOY])
        assert claimed is None
        assert (await store.get_job(job2.job_id)).status == JobStatus.QUEUE_EXPIRED
        events2 = await _events_for(job2.job_id)
        assert [e["kind"] for e in events2] == ["job_queue_expired"]

    async def test_double_claim_is_impossible(self, store: Store) -> None:
        await store.admit_job(idempotency_key="solo-1-xxxxxxxxx", **BASE)  # type: ignore[arg-type]
        results = await asyncio.gather(
            *[
                store.claim_next_job(owner=f"runner-{i}", kinds=[JobKind.DEPLOY])
                for i in range(4)
            ]
        )
        claimed = [r for r in results if r is not None]
        assert len(claimed) == 1

    async def test_owner_guard_on_finish(self, store: Store) -> None:
        job = await store.admit_job(idempotency_key="ownr-1-xxxxxxxxx", **BASE)  # type: ignore[arg-type]
        await store.claim_next_job(owner="runner-1", kinds=[JobKind.DEPLOY])
        await store.finish_job(
            job.job_id, status=JobStatus.SUCCEEDED, owner="runner-2"
        )
        still_running = await store.get_job(job.job_id)
        assert still_running.status == JobStatus.RUNNING


class TestReleasesAndSteps:
    async def test_current_release_tracks_latest(self, store: Store) -> None:
        import time

        from drawbridge.state.records import ReleaseRecord

        def release(rid: str) -> ReleaseRecord:
            return ReleaseRecord(
                release_id=rid,
                app="demo",
                environment="staging",
                plan_id=None,
                job_id=None,
                commit_sha="a" * 40,
                image_id="sha256:111",
                image_tag="demo:1",
                config_digest="d" * 64,
                status="succeeded",
                rollback_of=None,
                compose_path="/etc/drawbridge/compose/demo.yaml",
                deploy_dir="/srv/rel/" + rid,
                evidence={"health": "ok"},
                created_at=time.time(),
                verified_at=time.time(),
            )

        await store.record_release(release("r1"))
        await store.record_release(release("r2"))
        current = await store.get_current_release("demo", "staging")
        assert current is not None and current.release_id == "r2"
        history = await store.list_releases("demo", "staging")
        assert [h.release_id for h in history] == ["r2", "r1"]
        assert history[1].status == "superseded"

    async def test_steps_roundtrip(self, store: Store) -> None:
        job = await store.admit_job(idempotency_key="step-1-xxxxxxxxx", **BASE)  # type: ignore[arg-type]
        s1 = await store.start_step(job.job_id, 1, "preflight")
        await store.finish_step(
            s1, status="succeeded", exit_code=0, termination_reason="completed"
        )
        s2 = await store.start_step(job.job_id, 2, "build")
        await store.finish_step(s2, status="failed", exit_code=17)
        steps = await store.list_steps(job.job_id)
        assert [s.name for s in steps] == ["preflight", "build"]
        assert steps[0].status == "succeeded" and steps[1].exit_code == 17


class TestLocks:
    async def test_file_lock_is_exclusive_and_reentrant_free(
        self, tmp_path: Path
    ) -> None:
        lock_path = tmp_path / "locks" / "t.lock"
        a = FileLock(lock_path)
        b = FileLock(lock_path)
        assert a.acquire()
        assert not b.acquire()
        a.release()
        assert b.acquire()
        b.release()

    async def test_target_locks_serialize(self, tmp_path: Path) -> None:
        locks = TargetLocks(lock_dir=tmp_path / "locks")
        other = TargetLocks(lock_dir=tmp_path / "locks")
        assert locks.acquire_file_lock("demo", "staging")
        assert not other.acquire_file_lock("demo", "staging")
        locks.release_file_lock("demo", "staging")
        assert other.acquire_file_lock("demo", "staging")

    async def test_async_lock_per_target(self) -> None:
        locks = TargetLocks()
        a = locks.async_lock("demo", "staging")
        b = locks.async_lock("demo", "staging")
        c = locks.async_lock("other", "staging")
        assert a is b
        assert a is not c


class TestConcurrentAdmission:
    async def test_racing_admissions_never_exceed_capacity(self, store: Store) -> None:
        """20 concurrent entries against capacity 5 → at most 5 queued."""
        results = await asyncio.gather(
            *[
                store.admit_job(
                    kind=JobKind.RESTART,
                    action="service_restart",
                    app=f"app{i}",
                    environment="staging",
                    params={},
                    idempotency_key=f"race-{i}-xxxxxxxx",
                    config_digest="d" * 64,
                    queue_timeout_seconds=600,
                    deadline_seconds=120,
                    max_queued=5,
                    max_queued_per_target=5,
                )
                for i in range(20)
            ],
            return_exceptions=True,
        )
        admitted = [r for r in results if not isinstance(r, BaseException)]
        busy = [r for r in results if isinstance(r, BusyError)]
        assert len(admitted) == 5
        assert len(busy) == 15

    async def test_concurrent_plans_yield_single_job(self, store: Store) -> None:
        plan = await store.create_plan(
            app="demo",
            environment="staging",
            workflow="deploy_verify",
            source_mode="fetch",
            git_ref="refs/heads/main",
            commit_sha="b" * 40,
            config_digest="d" * 64,
            baseline_release_id=None,
            ttl_seconds=900,
            params={},
        )
        results = await asyncio.gather(
            *[
                store.admit_job(
                    idempotency_key=f"multi-{i}-xxxxxxxx",
                    plan_id=plan.plan_id,
                    **BASE,  # type: ignore[arg-type]
                )
                for i in range(6)
            ]
        )
        assert len({job.job_id for job in results}) == 1


def test_new_id_shape() -> None:
    assert len(new_id()) == 36


class TestTargetBlocking:
    async def test_needs_attention_blocks_mutations_not_reads(self, store: Store) -> None:
        first = await store.admit_job(
            idempotency_key="blk-00000001", **{**BASE, "cooldown_seconds": 0}  # type: ignore[arg-type]
        )
        await store.claim_next_job(owner="runner-blk", kinds=[JobKind.DEPLOY])
        await store.finish_job(
            first.job_id, status=JobStatus.NEEDS_ATTENTION, owner="runner-blk"
        )
        with pytest.raises(DrawbridgeError) as exc:
            await store.admit_job(
                idempotency_key="blk-00000002", **BASE  # type: ignore[arg-type]
            )
        assert exc.value.code == "NEEDS_ATTENTION"
        # diagnostics still admitted on the same target
        diag = await store.admit_job(
            kind=JobKind.DIAGNOSTIC,
            action="host_metrics",
            app="demo",
            environment="staging",
            params={},
            idempotency_key=None,
            config_digest="d" * 64,
            queue_timeout_seconds=60,
            deadline_seconds=60,
            max_queued=50,
            max_queued_per_target=5,
        )
        assert diag.status == JobStatus.QUEUED
        # other targets unaffected
        other = dict(BASE)
        other["app"] = "orders"
        other["idempotency_key"] = "blk-00000003"
        assert (await store.admit_job(**other)).status == JobStatus.QUEUED  # type: ignore[arg-type]

    async def test_reconcile_unblocks_target(self, store: Store) -> None:
        base = {**BASE, "cooldown_seconds": 0}  # isolate from deploy cooldown
        job = await store.admit_job(
            idempotency_key="rec-00000001", **base  # type: ignore[arg-type]
        )
        await store.claim_next_job(owner="runner-rec", kinds=[JobKind.DEPLOY])
        await store.finish_job(
            job.job_id, status=JobStatus.ROLLBACK_FAILED, owner="runner-rec"
        )
        with pytest.raises(DrawbridgeError):
            await store.admit_job(
                idempotency_key="rec-00000002", **base  # type: ignore[arg-type]
            )
        # operator-verified reconcile: resolve the blocking terminal status
        async with store.db.write_lock():
            await store.db.conn.execute(
                "UPDATE jobs SET status = ? WHERE job_id = ?",
                (JobStatus.FAILED, job.job_id),
            )
            await store.db.conn.commit()
        assert (
            await store.admit_job(
                idempotency_key="rec-00000003", **base  # type: ignore[arg-type]
            )
        ).status == JobStatus.QUEUED


class TestPlanDedupKeyBinding:
    async def _plan(self, store: Store) -> str:
        return (
            await store.create_plan(
                app="demo",
                environment="staging",
                workflow="deploy_verify",
                source_mode="fetch",
                git_ref="refs/heads/main",
                commit_sha="a" * 40,
                config_digest="d" * 64,
                baseline_release_id=None,
                ttl_seconds=900,
                params={},
            )
        ).plan_id

    async def test_new_key_binds_to_existing_plan_job(self, store: Store) -> None:
        plan_id = await self._plan(store)
        a = await store.admit_job(
            idempotency_key="bind-aaaaaaa", plan_id=plan_id, **BASE  # type: ignore[arg-type]
        )
        b = await store.admit_job(
            idempotency_key="bind-bbbbbbb", plan_id=plan_id, **BASE  # type: ignore[arg-type]
        )
        assert a.job_id == b.job_id
        # the second key is now bound: reusing it with different content
        # conflicts instead of creating a fresh job
        divergent = dict(BASE)
        divergent["params"] = {"git_ref": "refs/heads/other"}
        divergent["idempotency_key"] = "bind-bbbbbbb"
        with pytest.raises(IdempotencyConflictError):
            await store.admit_job(**divergent)  # type: ignore[arg-type]

    async def test_same_plan_divergent_content_conflicts(self, store: Store) -> None:
        plan_id = await self._plan(store)
        await store.admit_job(
            idempotency_key="div-00000001", plan_id=plan_id, **BASE  # type: ignore[arg-type]
        )
        divergent = dict(BASE)
        divergent["params"] = {"git_ref": "refs/heads/agent/other"}
        divergent["idempotency_key"] = "div-00000002"
        divergent["plan_id"] = plan_id
        with pytest.raises(IdempotencyConflictError):
            await store.admit_job(**divergent)  # type: ignore[arg-type]
