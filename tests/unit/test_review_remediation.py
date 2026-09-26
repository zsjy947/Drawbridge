"""Review remediation regression tests (2026-09-26 full review).

Covers the blocking findings fixed after the plan-D implementation:
dispatch-time blocked-target deferral, stale-finish overwrite prevention,
cancelled diagnostics staying non-blocking, spool-open failure as a
structured start error, non-UTF-8 template rejection and missing MCP
argument mapping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from drawbridge.config.loader import load_config_from_dir
from drawbridge.errors import ConfigInvalidError
from drawbridge.executor.process import ProcessManager
from drawbridge.executor.spec import ExecutionSpec
from drawbridge.gateway.service import GatewayService
from drawbridge.runner.loop import Runner
from drawbridge.state.db import Database
from drawbridge.state.records import JobKind, JobRecord, JobStatus
from drawbridge.state.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"

BASE: dict[str, object] = {
    "kind": JobKind.DEPLOY,
    "action": "deploy_verify",
    "app": "demo",
    "environment": "staging",
    "params": {},
    "config_digest": "d" * 64,
    "queue_timeout_seconds": 600,
    "deadline_seconds": 1800,
    "max_queued": 50,
    "max_queued_per_target": 5,
    "cooldown_seconds": 0,
}


@pytest.fixture()
async def env(tmp_path: Path):
    from tests.conftest import install_compose_template

    config = load_config_from_dir(CONFIG_DIR)
    config.main.paths.state_dir = str(tmp_path / "state")
    config.main.paths.lock_dir = str(tmp_path / "locks")
    config.main.paths.log_dir = str(tmp_path / "logs")
    config.main.concurrency.min_deploy_interval_seconds = 0
    install_compose_template(config, tmp_path)
    database = Database(tmp_path / "state" / "state.db")
    await database.connect()
    await database.initialize()
    store = Store(database)
    service = GatewayService(config, store)
    yield config, store, service
    await database.close()


class TestDispatchTimeBlocking:
    async def test_claim_defers_mutations_on_blocked_target(self, env) -> None:
        """R-MAJOR-1: a queued deploy must not dispatch after its target
        flipped to needs_attention (admission only checks at enqueue)."""
        _config, store, _ = env
        first = await store.admit_job(idempotency_key="blk-a-1", **BASE)  # type: ignore[arg-type]
        second = await store.admit_job(idempotency_key="blk-a-2", **BASE)  # type: ignore[arg-type]
        assert second.status == JobStatus.QUEUED  # queued before the block
        await store.claim_next_job(owner="r1", kinds=[JobKind.DEPLOY])
        await store.finish_job(
            first.job_id, status=JobStatus.NEEDS_ATTENTION, owner="r1"
        )
        # The still-queued second deploy must NOT be claimed on the blocked
        # target; diagnostics still flow.
        assert (
            await store.claim_next_job(owner="r2", kinds=[JobKind.DEPLOY]) is None
        )
        diag = await store.admit_job(
            **{**BASE, "kind": JobKind.DIAGNOSTIC, "idempotency_key": "blk-diag-1"}  # type: ignore[arg-type]
        )
        claimed = await store.claim_next_job(
            owner="r2", kinds=[JobKind.DIAGNOSTIC]
        )
        assert claimed is not None and claimed.job_id == diag.job_id

    async def test_stale_finish_cannot_overwrite_reconcile(self, env) -> None:
        """S-MAJOR-1: after the reconciler flips a job to needs_attention,
        a stale runner's finish (same owner) is refused — no phantom
        transition and no job_finished event."""
        _config, store, _ = env
        job = await store.admit_job(idempotency_key="ovr-a-1", **BASE)  # type: ignore[arg-type]
        await store.claim_next_job(owner="r1", kinds=[JobKind.DEPLOY])
        await store.reconcile_stale_running(now=1e12, max_age_seconds=0.0)
        assert (await store.get_job(job.job_id)).status == JobStatus.NEEDS_ATTENTION
        # stale runner tries to finish as succeeded
        assert (
            await store.finish_job(
                job.job_id, status=JobStatus.SUCCEEDED, owner="r1"
            )
            is False
        )
        after = await store.get_job(job.job_id)
        assert after.status == JobStatus.NEEDS_ATTENTION
        async with store.db.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind = 'job_finished'"
            " AND job_id = ?", (job.job_id,)
        ) as cursor:
            row = await cursor.fetchone()
        assert row["n"] == 0

    async def test_cancelled_diagnostic_is_failed_not_blocking(self, env) -> None:
        """R-MAJOR-3: a diagnostic cancelled by runner shutdown lands FAILED
        and never blocks the target's mutations."""
        import asyncio

        config, store, _ = env

        async def cancelled_handler(ctx: Any, job: JobRecord) -> dict[str, Any]:
            raise asyncio.CancelledError

        runner = Runner(
            config,
            store,
            handler_registry={"compose_logs": cancelled_handler},
            diagnostic_actions=frozenset({"compose_logs"}),
        )
        diag = await store.admit_job(
            **{**BASE, "kind": JobKind.DIAGNOSTIC, "action": "compose_logs", "idempotency_key": "cxl-diag-1"}  # type: ignore[arg-type]
        )
        await store.claim_next_job(owner=runner.instance_id, kinds=[JobKind.DIAGNOSTIC])
        # simulate the dispatch path _execute with cancellation
        job = await store.get_job(diag.job_id)
        with pytest.raises(asyncio.CancelledError):
            await runner._execute(job, diagnostic=True)
        after = await store.get_job(diag.job_id)
        assert after.status == JobStatus.FAILED
        # mutations still admissible on the target
        nxt = await store.admit_job(idempotency_key="cxl-after-1", **BASE)  # type: ignore[arg-type]
        assert nxt.status == JobStatus.QUEUED


