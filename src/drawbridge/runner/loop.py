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
from drawbridge.errors import DrawbridgeError, ErrorCode
from drawbridge.executor.process import ProcessManager
from drawbridge.logsetup import get_logger
from drawbridge.state.locking import TargetLocks
from drawbridge.state.records import JobKind, JobStatus
from drawbridge.state.store import Store

log = get_logger(__name__)


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
        self.process_manager = ProcessManager()
        if mutation_handler is None:
            from drawbridge.runner.simulation import build_runtime

            mutation_handler = build_runtime(
                config=self.config,
                store=self.store,
                process_manager=self.process_manager,
                log_dir=self.config.main.paths.log_dir,
            )
        self.mutation_handler = mutation_handler
        self.locks = TargetLocks(lock_dir=Path(config.main.paths.lock_dir))
        self.poll_interval = poll_interval
        self._stopping = asyncio.Event()
        self._diag_semaphore = asyncio.Semaphore(config.main.diagnostics.max_concurrent)
        self._mutation_semaphore = asyncio.Semaphore(config.main.concurrency.max_running_jobs)
        self._running_mutations = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._next_retention_at = time.monotonic() + config.main.retention.cleanup_interval_seconds
        # 0.0 => the first tick reconciles stale running jobs left by a
        # crashed previous Runner instance (MVP spec §8).
        self._next_reconcile_at = 0.0

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

    async def tick_once(self) -> None:
        """One admission-limited queue pass, then settle in-flight jobs.

        Public single-step contract for ``drawbridge-runner --once`` and
        no-systemd command-line operation.
        """
        await self._tick()
        await self.drain()

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

    async def run_until_idle(self, *, timeout: float = 300.0) -> int:
        """Tick until no queued or running jobs remain; returns jobs handled.

        Bounded single-purpose entry for command-line operation without
        systemd (``drawbridge-runner --drain`` and drawbridge-simulate):
        the loop keeps the normal admission/lock/maintenance semantics and
        simply stops once the queue is empty and in-flight tasks settled.
        """
        deadline = time.monotonic() + timeout
        while True:
            await self._tick()
            await self.drain()
            pending = await self._pending_jobs()
            if pending == 0:
                return 0
            if time.monotonic() >= deadline:
                raise DrawbridgeError(
                    f"queue still has {pending} pending job(s) after {timeout:.0f}s",
                    code=ErrorCode.TIMEOUT,
                )
            await asyncio.sleep(self.poll_interval)

    async def _pending_jobs(self) -> int:
        async with self.store.db.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'running')"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row is not None else 0

    async def _tick(self) -> None:
        expired = await self.store.expire_stale_queue()
        if expired:
            log.info("expired queued jobs", count=expired)

        await self._maybe_reconcile_stale()
        await self._maybe_run_retention()

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
        job = await self.store.claim_next_job(owner=self.instance_id, kinds=[JobKind.DIAGNOSTIC])
        if job is not None:
            self._spawn(self._run_diagnostic(job))

    async def _maybe_run_retention(self) -> None:
        """Throttled retention pass (OPERATIONS.md §5).

        Runs before the maintenance gate on purpose: deleting expired
        records is bookkeeping, not a mutation of any deployment target.
        Failures never stop queue consumption — the next tick retries.
        """
        if time.monotonic() < self._next_retention_at:
            return
        self._next_retention_at = (
            time.monotonic() + self.config.main.retention.cleanup_interval_seconds
        )
        try:
            from drawbridge.runner.retention import run_retention

            await run_retention(self.config, self.store)
        except Exception as exc:
            log.error("retention cleanup failed", error=str(exc))

    async def _maybe_reconcile_stale(self) -> None:
        """Flip running jobs with dead heartbeats to needs_attention.

        Runs once at startup and then on the retention cadence.  Never
        re-runs or takes over a job (spec §8); single-instance flock makes
        the heartbeat the reliable liveness signal.  Failures never stop
        queue consumption.
        """
        if time.monotonic() < self._next_reconcile_at:
            return
        self._next_reconcile_at = (
            time.monotonic() + self.config.main.retention.cleanup_interval_seconds
        )
        try:
            reconciled = await self.store.reconcile_stale_running(
                now=time.time(),
                max_age_seconds=float(
                    self.config.main.recovery.stale_running_job_seconds
                ),
            )
            for job_id in reconciled:
                log.warning(
                    "stale running job reconciled to needs_attention", job_id=job_id
                )
        except Exception as exc:
            log.error("stale-job reconcile failed", error=str(exc))

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
        await self.store.append_event(
            "job_requeued",
            job_id=job.job_id,
            app=job.app,
            environment=job.environment,
            request_id=job.request_id,
            agent_id=job.agent_id,
            detail={"reason": "cross-process target lock busy"},
        )

    async def _execute(self, job: Any, *, diagnostic: bool) -> None:
        action = job.action
        handler = self.handlers.get(action)
        if handler is None:
            await self._finish_with_event(
                job,
                JobStatus.FAILED,
                {
                    "error": {
                        "code": "UNKNOWN_OPERATION",
                        "message": f"no handler for {action!r}",
                    }
                },
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
            if ctx.staged_releases:
                # Handler staged a release (release_rollback): atomic success
                # completion (D4) — release + events + terminal state together.
                await self.store.complete_job_with_release(
                    job=job,
                    terminal_status=JobStatus.SUCCEEDED,
                    result=result,
                    recovery=None,
                    owner=self.instance_id,
                    staged=ctx.staged_releases,
                )
            else:
                await self._finish_with_event(job, JobStatus.SUCCEEDED, result)
            log.info(
                "job finished",
                job_id=job.job_id,
                action=action,
                seconds=round(time.monotonic() - started, 3),
            )
        except TimeoutError:
            await self._finish_with_event(
                job,
                JobStatus.FAILED,
                {"error": {"code": "TIMEOUT", "message": "job deadline exceeded"}},
            )
        except DrawbridgeError as exc:
            await self._finish_with_event(job, JobStatus.FAILED, {"error": exc.to_dict()})
        except asyncio.CancelledError:
            await self._finish_with_event(
                job,
                JobStatus.NEEDS_ATTENTION,
                {
                    "error": {
                        "code": "NEEDS_ATTENTION",
                        "message": "runner stopped mid-job; verify the actual scene",
                    }
                },
            )
            raise
        except Exception as exc:
            await self._finish_with_event(
                job,
                JobStatus.FAILED,
                {
                    "error": {
                        "code": "INTERNAL",
                        "message": f"{type(exc).__name__}: {exc}"[:500],
                    }
                },
            )
        finally:
            heartbeat_task.cancel()

    async def _finish_with_event(
        self,
        job: Any,
        status: str,
        result: dict[str, Any] | None,
        recovery: dict[str, Any] | None = None,
    ) -> None:
        """Terminal transition plus the append-only audit event."""
        await self.store.finish_job(
            job.job_id,
            status=status,
            result=result,
            recovery=recovery,
            owner=self.instance_id,
        )
        await self.store.append_event(
            "job_finished",
            job_id=job.job_id,
            app=job.app,
            environment=job.environment,
            request_id=job.request_id,
            agent_id=job.agent_id,
            detail={"action": job.action, "status": status},
        )

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
        """Deploy jobs run the frozen workflow; every exit path terminates
        the job (the stuck-running bug this replaces lost failures)."""
        from drawbridge.runner.deploy import DeployWorkflow

        heartbeat_task = asyncio.create_task(self._heartbeat_loop(job.job_id))
        try:
            try:
                self.workflow_for(job)  # validates the workflow exists
            except DrawbridgeError as exc:
                await self._finish_with_event(job, JobStatus.FAILED, {"error": exc.to_dict()})
                return
            workflow_runner = DeployWorkflow(
                config=self.config,
                store=self.store,
                step_executor=self.mutation_handler,
                workflow_name=job.action,
            )
            try:
                status, result, recovery, staged = await asyncio.wait_for(
                    workflow_runner.run(job), timeout=self._job_timeout(job)
                )
            except asyncio.CancelledError:
                await self._finish_with_event(
                    job,
                    JobStatus.NEEDS_ATTENTION,
                    {
                        "error": {
                            "code": "NEEDS_ATTENTION",
                            "message": "runner stopped mid-deploy; verify the "
                            "actual scene before any further change",
                        }
                    },
                )
                raise
            except TimeoutError:
                await self._finish_with_event(
                    job,
                    JobStatus.FAILED,
                    {"error": {"code": "TIMEOUT", "message": "deploy deadline exceeded"}},
                )
                return
            except DrawbridgeError as exc:
                await self._finish_with_event(job, JobStatus.FAILED, {"error": exc.to_dict()})
                return
            except Exception as exc:
                await self._finish_with_event(
                    job,
                    JobStatus.FAILED,
                    {
                        "error": {
                            "code": "INTERNAL",
                            "message": f"{type(exc).__name__}: {exc}"[:500],
                        }
                    },
                )
                return
            if staged is not None:
                # Atomic success completion (D4): release + artifacts +
                # events + job terminal state in one transaction.
                await self.store.complete_job_with_release(
                    job=job,
                    terminal_status=status,
                    result=result,
                    recovery=recovery,
                    owner=self.instance_id,
                    staged=[staged],
                )
            else:
                await self._finish_with_event(job, status, result, recovery)
        finally:
            heartbeat_task.cancel()

    def workflow_for(self, job: Any) -> Any:
        name = job.action
        if name not in self.config.workflows:
            raise DrawbridgeError(f"unknown workflow {name!r}", code="CONFIG_INVALID")
        return self.config.workflows[name]
