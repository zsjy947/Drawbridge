"""deploy_verify workflow orchestration and recovery (MVP spec §8).

Sequence: preflight → source_snapshot → image_build → image_import →
image_identify → compose_deploy → health_check → test_suite → finalize.

Runtime isolation: all steps execute through a :class:`DeployRuntime`; the
production runtime shells out only to the registered toolchain via the
ProcessManager, tests inject fakes.  Failure handling follows the frozen
workflow policy:

* before ``runtime_change_started`` → stop, job Failed (no side effects);
* after it → recovery with an independent budget: restore the previous
  successful release (or stop the initial deployment when none exists).
  RolledBack still means the release failed; RollbackFailed / FailedNoBaseline
  block further mutations until an operator reconciles.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from drawbridge.config.compose_template import read_compose_template
from drawbridge.config.models import DrawbridgeConfig, excludes_simulated_releases
from drawbridge.errors import DrawbridgeError, ErrorCode, StalePlanError
from drawbridge.state.records import (
    JobRecord,
    JobStatus,
    PlanStatus,
    ReleaseRecord,
    StagedRelease,
)
from drawbridge.state.store import Store, new_id

StepExecutor = Callable[
    [str, "DeployState", dict[str, Any]], Awaitable[dict[str, Any]]
]

#: Steps that change the running service; the recovery boundary.
RUNTIME_CHANGE_STEP = "deploy"


@dataclass
class DeployState:
    job: JobRecord
    plan: Any  # PlanRecord
    app: str
    environment: str
    started: float
    total_budget: float
    recovery_budget: float
    commit_sha: str | None = None
    image_id: str | None = None
    image_tag: str | None = None
    source_dir: str | None = None
    archive_path: str | None = None
    baseline: ReleaseRecord | None = None
    runtime_change_started: bool = False
    step_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    workflow_steps: list[Any] = field(default_factory=list)

    def remaining(self) -> float:
        return self.total_budget - (time.monotonic() - self.started)

    def suite_name(self) -> str | None:
        """Suite requested by the workflow's test step, if any."""
        for step in self.workflow_steps:
            if step.operation == "test_suite" and step.parameters.get("suite"):
                return str(step.parameters["suite"])
        return None