class TestExecutorSpoolOpenFailure:
    async def test_unwritable_spool_is_structured_start_error(
        self, tmp_path: Path
    ) -> None:
        """C-MAJOR-1: opening the spool log happens BEFORE the spawn — a
        failure returns a start_error result and never leaks a child."""
        import sys

        pm = ProcessManager()
        spec = ExecutionSpec(
            operation="spool_open_fail",
            executable=sys.executable,
            argv=("-c", "import time; time.sleep(30)"),
            cwd=str(tmp_path),
            env={"PATH": ""},
            profile=__import__(
                "drawbridge.config.models", fromlist=["ExecutionProfile"]
            ).ExecutionProfile.RUNTIME_MANAGE,
            timeout_seconds=10,
            output_policy=__import__(
                "drawbridge.config.models", fromlist=["OutputPolicyKind"]
            ).OutputPolicyKind.SPOOL,
            max_output_bytes=1024,
            hard_output_limit=1024,
            accepted_exit_codes=frozenset({0}),
            log_path=str(tmp_path / "no-such-dir" / "sub" / "out.log"),
        )
        # the parent directory chain cannot be created (a FILE blocks it)
        blocker = tmp_path / "no-such-dir"
        blocker.write_text("i am a file", encoding="utf-8")
        result = await pm.execute(spec)
        assert result.termination_reason == "start_error"
        assert "spool open failed" in (result.start_error or "")
        assert result.accepted is False


class TestTemplateDecode:
    def test_non_utf8_template_rejected_as_config_invalid(self, tmp_path: Path) -> None:
        """C-MINOR-2: a GBK-saved template fails closed as CONFIG_INVALID on
        the client-reachable plan path, not as a native error."""
        from drawbridge.config.compose_template import read_compose_template

        template = tmp_path / "gbk.yaml"
        template.write_bytes(
            "services:\n  api:\n    image: REPLACE_BY_DRAWBRIDGE\n".encode("gbk")
            + b"\xff\xfe binary tail"
        )
        with pytest.raises(ConfigInvalidError, match="UTF-8"):
            read_compose_template(template, ["api"])


class TestMcpMissingArgument:
    async def test_missing_required_argument_is_tool_error(self) -> None:
        """S-MINOR-4: KeyError from a missing required argument maps to an
        INVALID_PARAMETER tool error, not a protocol internal error."""
        from drawbridge.gateway.mcp_app import MCPAppFactory

        class _Params:
            name = "ops_status"
            arguments = {}  # app/environment missing

        factory = MCPAppFactory(_MissingArgsService())
        result = await factory._call_tool(None, _Params())  # type: ignore[arg-type]
        assert result.is_error is True
        assert result.structured_content["code"] == "INVALID_PARAMETER"


class _MissingArgsService:
    """Minimal stub: dispatch reaches args['app'] and raises KeyError."""

    async def ops_status(self, app: str, environment: str) -> dict[str, Any]:
        raise AssertionError("should not be reached")
