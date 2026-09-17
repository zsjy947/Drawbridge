"""Runner: the single execution principal (tech design §3, §10).

Claim loops over the shared SQLite queue:

* diagnostics run concurrently under a bounded semaphore and never occupy
  mutation slots;
* mutation jobs (deploy / test / restart / rollback) run serialized per
  global limit and per-target asyncio/file locks;
* maintenance mode is re-read before EVERY dispatch; an unreadable control
  record stops dispatch entirely (fail closed);
* heartbeats are diagnostic only — a crashed Runner is detected by the
  operator, never replaced silently.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from drawbridge.config.models import DrawbridgeConfig
from drawbridge.errors import DrawbridgeError
from drawbridge.executor.process import ProcessManager
from drawbridge.logsetup import get_logger
from drawbridge.state.locking import TargetLocks
from drawbridge.state.records import JobKind, JobStatus
from drawbridge.state.store import Store

log = get_logger(__name__)

_TERMINAL_BY_KIND: dict[str, str] = {}


class Runner:
    def __init__(
        self,
        config: DrawbridgeConfig,
        store: Store,
        *,
        instance_id: str | None = None,
        handler_registry: dict[str, Any] | None = None,
        diagnostic_actions: frozenset[str] | None = None,
        mutation_handler: Any = None,
        poll_interval: float = 0.2,
    ) -> None:
        self.config = config
        self.store = store
        self.instance_id = instance_id or f"runner-{os.getpid()}"
        from drawbridge.runner.handlers import DIAGNOSTIC_ACTIONS, HANDLERS

        self.handlers = handler_registry if handler_registry is not None else HANDLERS
        self.diagnostic_actions = (
            diagnostic_actions if diagnostic_actions is not None else DIAGNOSTIC_ACTIONS
        )
        self.mutation_handler = mutation_handler
        self.process_manager = ProcessManager()
        self.locks = TargetLocks(lock_dir=Path(config.main.paths.lock_dir))
        self.poll_interval = poll_interval
        self._stopping = asyncio.Event()
        self._diag_semaphore = asyncio.Semaphore(config.main.diagnostics.max_concurrent)
        self._mutation_semaphore = asyncio.Semaphore(config.main.concurrency.max_running_jobs)
        self._running_mutations = 0
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        log.info("runner started", instance=self.instance_id)
        while not self._stopping.is_set():
            try:
                await self._tick()
            except Exception as exc:
                log.error("runner tick failed", error=str(exc))
                await asyncio.sleep(1.0)
            await asyncio.sleep(self.poll_interval)

    async def stop(self) -> None:
        self._stopping.set()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def drain(self) -> None:
        """Wait for all in-flight job tasks (tests and graceful shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _tick(self) -> None:
        expired = await self.store.expire_stale_queue()
        if expired:
            log.info("expired queued jobs", count=expired)

        maintenance = await self.store.db.get_control("maintenance")
        if maintenance is None:
            # Control record unreadable: fail closed, dispatch nothing.
            return
        maintenance_active = maintenance == "true"

        if (
            not maintenance_active
            and self._running_mutations < self.config.main.concurrency.max_running_jobs
        ):
            job = await self.store.claim_next_job(
                owner=self.instance_id,
                kinds=[
                    JobKind.DEPLOY,
                    JobKind.TEST,
                    JobKind.RESTART,
                    JobKind.ROLLBACK,
                ],
                skip_targets=await self._cooldown_targets(),
            )
            if job is not None:
                self._running_mutations += 1
                self._spawn(self._run_mutation(job))
        job = await self.store.claim_next_job(
            owner=self.instance_id, kinds=[JobKind.DIAGNOSTIC]
        )
        if job is not None:
            self._spawn(self._run_diagnostic(job))

    async def _cooldown_targets(self) -> dict[str, float]:
        """Targets whose deploy cooldown has not yet elapsed."""
        cooldown = self.config.main.concurrency.min_deploy_interval_seconds
        if cooldown <= 0:
            return {}
        now = time.time()
        async with self.store.db.conn.execute(
            "SELECT app, environment, MAX(finished_at) AS last_finished FROM jobs"
            " WHERE kind IN ('deploy', 'rollback') AND finished_at IS NOT NULL"
            " GROUP BY app, environment",
        ) as cursor:
            rows = await cursor.fetchall()
        return {
            f"{r['app']}/{r['environment']}": r["last_finished"] + cooldown
            for r in rows
            if now < r["last_finished"] + cooldown
        }

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_diagnostic(self, job: Any) -> None:
        async with self._diag_semaphore:
            # Diagnostics are read entries: they still run during maintenance.
            await self._execute(job, diagnostic=True)

    async def _run_mutation(self, job: Any) -> None:
        try:
            target = f"{job.app}/{job.environment}"
            if not self.locks.acquire_file_lock(job.app, job.environment):
                log.info("target locked cross-process; deferring", target=target)
                await self._requeue(job)
                return
            try:
                async with self.locks.async_lock(job.app, job.environment):
                    if job.kind == JobKind.DEPLOY:
                        await self._execute_deploy(job)
                    else:
                        await self._execute(job, diagnostic=False)
            finally:
                self.locks.release_file_lock(job.app, job.environment)
        finally:
            self._running_mutations -= 1

    async def _requeue(self, job: Any) -> None:
        async with self.store.db.write_lock():
            await self.store.db.conn.execute(
                "UPDATE jobs SET status = 'queued', owner = NULL, started_at = NULL"
                " WHERE job_id = ? AND status = 'running'",
                (job.job_id,),
            )
            await self.store.db.conn.commit()

    async def _execute(self, job: Any, *, diagnostic: bool) -> None:
        action = job.action
        handler = self.handlers.get(action)
        if handler is None:
            await self.store.finish_job(
                job.job_id,
                status=JobStatus.FAILED,
                result={
                    "error": {
                        "code": "UNKNOWN_OPERATION",
                        "message": f"no handler for {action!r}",
                    }
                },
                owner=self.instance_id,
            )
            return
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(job.job_id))
        started = time.monotonic()
        try:
            from drawbridge.runner.handlers import JobContext

            ctx = JobContext(
                config=self.config,
                store=self.store,
                process_manager=self.process_manager,
                log_dir=self.config.main.paths.log_dir,
            )
            result = await asyncio.wait_for(handler(ctx, job), timeout=self._job_timeout(job))
            await self.store.finish_job(
                job.job_id,
                status=JobStatus.SUCCEEDED,
                result=result,
                owner=self.instance_id,
            )
            log.info(
                "job finished",
                job_id=job.job_id,
                action=action,
                seconds=round(time.monotonic() - started, 3),
            )
        except TimeoutError:
            await self.store.finish_job(
                job.job_id,
                status=JobStatus.FAILED,
                result={"error": {"code": "TIMEOUT", "message": "job deadline exceeded"}},
                owner=self.instance_id,
            )
        except DrawbridgeError as exc:
            await self.store.finish_job(
                job.job_id,
                status=JobStatus.FAILED,
                result={"error": exc.to_dict()},
                owner=self.instance_id,
            )
        except asyncio.CancelledError:
            await self.store.finish_job(
                job.job_id,
                status=JobStatus.NEEDS_ATTENTION,
                result={
                    "error": {
                        "code": "NEEDS_ATTENTION",
                        "message": "runner stopped mid-job; verify the actual scene",
                    }
                },
                owner=self.instance_id,
            )
            raise
        except Exception as exc:
            await self.store.finish_job(
                job.job_id,
                status=JobStatus.FAILED,
                result={
                    "error": {"code": "INTERNAL", "message": f"{type(exc).__name__}: {exc}"[:500]}
                },
                owner=self.instance_id,
            )
        finally:
            heartbeat_task.cancel()

    def _job_timeout(self, job: Any) -> float:
        if job.deadline_at is None:
            return 1800.0
        remaining = job.deadline_at - time.time()
        return float(max(5.0, remaining))

    async def _heartbeat_loop(self, job_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(5.0)
                await self.store.heartbeat(job_id, self.instance_id)
        except asyncio.CancelledError:
            raise

    async def _execute_deploy(self, job: Any) -> None:
        from drawbridge.runner.deploy import DeployWorkflow

        self.workflow_for(job)  # validates the workflow exists
        executor = self.mutation_handler
        if executor is None:
            raise DrawbridgeError(
                "deploy runtime is not available on this host; run the Runner on "
                "the target server (self-check enforces this)",
                code="UNSUPPORTED",
            )
        workflow_runner = DeployWorkflow(
            config=self.config,
            store=self.store,
            step_executor=executor,
            workflow_name=job.action,
        )
        status, result, recovery = await workflow_runner.run(job)
        await self.store.finish_job(
            job.job_id,
            status=status,
            result=result,
            recovery=recovery,
            owner=self.instance_id,
        )

    def workflow_for(self, job: Any) -> Any:
        name = job.action
        if name not in self.config.workflows:
            raise DrawbridgeError(f"unknown workflow {name!r}", code="CONFIG_INVALID")
        return self.config.workflows[name]
