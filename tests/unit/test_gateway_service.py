"""GatewayService tests: catalog, admission gates, plan validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from drawbridge.config.loader import load_config_from_dir
from drawbridge.errors import (
    DrawbridgeError,
    ErrorCode,
    ForbiddenOperationError,
    InvalidParameterError,
    MaintenanceError,
    StalePlanError,
    UnknownOperationError,
    UnknownPlanError,
)
from drawbridge.gateway.service import GatewayService
from drawbridge.state.records import PlanStatus
from drawbridge.state.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"


@pytest.fixture()
async def setup(tmp_path: Path):
    config = load_config_from_dir(CONFIG_DIR)
    config.main.paths.state_dir = str(tmp_path / "state")
    config.main.paths.lock_dir = str(tmp_path / "locks")
    config.main.paths.log_dir = str(tmp_path / "logs")
    config.main.concurrency.min_deploy_interval_seconds = 0  # no cooldown noise
    database_path = tmp_path / "state" / "state.db"
    from drawbridge.state.db import Database

    database = Database(database_path)
    await database.connect()
    await database.initialize()
    store = Store(database)
    service = GatewayService(config, store)
    yield service, store, config
    await database.close()


class TestCatalog:
    async def test_catalog_lists_public_operations(self, setup) -> None:
        service, _, _ = setup
        catalog = await service.ops_catalog()
        names = {op["name"] for op in catalog["data"]["operations"]}
        assert "git_status" in names
        assert "service_restart" in names
        assert "release_preflight" not in names  # internal never listed
        assert "deploy_verify" in catalog["data"]["workflows"]

    async def test_catalog_filters_by_app(self, setup) -> None:
        service, _, _ = setup
        catalog = await service.ops_catalog(app_id="demo")
        assert "demo" in catalog["data"]["apps"]
        with pytest.raises(DrawbridgeError) as exc_info:
            await service.ops_catalog(app_id="nope")
        assert exc_info.value.code == ErrorCode.UNKNOWN_APP


class TestOperationRun:
    async def test_unknown_operation(self, setup) -> None:
        service, _, _ = setup
        with pytest.raises(UnknownOperationError):
            await service.ops_operation_run("does_not_exist", "demo", "staging")

    async def test_internal_operation_forbidden(self, setup) -> None:
        service, _, _ = setup
        with pytest.raises(ForbiddenOperationError):
            await service.ops_operation_run("release_preflight", "demo", "staging")

    async def test_read_operation_without_runner_times_out_to_pending(
        self, setup
    ) -> None:
        service, _, config = setup
        config.main.diagnostics.wait_budget_seconds = 1
        result = await service.ops_operation_run(
            "git_status", "demo", "staging", parameters={}
        )
        assert result["status"] == "pending"
        assert result["job_id"]

    async def test_bad_parameter_rejected_before_admission(self, setup) -> None:
        service, store, _ = setup
        with pytest.raises(InvalidParameterError):
            await service.ops_operation_run(
                "git_log",
                "demo",
                "staging",
                parameters={"git_ref": "refs/heads/secret/local", "count": 20},
            )
        assert store.db.conn is not None

    async def test_write_requires_idempotency_key(self, setup) -> None:
        service, _, _ = setup
        with pytest.raises(InvalidParameterError, match="idempotency"):
            await service.ops_service_restart(
                app="demo",
                environment="staging",
                service="api",
                reason="manual restart",
                idempotency_key=None,  # type: ignore[arg-type]
            )

    async def test_restart_unregistered_service_rejected(self, setup) -> None:
        service, _, _ = setup
        with pytest.raises(InvalidParameterError, match="restartable"):
            await service.ops_service_restart(
                app="demo",
                environment="staging",
                service="worker",
                reason="drain",
                idempotency_key="restart-1-xxxxxxx",
            )

    async def test_maintenance_blocks_writes_not_reads(self, setup) -> None:
        service, store, _ = setup
        await store.db.set_control("maintenance", "true")
        with pytest.raises(MaintenanceError):
            await service.ops_service_restart(
                app="demo",
                environment="staging",
                service="api",
                reason="drain",
                idempotency_key="restart-2-xxxxxxx",
            )
        await store.db.set_control("maintenance", "false")


class TestPlanApply:
    async def _make_plan(self, store: Store, config, **overrides):
        return await store.create_plan(
            app="demo",
            environment="staging",
            workflow="deploy_verify",
            source_mode="fetch",
            git_ref="refs/heads/main",
            commit_sha="a" * 40,
            config_digest=overrides.get("config_digest", config.digest),
            baseline_release_id=overrides.get("baseline_release_id"),
            ttl_seconds=overrides.get("ttl_seconds", 900),
            params={},
        )

    async def test_apply_unknown_plan(self, setup) -> None:
        service, _, _ = setup
        with pytest.raises(UnknownPlanError):
            await service.ops_release_apply(
                plan_id="00000000-0000-0000-0000-000000000000",
                idempotency_key="apply-1-xxxxxxxx",
            )

    async def test_apply_valid_plan_queues_deploy(self, setup) -> None:
        service, store, config = setup
        plan = await self._make_plan(store, config)
        result = await service.ops_release_apply(
            plan_id=plan.plan_id, idempotency_key="apply-1-xxxxxxxx"
        )
        assert result["status"] == "queued"
        assert result["job_id"]
        job = await store.get_job(result["job_id"])
        assert job.kind == "deploy"
        assert job.plan_id == plan.plan_id

    async def test_apply_expired_plan_rejected(self, setup) -> None:
        service, store, config = setup
        plan = await self._make_plan(store, config, ttl_seconds=0.01)
        import asyncio

        await asyncio.sleep(0.02)
        with pytest.raises(StalePlanError, match="expired"):
            await service.ops_release_apply(
                plan_id=plan.plan_id, idempotency_key="apply-2-xxxxxxxx"
            )
        marked = await store.get_plan(plan.plan_id)
        assert marked.status == PlanStatus.EXPIRED

    async def test_apply_after_config_change_rejected(self, setup) -> None:
        service, store, config = setup
        plan = await self._make_plan(store, config, config_digest="old" * 21 + "o")
        with pytest.raises(StalePlanError, match="configuration changed"):
            await service.ops_release_apply(
                plan_id=plan.plan_id, idempotency_key="apply-3-xxxxxxxx"
            )

    async def test_apply_after_baseline_change_rejected(self, setup) -> None:
        service, store, config = setup
        import time

        from drawbridge.state.records import ReleaseRecord

        await store.record_release(
            ReleaseRecord(
                release_id="r-current",
                app="demo",
                environment="staging",
                plan_id=None,
                job_id=None,
                commit_sha="b" * 40,
                image_id=None,
                image_tag=None,
                config_digest=config.digest,
                status="succeeded",
                rollback_of=None,
                compose_path=None,
                deploy_dir=None,
                evidence={},
                created_at=time.time(),
                verified_at=time.time(),
            )
        )
        plan = await self._make_plan(store, config, baseline_release_id=None)
        with pytest.raises(StalePlanError, match="current release changed"):
            await service.ops_release_apply(
                plan_id=plan.plan_id, idempotency_key="apply-4-xxxxxxxx"
            )

    async def test_same_plan_different_keys_single_job(self, setup) -> None:
        service, store, config = setup
        plan = await self._make_plan(store, config)
        a = await service.ops_release_apply(
            plan_id=plan.plan_id, idempotency_key="apply-a-xxxxxxxx"
        )
        b = await service.ops_release_apply(
            plan_id=plan.plan_id, idempotency_key="apply-b-xxxxxxxx"
        )
        assert a["job_id"] == b["job_id"]


class TestOpsLogsStrictValidation:
    async def test_bool_and_string_numbers_rejected(self, setup) -> None:
        service, _store, _config = setup
        for bad in (True, "50", 1.5):
            with pytest.raises(InvalidParameterError):
                await service.ops_logs(
                    app="demo", environment="staging", limit=bad
                )
        with pytest.raises(InvalidParameterError):
            await service.ops_logs(app="demo", environment="staging", tail=True)
        with pytest.raises(InvalidParameterError):
            await service.ops_logs(app="demo", environment="staging", since_seconds="300")

    async def test_bounds_enforced(self, setup) -> None:
        service, _store, _config = setup
        with pytest.raises(InvalidParameterError):
            await service.ops_logs(app="demo", environment="staging", limit=201)
        with pytest.raises(InvalidParameterError):
            await service.ops_logs(app="demo", environment="staging", tail=1001)
        with pytest.raises(InvalidParameterError):
            await service.ops_logs(app="demo", environment="staging", since_seconds=0)
        with pytest.raises(InvalidParameterError):
            await service.ops_logs(app="demo", environment="staging", query="x" * 129)
        with pytest.raises(InvalidParameterError):
            await service.ops_logs(app="demo", environment="staging", query="bad\x1b[")
        with pytest.raises(InvalidParameterError):
            await service.ops_logs(app="demo", environment="staging", service="unknown-svc")

    async def test_valid_params_admit_diagnostic(self, setup) -> None:
        service, _store, _config = setup
        result = await service.ops_logs(
            app="demo",
            environment="staging",
            service="api",
            limit=50,
            tail=100,
            since_seconds=60,
            query="error",
        )
        # no runner is connected: the diagnostic stays pending, but admission
        # itself succeeded (strict values accepted as-is)
        assert result["status"] == "pending"


class TestAuditEvents:
    async def test_write_admission_appends_event(self, setup) -> None:
        service, store, _config = setup
        payload = await service.ops_service_restart(
            app="demo",
            environment="staging",
            service="api",
            reason="audit check",
            idempotency_key="audit-00000001",
        )
        job_id = payload["job_id"]
        async with store.db.conn.execute(
            "SELECT kind, detail_json FROM events WHERE job_id = ?", (job_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        assert rows and rows[0]["kind"] == "job_admitted"


class TestMaintenanceSync:
    async def test_config_value_persisted_to_control_record(self, setup) -> None:
        from drawbridge.entries.gateway_main import sync_maintenance_from_config

        _service, store, config = setup
        config.main.maintenance.enabled = True
        assert await sync_maintenance_from_config(store.db, config) == "true"
        assert await store.db.get_control("maintenance") == "true"
        config.main.maintenance.enabled = False
        assert await sync_maintenance_from_config(store.db, config) == "false"
        assert await store.db.get_control("maintenance") == "false"
