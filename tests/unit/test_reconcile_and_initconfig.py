"""Stale running-job reconciliation and init-config tests (plan B)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from drawbridge.config.loader import load_config_from_dir
from drawbridge.entries.initconfig_main import _default_source_config_dir, init_config
from drawbridge.runner.loop import Runner
from drawbridge.state.db import Database
from drawbridge.state.records import JobStatus
from drawbridge.state.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"

OLD = time.time() - 3600


@pytest.fixture()
async def env(tmp_path: Path):
    config = load_config_from_dir(CONFIG_DIR)
    config.main.paths.state_dir = str(tmp_path / "state")
    config.main.paths.lock_dir = str(tmp_path / "locks")
    config.main.paths.log_dir = str(tmp_path / "logs")
    database = Database(tmp_path / "state" / "state.db")
    await database.connect()
    await database.initialize()
    store = Store(database)
    yield config, store, tmp_path
    await database.close()


async def seed_running(store: Store, *, heartbeat: float | None, key: str) -> str:
    job = await store.admit_job(
        kind="restart",
        action="service_restart",
        app="demo",
        environment="staging",
        params={},
        idempotency_key=key,
        config_digest="d" * 64,
        queue_timeout_seconds=600,
        deadline_seconds=120,
        max_queued=50,
        max_queued_per_target=5,
    )
    await store.claim_next_job(owner="dead-runner", kinds=["restart"])
    async with store.db.write_lock():
        await store.db.conn.execute(
            "UPDATE jobs SET heartbeat_at = ?, started_at = ? WHERE job_id = ?",
            (heartbeat, heartbeat if heartbeat is not None else OLD, job.job_id),
        )
        await store.db.conn.commit()
    return job.job_id


async def test_stale_running_flips_to_needs_attention(env) -> None:
    config, store, _ = env
    stale = await seed_running(store, heartbeat=OLD, key="stale-run-0001")
    fresh = await seed_running(store, heartbeat=time.time(), key="fresh-run-0001")

    runner = Runner(config, store, poll_interval=0.01)
    runner._next_reconcile_at = 0.0  # due on the first tick
    await runner.tick_once()
    await runner.stop()

    stale_job = await store.get_job(stale)
    assert stale_job.status == JobStatus.NEEDS_ATTENTION
    assert stale_job.result["error"]["code"] == "NEEDS_ATTENTION"

    fresh_job = await store.get_job(fresh)
    assert fresh_job.status == JobStatus.RUNNING  # healthy heartbeat untouched

    async with store.db.conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = 'job_reconciled'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row["n"] == 1  # one audit event, append-only


async def test_reconciled_job_blocks_target(env) -> None:
    _, store, _ = env
    from drawbridge.errors import DrawbridgeError

    await seed_running(store, heartbeat=OLD, key="stale-run-0002")
    reconciled = await store.reconcile_stale_running(now=time.time(), max_age_seconds=900)
    assert reconciled

    with pytest.raises(DrawbridgeError) as excinfo:
        await store.admit_job(
            kind="restart",
            action="service_restart",
            app="demo",
            environment="staging",
            params={},
            idempotency_key="blocked-key-0001",
            config_digest="d" * 64,
            queue_timeout_seconds=600,
            deadline_seconds=120,
            max_queued=50,
            max_queued_per_target=5,
        )
    assert excinfo.value.code == "NEEDS_ATTENTION"


async def test_reconcile_ignores_terminal_jobs(env) -> None:
    _, store, _ = env
    job = await store.admit_job(
        kind="restart",
        action="service_restart",
        app="demo",
        environment="staging",
        params={},
        idempotency_key="done-run-00001",
        config_digest="d" * 64,
        queue_timeout_seconds=600,
        deadline_seconds=120,
        max_queued=50,
        max_queued_per_target=5,
    )
    await store.finish_job(job.job_id, status=JobStatus.SUCCEEDED, result={}, owner=None)
    async with store.db.write_lock():
        await store.db.conn.execute(
            "UPDATE jobs SET heartbeat_at = ? WHERE job_id = ?", (OLD, job.job_id)
        )
        await store.db.conn.commit()
    reconciled = await store.reconcile_stale_running(now=time.time(), max_age_seconds=900)
    assert reconciled == []


def test_init_config_generates_loadable_bundle(tmp_path: Path) -> None:
    source = _default_source_config_dir()
    if source is None:  # pragma: no cover - repository checkout required
        pytest.skip("repository config bundle not available")
    created = init_config(
        tmp_path / "bundle",
        root=Path("/srv/drawbridge"),
        host="192.168.18.7",
        port=8787,
        cidr="192.168.0.0/16",
        source_config_dir=source,
    )
    names = {p.name for p in created}
    assert {"drawbridge.yaml", "apps.yaml", "operations.yaml", "workflows.yaml"} <= names
    # D13: the skeleton ships the compose interpolation pin.
    assert (tmp_path / "bundle" / "compose" / "empty.env").is_file()

    config = load_config_from_dir(tmp_path / "bundle")
    assert config.environment("demo", "staging").runtime == "compose"
    assert config.digest
    # Placeholders must be visible to the administrator, not silently valid.
    apps_text = (tmp_path / "bundle" / "apps.yaml").read_text(encoding="utf-8")
    assert "REPLACE_WITH_REPO_ORIGIN" in apps_text

    with pytest.raises(FileExistsError):
        init_config(
            tmp_path / "bundle",
            root=Path("/srv/drawbridge"),
            host="192.168.18.7",
            port=8787,
            cidr="192.168.0.0/16",
            source_config_dir=source,
        )
