"""ops_history and retention enforcement tests (optimization plan A).

Covers the acceptance criteria from
``plans/OPTIMIZATION_A_OPERATIONAL_VISIBILITY.md``:

* bounded releases/jobs/events history with rollback eligibility and
  cursor pagination; strict parameter validation;
* retention cleanup: expired idempotency keys / plans / diagnostic jobs
  are removed; jobs referenced by releases and blocking-status jobs are
  protected; steps cascade; log directories follow their jobs;
* the Runner triggers the retention pass on schedule.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from drawbridge.config.loader import load_config_from_dir
from drawbridge.errors import DrawbridgeError
from drawbridge.gateway.service import GatewayService
from drawbridge.runner.loop import Runner
from drawbridge.runner.retention import run_retention
from drawbridge.state.db import Database
from drawbridge.state.records import ReleaseRecord
from drawbridge.state.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"

OLD = time.time() - 40 * 86400  # well past every retention window


@pytest.fixture()
async def env(tmp_path: Path):
    config = load_config_from_dir(CONFIG_DIR)
    config.main.paths.state_dir = str(tmp_path / "state")
    config.main.paths.lock_dir = str(tmp_path / "locks")
    config.main.paths.log_dir = str(tmp_path / "logs")
    config.main.concurrency.min_deploy_interval_seconds = 0
    database = Database(tmp_path / "state" / "state.db")
    await database.connect()
    await database.initialize()
    store = Store(database)
    service = GatewayService(config, store)
    yield config, store, service, tmp_path
    await database.close()


async def seed_release(store: Store, *, app="demo", sha="a", status="succeeded") -> str:
    record = ReleaseRecord(
        release_id=f"r-{sha}-{int(time.time() * 1000000)}",
        app=app,
        environment="staging",
        plan_id=None,
        job_id=None,
        commit_sha=sha * 40,
        image_id=f"sha256:{sha * 64}",
        image_tag=None,
        config_digest="d" * 64,
        status=status,
        rollback_of=None,
        compose_path=None,
        deploy_dir=None,
        evidence={},
        created_at=time.time(),
        verified_at=time.time(),
    )
    await store.record_release(record)
    return record.release_id


# ---------------------------------------------------------------------------
# ops_history
# ---------------------------------------------------------------------------


async def test_history_releases_marks_current_and_rollback_eligibility(env) -> None:
    _, store, service, _ = env
    first = await seed_release(store, sha="a")
    second = await seed_release(store, sha="b")  # current

    result = await service.ops_history("demo", "staging", what="releases", limit=10)
    data = result["data"]
    assert data["what"] == "releases"
    assert len(data["releases"]) == 2
    by_id = {r["release_id"]: r for r in data["releases"]}
    assert by_id[second]["is_current"] is True
    assert by_id[second]["rollback_eligible"] is False
    assert by_id[first]["is_current"] is False
    assert by_id[first]["rollback_eligible"] is True
    # Newest first.
    assert data["releases"][0]["release_id"] == second


async def test_history_jobs_and_events_pagination(env) -> None:
    _, store, service, _ = env
    for i in range(5):
        await store.admit_job(
            kind="restart",
            action="service_restart",
            app="demo",
            environment="staging",
            params={"seq": i},
            idempotency_key=f"hist-key-{i:04d}",
            config_digest="d" * 64,
            queue_timeout_seconds=600,
            deadline_seconds=120,
            max_queued=50,
            max_queued_per_target=5,
        )
    await store.append_event("job_admitted", app="demo", environment="staging", detail={"n": 1})

    jobs = await service.ops_history("demo", "staging", what="jobs", limit=2)
    assert len(jobs["data"]["jobs"]) == 2
    assert jobs["data"]["next_cursor"] is not None
    page2 = await service.ops_history(
        "demo", "staging", what="jobs", limit=2, cursor=jobs["data"]["next_cursor"]
    )
    ids_page1 = {j["job_id"] for j in jobs["data"]["jobs"]}
    ids_page2 = {j["job_id"] for j in page2["data"]["jobs"]}
    assert ids_page1 and ids_page2 and not (ids_page1 & ids_page2)

    events = await service.ops_history("demo", "staging", what="events", limit=1)
    assert len(events["data"]["events"]) == 1
    assert events["data"]["events"][0]["kind"] == "job_admitted"


async def test_history_strict_validation(env) -> None:
    _, _, service, _ = env
    for kwargs in (
        {"what": "secrets"},
        {"limit": 0},
        {"limit": 51},
        {"limit": True},
        {"limit": "10"},
    ):
        with pytest.raises(DrawbridgeError) as excinfo:
            await service.ops_history("demo", "staging", **kwargs)
        assert excinfo.value.code == "INVALID_PARAMETER"
    # Cursor validation applies to the cursor-consuming views.
    with pytest.raises(DrawbridgeError) as excinfo:
        await service.ops_history("demo", "staging", what="jobs", cursor="not-a-timestamp")
    assert excinfo.value.code == "INVALID_PARAMETER"


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def seed_job(
    store: Store,
    *,
    kind: str,
    status: str,
    finished: float | None,
    key: str | None = None,
) -> str:
    job = await store.admit_job(
        kind=kind,
        action={
            "diagnostic": "git_status",
            "restart": "service_restart",
            "deploy": "deploy_verify",
        }.get(kind, "test_suite"),
        app="demo",
        environment="staging",
        params={"seed": key or kind},
        idempotency_key=key,
        config_digest="d" * 64,
        queue_timeout_seconds=600,
        deadline_seconds=120,
        max_queued=50,
        max_queued_per_target=5,
    )
    if status != "queued":
        async with store.db.write_lock():
            await store.db.conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE job_id = ?",
                (status, finished, job.job_id),
            )
            await store.db.conn.commit()
    return job.job_id


async def test_retention_cleanup_protects_invariants(env) -> None:
    config, store, _, _ = env

    # 1) diagnostic job, terminal, old -> removed (with its step).
    diag = await seed_job(store, kind="diagnostic", status="succeeded", finished=OLD)
    await store.start_step(diag, 0, "s0")

    # 2) old failed restart job, NOT referenced by a release -> removed.
    await seed_job(store, kind="restart", status="failed", finished=OLD, key="restart-old-1")

    # 3) old failed deploy job, referenced by a release row -> kept.
    await seed_release(store, sha="c")
    referenced = await seed_job(
        store, kind="deploy", status="failed", finished=OLD, key="deploy-kept-1"
    )
    async with store.db.write_lock():
        await store.db.conn.execute(
            "UPDATE releases SET job_id = ? WHERE release_id ="
            " (SELECT release_id FROM releases WHERE app='demo' LIMIT 1)",
            (referenced,),
        )
        await store.db.conn.commit()

    # 5) fresh job -> kept (seeded BEFORE the blocking job: a blocked
    # target refuses any further mutation admission).
    fresh = await seed_job(
        store, kind="restart", status="succeeded", finished=time.time(), key="restart-new-1"
    )

    # 4) blocking-status job -> kept regardless of age.
    await seed_job(
        store, kind="deploy", status="needs_attention", finished=OLD, key="deploy-block-1"
    )

    # An old idempotency key and an old expired plan.
    await store.create_plan(
        app="demo",
        environment="staging",
        workflow="deploy_verify",
        source_mode="local",
        git_ref="refs/heads/main",
        commit_sha="a" * 40,
        config_digest="d" * 64,
        baseline_release_id=None,
        ttl_seconds=900,
        params={},
    )
    async with store.db.write_lock():
        await store.db.conn.execute(
            "UPDATE idempotency_keys SET created_at = ? WHERE key = ?",
            (OLD, "restart-old-1"),
        )
        await store.db.conn.execute("UPDATE plans SET expires_at = ?", (OLD,))
        await store.db.conn.commit()

    log_dir = Path(config.main.paths.log_dir) / diag
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "out.log").write_text("x", encoding="utf-8")

    counts = await run_retention(config, store)
    assert counts["jobs"] == 2  # the old diagnostic + the old unreferenced restart
    assert counts["steps"] >= 1
    assert counts["plans"] == 1
    assert counts["log_dirs"] == 1
    assert not log_dir.exists()

    remaining = {r.job_id for r in await store.list_jobs("demo", "staging", limit=50)}
    assert referenced in remaining  # release-referenced
    assert fresh in remaining
    assert len(remaining) == 3  # + the blocking one

    # A second pass removes nothing and writes no further event.
    events_before = len(await store.list_events("demo", "staging", limit=1000))
    counts2 = await run_retention(config, store)
    assert counts2["jobs"] == 0 and counts2["log_dirs"] == 0
    events_after = len(await store.list_events("demo", "staging", limit=1000))
    assert events_after == events_before
    # The first pass recorded its global audit event.
    async with store.db.conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = 'retention_cleanup'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row["n"] == 1


async def test_retention_never_touches_active_jobs(env) -> None:
    config, store, _, _ = env
    queued = await seed_job(
        store, kind="restart", status="queued", finished=None, key="restart-live-1"
    )
    running = await seed_job(
        store, kind="restart", status="running", finished=None, key="restart-live-2"
    )
    counts = await run_retention(config, store)
    remaining = {r.job_id for r in await store.list_jobs("demo", "staging", limit=50)}
    assert queued in remaining and running in remaining
    assert counts["jobs"] == 0


async def test_runner_tick_triggers_retention_on_schedule(env) -> None:
    config, store, _, _ = env
    # Due immediately: backdate the schedule and let one tick run it.
    runner = Runner(config, store, poll_interval=0.01)
    runner._next_retention_at = 0.0
    await seed_job(store, kind="diagnostic", status="succeeded", finished=OLD)
    await runner.tick_once()
    async with store.db.conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = 'retention_cleanup'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row["n"] == 1
    await runner.stop()