class DeployWorkflow:
    """Executes the frozen workflow with budget and recovery semantics."""

    def __init__(
        self,
        *,
        config: DrawbridgeConfig,
        store: Store,
        step_executor: StepExecutor,
        workflow_name: str = "deploy_verify",
        step_budgets: dict[str, float] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.step_executor = step_executor
        self.workflow_name = workflow_name
        self.step_budgets = step_budgets or {}
        self.workflow = config.workflows[workflow_name]

    async def run(
        self, job: JobRecord
    ) -> tuple[str, dict[str, Any], dict[str, Any] | None, StagedRelease | None]:
        """Returns (final_status, result, recovery_info, staged_release).

        ``staged_release`` is non-None only on success: the release record
        (and its artifact) are handed to the caller unwritten so the job's
        terminal transition and the release become ONE transaction (D4)."""
        plan = await self.store.get_plan(job.params["plan_id"])
        state = await self._prepare_state(job, plan)

        failure: BaseException | None = None
        completed_steps: list[str] = []
        for step in self.workflow.steps:
            budget = self._step_budget(step.id)
            if state.remaining() <= 0:
                failure = DrawbridgeError(
                    f"deploy budget exhausted before step {step.id!r}",
                    code=ErrorCode.TIMEOUT,
                )
                break
            step_record_id = await self.store.start_step(
                job.job_id, len(completed_steps), step.id
            )
            if step.id == RUNTIME_CHANGE_STEP or step.operation == "compose_deploy":
                # Persisted BEFORE the first runtime mutation: a crash after
                # this point must trigger the recovery path, not a clean stop.
                state.runtime_change_started = True
                await self.store.mark_runtime_change_started(job.job_id)
            try:
                detail = await asyncio_wait_for(
                    self.step_executor(step.operation, state, dict(step.parameters)),
                    timeout=budget,
                )
                await self.store.finish_step(
                    step_record_id, status="succeeded", detail=detail
                )
            except BaseException as exc:
                await self.store.finish_step(
                    step_record_id,
                    status="failed",
                    termination_reason=type(exc).__name__,
                    detail={"error": str(exc)[:300]},
                )
                completed_steps.append(step.id)
                if isinstance(exc, asyncio.CancelledError):
                    # Runner shutdown mid-deploy: never continue into
                    # recovery with a torn-down executor — the operator
                    # reconciles the scene instead (spec §8).
                    raise
                failure = exc
                break
            state.step_results[step.id] = detail
            state.image_id = detail.get("image_id", state.image_id)
            state.image_tag = detail.get("image_tag", state.image_tag)
            state.source_dir = detail.get("source_dir", state.source_dir)
            completed_steps.append(step.id)

        if failure is None:
            finalize_result, staged = await self._finalize(job, state)
            return JobStatus.SUCCEEDED, finalize_result, None, staged

        env_cfg_runtime = self.config.environment(
            state.app, state.environment
        ).runtime
        recovery = None
        if state.runtime_change_started:
            recovery = await self._recover(job, state, failure)
        final_status = self._failure_status(job, recovery)
        result = {
            "error": {
                "code": _error_code(failure),
                "message": str(failure)[:500],
            },
            "steps_completed": completed_steps,
            "current_release": _release_dict(
                await self.store.get_current_release(
                    state.app,
                    state.environment,
                    exclude_simulated=excludes_simulated_releases(env_cfg_runtime),
                )
            ),
        }
        return final_status, result, recovery, None

    async def _prepare_state(self, job: JobRecord, plan: Any) -> DeployState:
        # Re-validate the frozen plan at execution start (spec §7).
        if plan.status != PlanStatus.PLANNED:
            raise StalePlanError(f"plan {plan.plan_id} is {plan.status}")
        if plan.expires_at <= time.time():
            await self.store.mark_plan(plan.plan_id, PlanStatus.EXPIRED)
            raise StalePlanError("plan expired before dispatch")
        if plan.config_digest != self.config.digest:
            raise StalePlanError("configuration changed since planning")
        env_cfg = self.config.environment(plan.app, plan.environment)
        template = read_compose_template(env_cfg.compose_file, list(env_cfg.services))
        if plan.compose_template_digest != template.digest:
            raise StalePlanError("compose template changed since planning")
        baseline = await self.store.get_current_release(
            plan.app,
            plan.environment,
            exclude_simulated=excludes_simulated_releases(env_cfg.runtime),
        )
        baseline_id = baseline.release_id if baseline else None
        if plan.baseline_release_id != baseline_id:
            raise StalePlanError("baseline release changed since planning")
        await self.store.mark_plan(plan.plan_id, PlanStatus.APPLIED)
        state = DeployState(
            job=job,
            plan=plan,
            app=plan.app,
            environment=plan.environment,
            started=time.monotonic(),
            total_budget=float(self.workflow.timeout_seconds),
            recovery_budget=float(self.workflow.recovery_timeout_seconds),
            commit_sha=plan.commit_sha,
            baseline=baseline,
            workflow_steps=list(self.workflow.steps),
        )
        return state

    def _step_budget(self, step_id: str) -> float:
        configured = self.step_budgets.get(step_id)
        remaining = self.workflow.timeout_seconds
        state_budget = configured if configured is not None else remaining
        return max(1.0, min(state_budget, max(1.0, remaining)))

    async def _finalize(
        self, job: JobRecord, state: DeployState
    ) -> tuple[dict[str, Any], StagedRelease]:
        """Build the release record WITHOUT writing it (plan D4).

        Persistence happens in :meth:`Store.complete_job_with_release`
        together with the job's terminal transition; a crash between here
        and that call leaves no release row behind."""
        from drawbridge.state.records import ArtifactRecord

        release_id = new_id()
        now = time.time()
        env_cfg = self.config.environment(state.app, state.environment)
        rendered_compose = state.step_results.get("deploy", {}).get("compose_file")
        release = ReleaseRecord(
            release_id=release_id,
            app=state.app,
            environment=state.environment,
            plan_id=state.plan.plan_id,
            job_id=job.job_id,
            commit_sha=state.commit_sha,
            image_id=state.image_id,
            image_tag=state.image_tag,
            config_digest=self.config.digest,
            simulated=env_cfg.runtime == "simulation",
            status="succeeded",
            rollback_of=None,
            compose_path=str(rendered_compose) if rendered_compose else env_cfg.compose_file,
            deploy_dir=env_cfg.deploy_root,
            evidence={
                "health": state.step_results.get("health", {}),
                "smoke": state.step_results.get("test", {}),
                "verified_at": now,
            },
            created_at=now,
            verified_at=now,
        )
        artifact = (
            ArtifactRecord(
                artifact_id=new_id(),
                app=state.app,
                environment=state.environment,
                kind="image",
                ref=state.image_id,
                release_id=release_id,
                size_bytes=None,
                sha256=None,
                created_at=now,
                retention_class="current",
            )
            if state.image_id
            else None
        )
        staged = StagedRelease(
            release=release,
            artifact=artifact,
            event_detail={
                "status": "succeeded",
                "commit_sha": state.commit_sha,
                "image_id": state.image_id,
            },
        )
        return {
            "release_id": release_id,
            "commit_sha": state.commit_sha,
            "image_id": state.image_id,
            "status": "succeeded",
        }, staged

    async def _recover(
        self, job: JobRecord, state: DeployState, failure: BaseException
    ) -> dict[str, Any]:
        """Independent recovery budget; never re-runs the release itself."""
        recovery_started = time.monotonic()
        recovery: dict[str, Any] = {}
        suite_params = (
            {"suite": state.suite_name()} if state.suite_name() else {}
        )
        try:
            if state.baseline is None:
                await asyncio_wait_for(
                    self.step_executor("stop_initial", state, {}),
                    timeout=max(1.0, min(30.0, state.recovery_budget)),
                )
                recovery = {
                    "status": "stopped_initial",
                    "baseline_release_id": None,
                }
            else:
                state.image_id = state.baseline.image_id
                state.image_tag = state.baseline.image_tag
                state.commit_sha = state.baseline.commit_sha
                await asyncio_wait_for(
                    self.step_executor("restore_previous", state, suite_params),
                    timeout=state.recovery_budget,
                )
                recovery = {
                    "status": "restored_previous",
                    "baseline_release_id": state.baseline.release_id,
                }
            recovery["recovery_seconds"] = round(
                time.monotonic() - recovery_started, 3
            )
        except BaseException as exc:
            recovery = {
                "status": "recovery_failed",
                "baseline_release_id": (
                    state.baseline.release_id if state.baseline else None
                ),
                "error": str(exc)[:300],
                "needs_attention": True,
            }
        await self.store.append_event(
            "deploy_recovery",
            job_id=job.job_id,
            app=state.app,
            environment=state.environment,
            request_id=job.request_id,
            agent_id=job.agent_id,
            detail=recovery,
        )
        return recovery

    def _failure_status(
        self, job: JobRecord, recovery: dict[str, Any] | None
    ) -> str:
        if recovery is None:
            return JobStatus.FAILED
        if recovery.get("status") == "restored_previous":
            return JobStatus.ROLLED_BACK
        if recovery.get("status") == "stopped_initial":
            return JobStatus.FAILED_NO_BASELINE
        return JobStatus.ROLLBACK_FAILED


def _release_dict(release: Any) -> dict[str, Any] | None:
    if release is None:
        return None
    return {
        "release_id": release.release_id,
        "commit_sha": release.commit_sha,
        "image_id": release.image_id,
        "status": release.status,
    }


def _error_code(failure: BaseException) -> str:
    if isinstance(failure, DrawbridgeError):
        return failure.code
    if isinstance(failure, TimeoutError):
        return ErrorCode.TIMEOUT
    return ErrorCode.INTERNAL


async def asyncio_wait_for(awaitable: Any, timeout: float) -> Any:
    return await asyncio.wait_for(awaitable, timeout=timeout)
