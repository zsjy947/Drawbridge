"""GatewayService: validation, admission and orchestration of MCP tools.

The Gateway never executes anything itself (MVP spec §1): reads become
diagnostic jobs executed by the Runner under a bounded concurrency channel
(the Gateway waits within its HTTP budget, otherwise returns the job_id for
polling), writes become queued mutation jobs behind idempotency keys, plan
binding and capacity checks.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from drawbridge.config.compose_template import read_compose_template
from drawbridge.config.models import (
    Access,
    DrawbridgeConfig,
    ExecutionProfile,
    OperationConfig,
)
from drawbridge.errors import (
    BusyError,
    DrawbridgeError,
    ErrorCode,
    ForbiddenOperationError,
    InvalidParameterError,
    MaintenanceError,
    StalePlanError,
    UnknownAppError,
    UnknownEnvironmentError,
    UnknownJobError,
    UnknownOperationError,
    UnknownReleaseError,
)
from drawbridge.policy.params import (
    ValidationContext,
    validate_idempotency_key,
    validate_parameters,
    validate_reason,
    validate_tracing_id,
)
from drawbridge.state.records import JobKind, JobStatus, PlanStatus
from drawbridge.state.store import Store

#: Maps public write operations to their job kind.
_WRITE_KINDS: dict[str, str] = {
    "service_restart": JobKind.RESTART,
}


def _log_param_specs(*, include_service: bool, include_cursor: bool) -> dict[str, Any]:
    """ops_logs envelope validation — the same rules as the compose_logs
    operation (MVP spec §2/§4); strict types, no coercion.

    ``service`` and ``cursor`` are optional: their specs are only part of
    the validated set when the client actually provided a value (specs
    without a default are treated as required by the validation engine).
    """
    from drawbridge.config.models import ParameterSpec

    specs: dict[str, Any] = {
        "query": ParameterSpec.model_validate(
            {"type": "string", "default": "", "max_length": 128}
        ),
        "limit": ParameterSpec.model_validate(
            {"type": "integer", "default": 100, "minimum": 1, "maximum": 200}
        ),
        "tail": ParameterSpec.model_validate(
            {"type": "integer", "default": 200, "minimum": 1, "maximum": 1000}
        ),
        "since_seconds": ParameterSpec.model_validate(
            {"type": "integer", "default": 300, "minimum": 1, "maximum": 86400}
        ),
    }
    if include_service:
        specs["service"] = ParameterSpec.model_validate(
            {
                "type": "string",
                "max_length": 64,
                "pattern": r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}",
                "validators": ["registered_service"],
            }
        )
    if include_cursor:
        specs["cursor"] = ParameterSpec.model_validate(
            {"type": "string", "max_length": 512, "validators": ["opaque_cursor"]}
        )
    return specs


class GatewayService:
    def __init__(
        self,
        config: DrawbridgeConfig,
        store: Store,
        *,
        instance_id: str = "gateway",
    ) -> None:
        self.config = config
        self.store = store
        self.instance_id = instance_id

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _request_id(self) -> str:
        return str(uuid.uuid4())

    def _app_cfg(self, app_id: str) -> Any:
        try:
            return self.config.apps[app_id]
        except KeyError:
            raise UnknownAppError(f"unknown app {app_id!r}") from None

    def _env_cfg(self, app_id: str, environment: str) -> Any:
        app = self._app_cfg(app_id)
        try:
            return app.environments[environment]
        except KeyError:
            raise UnknownEnvironmentError(
                f"unknown environment {environment!r} for app {app_id!r}"
            ) from None

    def _validation_context(self, app_id: str, environment: str) -> ValidationContext:
        app = self._app_cfg(app_id)
        env = self._env_cfg(app_id, environment)
        diagnostics = env.diagnostics
        return ValidationContext(
            app_id=app_id,
            environment=environment,
            services=frozenset(env.services),
            restartable_services=frozenset(env.restartable_services),
            file_aliases=frozenset(diagnostics.config_files) if diagnostics else frozenset(),
            validator_names=frozenset(diagnostics.validators) if diagnostics else frozenset(),
            allowed_ref_patterns=tuple(app.git.allowed_ref_patterns),
        )

    async def _maintenance(self) -> bool:
        return (await self.store.db.get_control("maintenance")) == "true"

    async def _gate_write(self) -> None:
        if await self._maintenance():
            raise MaintenanceError("Drawbridge is in maintenance mode")

    def _wait_budget(self) -> float:
        return float(self.config.main.diagnostics.wait_budget_seconds)

    async def _diagnostic_channel_full(self) -> bool:
        """Admission-side cap for the diagnostic channel (plan D7).

        ``concurrency.max_read_requests`` bounds the number of admitted-but-
        not-yet-terminal diagnostic jobs.  Counted from the store (queued +
        running), so terminal transitions recycle quota automatically —
        including jobs whose HTTP wait already timed out — and the cap holds
        across gateway restarts.  Advisory throttle: a slight overshoot
        under racing admissions is harmless.
        """
        async with self.store.db.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE kind = ? AND status IN (?, ?)",
            (JobKind.DIAGNOSTIC, JobStatus.QUEUED, JobStatus.RUNNING),
        ) as cursor:
            row = await cursor.fetchone()
        limit = self.config.main.concurrency.max_read_requests
        return row is not None and int(row["n"]) >= limit

    async def _run_diagnostic(
        self,
        *,
        action: str,
        app: str,
        environment: str,
        params: dict[str, Any],
        operation_timeout: float,
        request_id: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """Admit a diagnostic job and wait within the HTTP budget."""
        if await self._diagnostic_channel_full():
            raise BusyError(
                "diagnostic channel is saturated; retry shortly",
                retry_after_seconds=max(1, int(self._wait_budget() // 2)),
            )
        job = await self.store.admit_job(
            kind=JobKind.DIAGNOSTIC,
            action=action,
            app=app,
            environment=environment,
            params=params,
            idempotency_key=None,
            config_digest=self.config.digest,
            queue_timeout_seconds=max(60.0, operation_timeout + 30),
            deadline_seconds=operation_timeout + 60,
            max_queued=self.config.main.concurrency.max_queued_jobs,
            max_queued_per_target=self.config.main.concurrency.max_queued_jobs_per_target,
            request_id=request_id,
        )
        deadline = time.monotonic() + min(self._wait_budget(), operation_timeout + 5)
        while time.monotonic() < deadline:
            current = await self.store.get_job(job.job_id)
            if current.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
                if current.status == JobStatus.SUCCEEDED:
                    return current.result or {}, ""
                detail = (current.result or {}).get("error", {})
                raise DrawbridgeError(
                    str(detail.get("message", f"diagnostic job {current.status}")),
                    code=str(detail.get("code", ErrorCode.INTERNAL)),
                )
            await asyncio.sleep(0.2)
        return None, job.job_id

    async def _admit_write(
        self,
        *,
        kind: str,
        action: str,
        app: str,
        environment: str,
        params: dict[str, Any],
        idempotency_key: str,
        request_id: str,
        deadline_seconds: float = 1800,
        plan_id: str | None = None,
        agent_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> dict[str, Any]:
        await self._gate_write()
        job = await self.store.admit_job(
            kind=kind,
            action=action,
            app=app,
            environment=environment,
            params=params,
            idempotency_key=idempotency_key,
            config_digest=self.config.digest,
            queue_timeout_seconds=self.config.main.concurrency.queue_timeout_seconds,
            deadline_seconds=deadline_seconds,
            max_queued=self.config.main.concurrency.max_queued_jobs,
            max_queued_per_target=self.config.main.concurrency.max_queued_jobs_per_target,
            cooldown_seconds=self.config.main.concurrency.min_deploy_interval_seconds,
            plan_id=plan_id,
            request_id=request_id,
            agent_id=agent_id,
            parent_task_id=parent_task_id,
        )
        await self.store.append_event(
            "job_admitted",
            job_id=job.job_id,
            app=app,
            environment=environment,
            request_id=request_id,
            agent_id=agent_id,
            detail={
                "kind": kind,
                "action": action,
                "plan_id": plan_id,
                "job_status": job.status,
            },
        )
        return {
            "request_id": request_id,
            "status": "queued",
            "job_id": job.job_id,
            "data": job.to_public_dict(),
        }

    # ------------------------------------------------------------------
    # ops_catalog
    # ------------------------------------------------------------------

    async def ops_catalog(self, app_id: str | None = None) -> dict[str, Any]:
        request_id = self._request_id()
        operations = []
        for name, op in sorted(self.config.operations.items()):
            if not op.public:
                continue
            if app_id is not None:
                self._app_cfg(app_id)
            operations.append(
                {
                    "name": name,
                    "handler": op.handler,
                    "access": op.access.value,
                    "execution_profile": op.execution_profile.value,
                    "timeout_seconds": op.timeout_seconds,
                    "parameters": _param_schemas(op),
                }
            )
        apps = {}
        for app_name, app_cfg in sorted(self.config.apps.items()):
            if app_id is not None and app_name != app_id:
                continue
            apps[app_name] = {
                "environments": sorted(app_cfg.environments),
                "services": {
                    env: env_cfg.services
                    for env, env_cfg in app_cfg.environments.items()
                },
                "runtimes": {
                    env: env_cfg.runtime
                    for env, env_cfg in app_cfg.environments.items()
                },
            }
        return {
            "request_id": request_id,
            "status": "ok",
            "data": {
                "operations": operations,
                "workflows": sorted(self.config.workflows),
                "apps": apps,
            },
        }

    # ------------------------------------------------------------------
    # ops_operation_run
    # ------------------------------------------------------------------

    async def ops_operation_run(
        self,
        operation: str,
        app: str,
        environment: str,
        parameters: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        agent_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._request_id()
        op_cfg = self.config.operations.get(operation)
        if op_cfg is None:
            raise UnknownOperationError(f"unknown operation {operation!r}")
        if not op_cfg.public:
            raise ForbiddenOperationError(
                f"operation {operation!r} is internal and cannot be invoked directly"
            )
        if app is not None:
            self._app_cfg(app)
        if environment and app:
            self._env_cfg(app, environment)

        provided = dict(parameters or {})
        is_restart = (
            op_cfg.handler == "service_restart_and_verify"
            or operation == "service_restart"
        )
        if is_restart and "reason" in provided:
            provided["reason"] = validate_reason(provided["reason"])
        resolved = validate_parameters(
            op_cfg.parameters, provided, self._validation_context(app, environment)
        )

        if op_cfg.access == Access.READ:
            result, job_id = await self._run_diagnostic(
                action=operation,
                app=app,
                environment=environment,
                params=dict(resolved),
                operation_timeout=op_cfg.timeout_seconds,
                request_id=request_id,
            )
            if result is None:
                return {
                    "request_id": request_id,
                    "status": "pending",
                    "job_id": job_id,
                    "data": {"message": "diagnostic still running; query ops_release_status"},
                }
            return {"request_id": request_id, "status": "ok", "data": result}

        # write path
        if idempotency_key is None:
            raise InvalidParameterError(
                "idempotency_key is required for every non-read operation"
            )
        validate_idempotency_key(idempotency_key)
        if operation not in _WRITE_KINDS:
            # A registered public write operation MUST have an explicit job
            # kind; silently treating a new one as a restart would misroute
            # it to the wrong handler.
            raise DrawbridgeError(
                f"operation {operation!r} has no registered job kind; "
                "extend the gateway mapping when registering new write "
                "operations",
                code=ErrorCode.CONFIG_INVALID,
            )
        kind = _WRITE_KINDS[operation]
        if agent_id:
            validate_tracing_id(agent_id, field="agent_id")
        if parent_task_id:
            validate_tracing_id(parent_task_id, field="parent_task_id")
        payload = await self._admit_write(
            kind=kind,
            action=operation,
            app=app,
            environment=environment,
            params=dict(resolved),
            idempotency_key=idempotency_key,
            request_id=request_id,
            deadline_seconds=float(op_cfg.timeout_seconds) + 60,
            agent_id=agent_id,
            parent_task_id=parent_task_id,
        )
        return payload

    # ------------------------------------------------------------------
    # ops_status / ops_logs
    # ------------------------------------------------------------------

    async def ops_status(self, app: str, environment: str) -> dict[str, Any]:
        request_id = self._request_id()
        self._env_cfg(app, environment)
        current = await self.store.get_current_release(app, environment)
        running = await self.store.find_active_job(app, environment)
        result, job_id = await self._run_diagnostic(
            action="host_metrics",
            app=app,
            environment=environment,
            params={},
            operation_timeout=10,
            request_id=request_id,
        )
        if result is None:
            return {
                "request_id": request_id,
                "status": "pending",
                "job_id": job_id,
                "data": {
                    "current_release": _release_summary(current),
                    "active_job": running,
                },
            }
        return {
            "request_id": request_id,
            "status": "ok",
            "data": {
                "host": result,
                "current_release": _release_summary(current),
                "active_job": running,
                "observed_at": time.time(),
            },
        }

    async def ops_logs(
        self,
        app: str,
        environment: str,
        service: str | None = None,
        query: str = "",
        cursor: str | None = None,
        limit: Any = 100,
        tail: Any = 200,
        since_seconds: Any = 300,
    ) -> dict[str, Any]:
        request_id = self._request_id()
        self._env_cfg(app, environment)
        provided: dict[str, Any] = {"query": query, "limit": limit, "tail": tail,
                                    "since_seconds": since_seconds}
        if service is not None:
            provided["service"] = service
        if cursor is not None:
            provided["cursor"] = cursor
        # Strict validation at the edge: bools, numeric strings and
        # out-of-range values are rejected here, never coerced.
        resolved = validate_parameters(
            _log_param_specs(
                include_service=service is not None, include_cursor=cursor is not None
            ),
            provided,
            self._validation_context(app, environment),
        )
        params: dict[str, Any] = dict(resolved)
        result, job_id = await self._run_diagnostic(
            action="compose_logs",
            app=app,
            environment=environment,
            params=params,
            operation_timeout=15,
            request_id=request_id,
        )
        if result is None:
            return {"request_id": request_id, "status": "pending", "job_id": job_id}
        return {"request_id": request_id, "status": "ok", "data": result}

    # ------------------------------------------------------------------
    # Plan / apply / status / rollback / test / restart
    # ------------------------------------------------------------------

    async def ops_release_plan(
        self,
        app: str,
        environment: str,
        source_mode: str,
        git_ref: str,
        workflow: str = "deploy_verify",
        agent_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._request_id()
        self._env_cfg(app, environment)
        if workflow not in self.config.workflows:
            raise InvalidParameterError(f"unknown workflow {workflow!r}")
        if source_mode not in ("fetch", "local"):
            raise InvalidParameterError("source_mode must be fetch or local")
        ctx = self._validation_context(app, environment)
        # shape + app-allowlist validation happens here, at the edge
        ref_spec = {
            "type": "string",
            "min_length": 1,
            "max_length": 200,
            "pattern": (
                "refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*"
                "|refs/tags/[A-Za-z0-9][A-Za-z0-9._/-]*|[0-9a-f]{40}"
            ),
            "validators": ["git_ref_or_commit", "allowed_app_ref"],
        }
        from drawbridge.config.models import ParameterSpec

        validate_parameters(
            {"git_ref": ParameterSpec.model_validate(ref_spec)},
            {"git_ref": git_ref},
            ctx,
        )
        if agent_id:
            validate_tracing_id(agent_id, field="agent_id")
        if parent_task_id:
            validate_tracing_id(parent_task_id, field="parent_task_id")

        plan_params = {
            "git_ref": git_ref,
            "source_mode": source_mode,
            "workflow": workflow,
        }
        result, job_id = await self._run_diagnostic(
            action="release_plan",
            app=app,
            environment=environment,
            params=plan_params,
            operation_timeout=150,
            request_id=request_id,
        )
        if result is None:
            return {
                "request_id": request_id,
                "status": "pending",
                "job_id": job_id,
                "data": {"message": "plan still resolving; query ops_release_status"},
            }
        # The Runner-side plan record is the single reporting authority for
        # the baseline (plan D11): this gateway-side read raced deploys that
        # finished between plan dispatch and the report, producing a
        # baseline that disagreed with the frozen plan the apply validates.
        return {"request_id": request_id, "status": "ok", "data": result}

    async def ops_release_apply(
        self,
        plan_id: str,
        idempotency_key: str,
        agent_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._request_id()
        validate_idempotency_key(idempotency_key)
        plan = await self.store.get_plan(plan_id)
        if plan.status != PlanStatus.PLANNED:
            raise StalePlanError(f"plan {plan_id} is {plan.status}, not planned")
        if plan.expires_at <= time.time():
            await self.store.mark_plan(plan_id, PlanStatus.EXPIRED)
            raise StalePlanError("plan has expired; create a new plan")
        if plan.config_digest != self.config.digest:
            raise StalePlanError(
                "configuration changed since the plan was created; re-plan"
            )
        env_cfg = self._env_cfg(plan.app, plan.environment)
        template = read_compose_template(env_cfg.compose_file, list(env_cfg.services))
        if plan.compose_template_digest != template.digest:
            raise StalePlanError(
                "compose template changed since the plan was created; re-plan"
            )
        baseline = await self.store.get_current_release(plan.app, plan.environment)
        baseline_id = baseline.release_id if baseline else None
        if plan.baseline_release_id != baseline_id:
            raise StalePlanError(
                "current release changed since the plan was created; re-plan"
            )
        workflow = self.config.workflows.get(plan.workflow)
        if workflow is None:
            raise DrawbridgeError(
                f"workflow {plan.workflow!r} is no longer registered",
                code=ErrorCode.CONFIG_INVALID,
            )
        return await self._admit_write(
            kind=JobKind.DEPLOY,
            action=plan.workflow,
            app=plan.app,
            environment=plan.environment,
            params={"plan_id": plan.plan_id},
            idempotency_key=idempotency_key,
            request_id=request_id,
            deadline_seconds=float(workflow.timeout_seconds),
            plan_id=plan.plan_id,
            agent_id=agent_id,
            parent_task_id=parent_task_id,
        )

    async def ops_release_status(
        self,
        job_id: str | None = None,
        release_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._request_id()
        if job_id:
            job = await self.store.get_job(job_id)
            steps = await self.store.list_steps(job_id)
            return {
                "request_id": request_id,
                "status": "ok",
                "data": {
                    "job": job.to_public_dict(),
                    "result": job.result,
                    "recovery": job.recovery,
                    "steps": [
                        {
                            "seq": s.seq,
                            "name": s.name,
                            "status": s.status,
                            "exit_code": s.exit_code,
                            "termination_reason": s.termination_reason,
                        }
                        for s in steps
                    ],
                },
            }
        if release_id:
            release = await self.store.get_release(release_id)
            return {
                "request_id": request_id,
                "status": "ok",
                "data": {"release": _release_summary(release)},
            }
        raise UnknownJobError("provide job_id or release_id")

    async def ops_history(
        self,
        app: str,
        environment: str,
        what: str = "releases",
        limit: Any = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Bounded target history: releases / jobs / audit events.

        Direct bounded SQLite reads — the same class as ops_release_status
        (no side effects, no host commands); the Runner diagnostic channel
        stays reserved for operations that execute something.
        """
        request_id = self._request_id()
        self._env_cfg(app, environment)
        if what not in ("releases", "jobs", "events"):
            raise InvalidParameterError(
                "what must be one of releases, jobs, events"
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or not (
            1 <= limit <= 50
        ):
            raise InvalidParameterError("limit must be an integer between 1 and 50")
        page_size = limit + 1  # fetch one extra row to detect the next page

        if what == "releases":
            before_created: float | None = None
            if cursor is not None:
                before_created = _parse_history_cursor(cursor)
            releases = await self.store.list_releases(
                app, environment, limit=page_size, before_created_at=before_created
            )
            has_more = len(releases) > limit
            releases = releases[:limit]
            current = await self.store.get_current_release(app, environment)
            current_id = current.release_id if current else None
            rows = []
            for release in releases:
                rows.append(
                    {
                        "release_id": release.release_id,
                        "commit_sha": release.commit_sha,
                        "image_id": release.image_id,
                        "status": release.status,
                        "created_at": release.created_at,
                        "rollback_of": release.rollback_of,
                        "is_current": release.release_id == current_id,
                        "rollback_eligible": (
                            release.status in ("succeeded", "rollback", "superseded")
                            and release.release_id != current_id
                        ),
                    }
                )
            next_cursor = str(releases[-1].created_at) if has_more and releases else None
            return {
                "request_id": request_id,
                "status": "ok",
                "data": {"what": "releases", "releases": rows, "next_cursor": next_cursor},
            }

        if what == "jobs":
            before: float | None = None
            if cursor is not None:
                before = _parse_history_cursor(cursor)
            jobs = await self.store.list_jobs(
                app, environment, limit=page_size, before_queued_at=before
            )
            has_more = len(jobs) > limit
            jobs = jobs[:limit]
            rows = [job.to_public_dict() for job in jobs]
            next_cursor = (
                str(jobs[-1].queued_at) if has_more and jobs else None
            )
            return {
                "request_id": request_id,
                "status": "ok",
                "data": {"what": "jobs", "jobs": rows, "next_cursor": next_cursor},
            }

        before_ts: float | None = None
        if cursor is not None:
            before_ts = _parse_history_cursor(cursor)
        events = await self.store.list_events(
            app, environment, limit=page_size, before_ts=before_ts
        )
        has_more = len(events) > limit
        events = events[:limit]
        next_cursor = str(events[-1]["ts"]) if has_more and events else None
        return {
            "request_id": request_id,
            "status": "ok",
            "data": {"what": "events", "events": events, "next_cursor": next_cursor},
        }

    async def ops_service_restart(
        self,
        app: str,
        environment: str,
        service: str,
        reason: str,
        idempotency_key: str,
        agent_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._request_id()
        env_cfg = self._env_cfg(app, environment)
        validate_reason(reason)
        validate_idempotency_key(idempotency_key)
        if service not in env_cfg.restartable_services:
            raise InvalidParameterError(
                f"service {service!r} is not restartable for app {app!r}"
            )
        return await self._admit_write(
            kind=JobKind.RESTART,
            action="service_restart",
            app=app,
            environment=environment,
            params={"service": service, "reason": reason},
            idempotency_key=idempotency_key,
            request_id=request_id,
            deadline_seconds=180,
            agent_id=agent_id,
            parent_task_id=parent_task_id,
        )

    async def ops_release_rollback(
        self,
        app: str,
        environment: str,
        release_id: str,
        reason: str,
        idempotency_key: str,
        agent_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._request_id()
        self._env_cfg(app, environment)
        validate_reason(reason)
        validate_idempotency_key(idempotency_key)
        release = await self.store.get_release(release_id)
        if release.app != app or release.environment != environment:
            raise UnknownReleaseError("release belongs to a different target")
        if release.status not in ("succeeded", "rollback", "superseded"):
            raise UnknownReleaseError(
                f"release {release_id} cannot be rolled back to (status {release.status})"
            )
        current = await self.store.get_current_release(app, environment)
        if current is not None and current.release_id == release_id:
            raise InvalidParameterError("release is already the current one")
        return await self._admit_write(
            kind=JobKind.ROLLBACK,
            action="release_rollback",
            app=app,
            environment=environment,
            params={"release_id": release_id, "reason": reason},
            idempotency_key=idempotency_key,
            request_id=request_id,
            deadline_seconds=300,
            agent_id=agent_id,
            parent_task_id=parent_task_id,
        )

    async def ops_test(
        self,
        release_id: str,
        suite: str,
        idempotency_key: str,
        agent_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._request_id()
        validate_idempotency_key(idempotency_key)
        release = await self.store.get_release(release_id)
        env_cfg = self._env_cfg(release.app, release.environment)
        if suite not in env_cfg.test_suites:
            raise InvalidParameterError(f"suite {suite!r} is not registered")
        current = await self.store.get_current_release(release.app, release.environment)
        if current is None or current.release_id != release_id:
            raise StalePlanError("tests must target the currently deployed release")
        return await self._admit_write(
            kind=JobKind.TEST,
            action="test_suite",
            app=release.app,
            environment=release.environment,
            params={"release_id": release_id, "suite": suite},
            idempotency_key=idempotency_key,
            request_id=request_id,
            deadline_seconds=300,
            agent_id=agent_id,
            parent_task_id=parent_task_id,
        )


def _release_summary(release: Any) -> dict[str, Any] | None:
    if release is None:
        return None
    return {
        "release_id": release.release_id,
        "commit_sha": release.commit_sha,
        "image_id": release.image_id,
        "status": release.status,
        "created_at": release.created_at,
        "rollback_of": release.rollback_of,
    }


def _parse_history_cursor(cursor: str) -> float:
    """History cursors are opaque timestamps of the last row seen."""
    try:
        value = float(cursor)
    except ValueError:
        raise InvalidParameterError("cursor is not a valid history cursor") from None
    if not 0 <= value <= 4102444800:  # up to 2100-01-01
        raise InvalidParameterError("cursor is not a valid history cursor")
    return value


def _param_schemas(op: OperationConfig) -> dict[str, Any]:
    schemas: dict[str, Any] = {}
    for name, spec in op.parameters.items():
        schema: dict[str, Any] = {"type": spec.type}
        if spec.pattern:
            schema["pattern"] = spec.pattern
        if spec.min_length is not None:
            schema["min_length"] = spec.min_length
        if spec.max_length is not None:
            schema["max_length"] = spec.max_length
        if spec.minimum is not None:
            schema["minimum"] = spec.minimum
        if spec.maximum is not None:
            schema["maximum"] = spec.maximum
        if spec.default is not None:
            schema["default"] = spec.default
        if spec.validators:
            schema["validators"] = list(spec.validators)
        schemas[name] = schema
    return schemas


__all__ = [
    "ExecutionProfile",
    "GatewayService",
]
