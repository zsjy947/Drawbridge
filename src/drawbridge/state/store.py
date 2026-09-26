"""State store: admission, claiming, releases, events.

Concurrency invariants (tech design §7 / MVP spec §7-§8):

* admission (idempotency dedup → plan-job uniqueness → capacity → cooldown
  → insert) happens inside ONE short ``BEGIN IMMEDIATE`` transaction, so
  concurrent entries can never exceed the queue or create two jobs;
* ``UNIQUE(plan_id)`` makes "one deploy job per plan" a database property;
* ``UNIQUE(idempotency_key)`` keeps every entry point deduplicated;
* claiming flips ``queued → running`` conditionally in a short transaction
  and records the owner; heartbeats are diagnostic, never a lease;
* cooldown is checked both at admission and again at dispatch.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from typing import Any

from drawbridge.errors import (
    BusyError,
    DrawbridgeError,
    ErrorCode,
    IdempotencyConflictError,
    RateLimitedError,
    UnknownJobError,
    UnknownPlanError,
)
from drawbridge.state.db import Database
from drawbridge.state.records import (
    ArtifactRecord,
    JobKind,
    JobRecord,
    JobStatus,
    PlanRecord,
    ReleaseRecord,
    StagedRelease,
    StepRecord,
    dumps,
    loads,
    row_to_job,
    row_to_plan,
    row_to_release,
)

TERMINAL_DEPLOY_KINDS = (JobKind.DEPLOY, JobKind.ROLLBACK)

#: Job statuses that block every further mutation on their target until an
#: operator reconciles the scene (tech design §7 / MVP spec §8).
BLOCKING_STATUSES = (JobStatus.ROLLBACK_FAILED, JobStatus.NEEDS_ATTENTION)


def new_id() -> str:
    return str(uuid.uuid4())


def request_digest(action: str, params: dict[str, Any]) -> str:
    """Stable digest binding an idempotency key to normalized request content.

    Tracing fields (agent_id / parent_task_id / request_id) must not be part
    of the digest (MVP spec §7) — callers pass only action + normalized
    parameters.
    """
    import hashlib

    payload = dumps({"action": action, "params": params})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------

    async def create_plan(
        self,
        *,
        app: str,
        environment: str,
        workflow: str,
        source_mode: str,
        git_ref: str,
        commit_sha: str | None,
        config_digest: str,
        baseline_release_id: str | None,
        ttl_seconds: float,
        params: dict[str, Any],
        request_id: str | None = None,
        compose_template_digest: str | None = None,
    ) -> PlanRecord:
        now = time.time()
        record = PlanRecord(
            plan_id=new_id(),
            app=app,
            environment=environment,
            workflow=workflow,
            source_mode=source_mode,
            git_ref=git_ref,
            commit_sha=commit_sha,
            config_digest=config_digest,
            baseline_release_id=baseline_release_id,
            status="planned",
            created_at=now,
            expires_at=now + ttl_seconds,
            params=params,
            request_id=request_id,
            compose_template_digest=compose_template_digest,
            plan_schema_version=2,
        )
        async with self.db.write_lock():
            await self.db.conn.execute(
                "INSERT INTO plans(plan_id, app, environment, workflow, source_mode,"
                " git_ref, commit_sha, config_digest, compose_template_digest,"
                " plan_schema_version, baseline_release_id, status,"
                " created_at, expires_at, params_json, request_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.plan_id,
                    record.app,
                    record.environment,
                    record.workflow,
                    record.source_mode,
                    record.git_ref,
                    record.commit_sha,
                    record.config_digest,
                    record.compose_template_digest,
                    record.plan_schema_version,
                    record.baseline_release_id,
                    record.status,
                    record.created_at,
                    record.expires_at,
                    dumps(record.params),
                    record.request_id,
                ),
            )
            await self.db.conn.commit()
        return record

    async def get_plan(self, plan_id: str) -> PlanRecord:
        async with self.db.conn.execute(
            "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise UnknownPlanError(f"plan {plan_id} does not exist")
        return row_to_plan(row)

    async def mark_plan(self, plan_id: str, status: str) -> None:
        async with self.db.write_lock():
            await self.db.conn.execute(
                "UPDATE plans SET status = ? WHERE plan_id = ?", (status, plan_id)
            )
            await self.db.conn.commit()

    # ------------------------------------------------------------------
    # Job admission — one short BEGIN IMMEDIATE transaction
    # ------------------------------------------------------------------

    async def admit_job(
        self,
        *,
        kind: str,
        action: str,
        app: str,
        environment: str,
        params: dict[str, Any],
        idempotency_key: str | None,
        config_digest: str,
        queue_timeout_seconds: float,
        deadline_seconds: float,
        max_queued: int,
        max_queued_per_target: int,
        cooldown_seconds: float = 0.0,
        plan_id: str | None = None,
        request_id: str | None = None,
        agent_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> JobRecord:
        now = time.time()
        digest = request_digest(action, params)
        conn = self.db.conn
        async with self.db.write_lock():
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._find_duplicate(
                    conn, idempotency_key, digest, plan_id, now
                )
                if existing is not None:
                    # Dedup is read-only except for one case: binding a NEW
                    # idempotency key to an existing plan job (MVP spec §7).
                    # Commit (not rollback) so that binding survives.
                    await conn.commit()
                    return existing

                if kind != JobKind.DIAGNOSTIC:
                    await self._check_target_not_blocked(conn, app, environment)
                    await self._check_capacity(
                        conn, app, environment, max_queued, max_queued_per_target
                    )
                    if kind in TERMINAL_DEPLOY_KINDS and cooldown_seconds > 0:
                        await self._check_cooldown(
                            conn, app, environment, cooldown_seconds, now
                        )

                job_id = new_id()
                cursor = await conn.execute(
                    "INSERT INTO jobs(job_id, kind, action, app, environment, plan_id,"
                    " status, params_json, config_digest, queue_expires_at,"
                    " deadline_at, queued_at, request_id, agent_id, parent_task_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        kind,
                        action,
                        app,
                        environment,
                        plan_id,
                        JobStatus.QUEUED,
                        dumps(params),
                        config_digest,
                        now + queue_timeout_seconds,
                        now + queue_timeout_seconds + deadline_seconds,
                        now,
                        request_id,
                        agent_id,
                        parent_task_id,
                    ),
                )
                if cursor.rowcount != 1:
                    # Core admission invariant — never depend on `python -O`
                    # stripping an assert away (plan D11).
                    raise DrawbridgeError(
                        "job insert did not produce exactly one row",
                        code=ErrorCode.INTERNAL,
                    )
                if idempotency_key is not None:
                    await conn.execute(
                        "INSERT INTO idempotency_keys(key, action, app, environment,"
                        " request_digest, job_id, created_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            idempotency_key,
                            action,
                            app,
                            environment,
                            digest,
                            job_id,
                            now,
                        ),
                    )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return await self.get_job(job_id)

    async def _find_duplicate(
        self,
        conn: Any,
        idempotency_key: str | None,
        digest: str,
        plan_id: str | None,
        now: float,
    ) -> JobRecord | None:
        if idempotency_key is not None:
            async with conn.execute(
                "SELECT request_digest, job_id FROM idempotency_keys WHERE key = ?",
                (idempotency_key,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                if row["request_digest"] != digest:
                    raise IdempotencyConflictError(
                        "idempotency key was already used with different request content"
                    )
                return await self._job_by_id_in_conn(conn, row["job_id"])
        if plan_id is not None:
            async with conn.execute(
                "SELECT job_id FROM jobs WHERE plan_id = ?", (plan_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                job = await self._job_by_id_in_conn(conn, row["job_id"])
                # A new key may bind to the existing plan job, but only when
                # the request content is identical (MVP spec §7).
                if idempotency_key is not None:
                    if request_digest(job.action, job.params) != digest:
                        raise IdempotencyConflictError(
                            "this plan already has a job created from different "
                            "request content"
                        )
                    await conn.execute(
                        "INSERT OR IGNORE INTO idempotency_keys(key, action, app,"
                        " environment, request_digest, job_id, created_at)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (
                            idempotency_key,
                            job.action,
                            job.app,
                            job.environment,
                            digest,
                            job.job_id,
                            now,
                        ),
                    )
                return job
        return None

    async def _check_target_not_blocked(
        self, conn: Any, app: str, environment: str
    ) -> None:
        placeholders = ",".join("?" * len(BLOCKING_STATUSES))
        query = (
            "SELECT job_id FROM jobs WHERE app = ? AND environment = ?"  # noqa: S608
            " AND status IN (" + placeholders + ") LIMIT 1"
        )
        async with conn.execute(query, (app, environment, *BLOCKING_STATUSES)) as cursor:
            row = await cursor.fetchone()
        if row is not None:
            raise DrawbridgeError(
                "target is blocked by an unresolved "
                "rollback_failed/needs_attention job; reconcile the scene first",
                code=ErrorCode.NEEDS_ATTENTION,
            )

    async def _check_capacity(
        self,
        conn: Any,
        app: str,
        environment: str,
        max_queued: int,
        max_queued_per_target: int,
    ) -> None:
        # Diagnostic jobs never consume mutation capacity (plan D7): they
        # are throttled separately by concurrency.max_read_requests at the
        # gateway, and their backlog must not starve change admission.
        async with conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = ? AND kind != ?",
            (JobStatus.QUEUED, JobKind.DIAGNOSTIC),
        ) as cursor:
            row = await cursor.fetchone()
        if row["n"] >= max_queued:
            raise BusyError("mutation queue is full", retry_after_seconds=5)
        async with conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = ? AND app = ?"
            " AND environment = ? AND kind != ?",
            (JobStatus.QUEUED, app, environment, JobKind.DIAGNOSTIC),
        ) as cursor:
            row = await cursor.fetchone()
        if row["n"] >= max_queued_per_target:
            raise BusyError(
                f"queue is full for target {app}/{environment}",
                retry_after_seconds=5,
            )

    async def _check_cooldown(
        self,
        conn: Any,
        app: str,
        environment: str,
        cooldown_seconds: float,
        now: float,
    ) -> None:
        placeholders = ",".join("?" * len(TERMINAL_DEPLOY_KINDS))
        query = (
            "SELECT MAX(finished_at) AS last_finished FROM jobs WHERE app = ?"  # noqa: S608
            " AND environment = ? AND kind IN (" + placeholders + ")"
            " AND finished_at IS NOT NULL"
        )  # placeholders expand to "?" marks only — never user input
        async with conn.execute(
            query,
            (app, environment, *TERMINAL_DEPLOY_KINDS),
        ) as cursor:
            row = await cursor.fetchone()
        last = row["last_finished"]
        if last is not None and now - last < cooldown_seconds:
            retry_after = max(1, int(cooldown_seconds - (now - last)) + 1)
            raise RateLimitedError(
                "deploy cooldown has not elapsed",
                retry_after_seconds=retry_after,
            )

    async def _job_by_id_in_conn(self, conn: Any, job_id: str) -> JobRecord:
        async with conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise DrawbridgeError(
                f"job {job_id} vanished mid-transaction",
                code=ErrorCode.INTERNAL,
            )
        return row_to_job(row)

    # ------------------------------------------------------------------
    # Job claiming / progress
    # ------------------------------------------------------------------

    async def claim_next_job(
        self,
        *,
        owner: str,
        kinds: Sequence[str],
        now: float | None = None,
        skip_targets: dict[str, float] | None = None,
    ) -> JobRecord | None:
        """Claim the oldest dispatchable queued job (FIFO per target).

        ``skip_targets`` maps ``app/environment`` to a wall-clock time before
        which the target's deploy cooldown has not elapsed — such jobs stay
        queued without occupying a run slot (spec §7).
        """
        now = time.time() if now is None else now
        conn = self.db.conn
        placeholders = ",".join("?" * len(kinds))
        skip_targets = skip_targets or {}
        async with self.db.write_lock():
            await conn.execute("BEGIN IMMEDIATE")
            try:
                query = (
                    "SELECT * FROM jobs WHERE status = ? AND kind IN ("  # noqa: S608
                    + placeholders + ") ORDER BY queued_at"
                )
                async with conn.execute(
                    query,
                    (JobStatus.QUEUED, *kinds),
                ) as cursor:
                    rows = await cursor.fetchall()
                claimed: JobRecord | None = None
                for row in rows:
                    job = row_to_job(row)
                    if job.queue_expires_at is not None and job.queue_expires_at <= now:
                        await conn.execute(
                            "UPDATE jobs SET status = ?, finished_at = ?"
                            " WHERE job_id = ? AND status = ?",
                            (JobStatus.QUEUE_EXPIRED, now, job.job_id, JobStatus.QUEUED),
                        )
                        # Audit the expiry here too (invariant 5): jobs that
                        # time out inside the claim pass must not disappear
                        # from the event chain (plan D11).
                        await conn.execute(
                            "INSERT INTO events(ts, kind, request_id, job_id, app,"
                            " environment, agent_id, detail_json)"
                            " VALUES(?,?,?,?,?,?,?,?)",
                            (
                                now,
                                "job_queue_expired",
                                job.request_id,
                                job.job_id,
                                job.app,
                                job.environment,
                                job.agent_id,
                                dumps(
                                    {
                                        "kind": job.kind,
                                        "queue_expires_at": job.queue_expires_at,
                                    }
                                ),
                            ),
                        )
                        continue
                    target = f"{job.app}/{job.environment}"
                    if job.kind in TERMINAL_DEPLOY_KINDS:
                        until = skip_targets.get(target)
                        if until is not None and now < until:
                            continue
                    await conn.execute(
                        "UPDATE jobs SET status = ?, owner = ?, started_at = ?,"
                        " heartbeat_at = ?, deadline_at = ?"
                        " WHERE job_id = ? AND status = ?",
                        (
                            JobStatus.RUNNING,
                            owner,
                            now,
                            now,
                            job.deadline_at,
                            job.job_id,
                            JobStatus.QUEUED,
                        ),
                    )
                    update_cursor = await conn.execute(
                        "SELECT status, owner FROM jobs WHERE job_id = ?", (job.job_id,)
                    )
                    updated = await update_cursor.fetchone()
                    if updated is None or updated["status"] != JobStatus.RUNNING:
                        # pragma: no cover - guarded by the IMMEDIATE txn
                        continue
                    claimed = await self._job_by_id_in_conn(conn, job.job_id)
                    break
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return claimed

    async def get_job(self, job_id: str) -> JobRecord:
        async with self.db.conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise UnknownJobError(f"job {job_id} does not exist")
        return row_to_job(row)

    async def find_job_by_plan(self, plan_id: str) -> JobRecord | None:
        async with self.db.conn.execute(
            "SELECT * FROM jobs WHERE plan_id = ?", (plan_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row_to_job(row) if row is not None else None

    async def find_active_job(
        self, app: str, environment: str
    ) -> dict[str, Any] | None:
        async with self.db.conn.execute(
            "SELECT * FROM jobs WHERE app = ? AND environment = ?"
            " AND status IN ('queued', 'running') ORDER BY queued_at LIMIT 1",
            (app, environment),
        ) as cursor:
            row = await cursor.fetchone()
        return row_to_job(row).to_public_dict() if row is not None else None

    async def list_jobs(
        self,
        app: str,
        environment: str,
        *,
        limit: int = 50,
        before_queued_at: float | None = None,
    ) -> list[JobRecord]:
        """Newest-first bounded job history for one target."""
        query = (
            "SELECT * FROM jobs WHERE app = ? AND environment = ?"
            " AND (? IS NULL OR queued_at < ?)"
            " ORDER BY queued_at DESC LIMIT ?"
        )
        async with self.db.conn.execute(
            query, (app, environment, before_queued_at, before_queued_at, limit)
        ) as cursor:
            rows = await cursor.fetchall()
        return [row_to_job(r) for r in rows]

    async def list_events(
        self,
        app: str,
        environment: str,
        *,
        limit: int = 50,
        before_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        """Newest-first bounded audit events for one target (read-only)."""
        query = (
            "SELECT id, ts, kind, request_id, job_id, release_id, agent_id,"
            " detail_json FROM events WHERE app = ? AND environment = ?"
            " AND (? IS NULL OR ts < ?)"
            " ORDER BY ts DESC, id DESC LIMIT ?"
        )
        async with self.db.conn.execute(
            query, (app, environment, before_ts, before_ts, limit)
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "kind": r["kind"],
                "request_id": r["request_id"],
                "job_id": r["job_id"],
                "release_id": r["release_id"],
                "agent_id": r["agent_id"],
                "detail": loads(r["detail_json"]) or {},
            }
            for r in rows
        ]

    async def heartbeat(self, job_id: str, owner: str, *, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        async with self.db.write_lock():
            await self.db.conn.execute(
                "UPDATE jobs SET heartbeat_at = ? WHERE job_id = ? AND owner = ?",
                (now, job_id, owner),
            )
            await self.db.conn.commit()

    async def mark_runtime_change_started(self, job_id: str) -> None:
        async with self.db.write_lock():
            await self.db.conn.execute(
                "UPDATE jobs SET runtime_change_started = 1 WHERE job_id = ?",
                (job_id,),
            )
            await self.db.conn.commit()

    async def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        recovery: dict[str, Any] | None = None,
        owner: str | None = None,
    ) -> None:
        if status not in JobStatus.TERMINAL:
            raise DrawbridgeError(
                f"cannot finish job into non-terminal status {status!r}",
                code=ErrorCode.INTERNAL,
            )
        now = time.time()
        async with self.db.write_lock():
            if owner is not None:
                await self.db.conn.execute(
                    "UPDATE jobs SET status = ?, result_json = ?, recovery_json = ?,"
                    " finished_at = ? WHERE job_id = ? AND owner = ?",
                    (status, dumps(result) if result else None,
                     dumps(recovery) if recovery else None, now, job_id, owner),
                )
            else:
                await self.db.conn.execute(
                    "UPDATE jobs SET status = ?, result_json = ?, recovery_json = ?,"
                    " finished_at = ? WHERE job_id = ?",
                    (status, dumps(result) if result else None,
                     dumps(recovery) if recovery else None, now, job_id),
                )
            await self.db.conn.commit()

    async def complete_job_with_release(
        self,
        *,
        job: JobRecord,
        terminal_status: str,
        result: dict[str, Any] | None,
        recovery: dict[str, Any] | None,
        owner: str | None,
        staged: Sequence[StagedRelease],
    ) -> None:
        """Atomic success completion (plan D4): releases, artifacts,
        ``release_recorded`` events, the job's terminal transition and the
        ``job_finished`` event in ONE ``BEGIN IMMEDIATE`` transaction.

        Replaces the previous ≥3-transaction success path whose crash window
        ("release recorded but job stuck running") needed manual
        reconciliation.  ``reconcile_stale_running`` still covers the window
        between the first runtime change and this transaction — there, no
        release has been written yet and its semantics are unchanged.  Event
        payloads and ordering match the pre-D4 writes exactly.
        """
        if terminal_status not in JobStatus.TERMINAL:
            raise DrawbridgeError(
                f"cannot finish job into non-terminal status {terminal_status!r}",
                code=ErrorCode.INTERNAL,
            )
        now = time.time()
        conn = self.db.conn
        async with self.db.write_lock():
            await conn.execute("BEGIN IMMEDIATE")
            try:
                for item in staged:
                    release = item.release
                    await conn.execute(
                        "UPDATE releases SET status = ? WHERE app = ?"
                        " AND environment = ? AND status IN (?, ?)",
                        ("superseded", release.app, release.environment,
                         "succeeded", "rollback"),
                    )
                    await conn.execute(
                        "INSERT INTO releases(release_id, app, environment,"
                        " plan_id, job_id, commit_sha, image_id, image_tag,"
                        " config_digest, simulated, status, rollback_of,"
                        " compose_path, deploy_dir, evidence_json, created_at,"
                        " verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            release.release_id,
                            release.app,
                            release.environment,
                            release.plan_id,
                            release.job_id,
                            release.commit_sha,
                            release.image_id,
                            release.image_tag,
                            release.config_digest,
                            1 if release.simulated else 0,
                            release.status,
                            release.rollback_of,
                            release.compose_path,
                            release.deploy_dir,
                            dumps(release.evidence),
                            release.created_at,
                            release.verified_at,
                        ),
                    )
                    if item.artifact is not None:
                        artifact = item.artifact
                        await conn.execute(
                            "INSERT INTO artifacts(artifact_id, app, environment,"
                            " kind, ref, release_id, size_bytes, sha256, created_at,"
                            " retention_class) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (
                                artifact.artifact_id,
                                artifact.app,
                                artifact.environment,
                                artifact.kind,
                                artifact.ref,
                                artifact.release_id,
                                artifact.size_bytes,
                                artifact.sha256,
                                artifact.created_at,
                                artifact.retention_class,
                            ),
                        )
                    await conn.execute(
                        "INSERT INTO events(ts, kind, request_id, job_id,"
                        " release_id, app, environment, agent_id, detail_json)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            now,
                            "release_recorded",
                            job.request_id,
                            job.job_id,
                            release.release_id,
                            release.app,
                            release.environment,
                            job.agent_id,
                            dumps(item.event_detail),
                        ),
                    )
                if owner is not None:
                    cursor = await conn.execute(
                        "UPDATE jobs SET status = ?, result_json = ?,"
                        " recovery_json = ?, finished_at = ? WHERE job_id = ?"
                        " AND owner = ?",
                        (
                            terminal_status,
                            dumps(result) if result else None,
                            dumps(recovery) if recovery else None,
                            now,
                            job.job_id,
                            owner,
                        ),
                    )
                else:
                    cursor = await conn.execute(
                        "UPDATE jobs SET status = ?, result_json = ?,"
                        " recovery_json = ?, finished_at = ? WHERE job_id = ?",
                        (
                            terminal_status,
                            dumps(result) if result else None,
                            dumps(recovery) if recovery else None,
                            now,
                            job.job_id,
                        ),
                    )
                if cursor.rowcount != 1:
                    raise DrawbridgeError(
                        "job disappeared before atomic completion",
                        code=ErrorCode.INTERNAL,
                    )
                await conn.execute(
                    "INSERT INTO events(ts, kind, request_id, job_id, release_id,"
                    " app, environment, agent_id, detail_json)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        now,
                        "job_finished",
                        job.request_id,
                        job.job_id,
                        None,
                        job.app,
                        job.environment,
                        job.agent_id,
                        dumps({"action": job.action, "status": terminal_status}),
                    ),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def expire_stale_queue(self) -> int:
        """Queue timeouts run on their own clock; returns expired count.

        Every flip to ``queue_expired`` appends a ``job_queue_expired``
        audit event inside the same transaction (invariant 5: terminal
        transitions must never vanish from the audit chain — plan D11)."""
        now = time.time()
        conn = self.db.conn
        expired = 0
        async with self.db.write_lock():
            await conn.execute("BEGIN IMMEDIATE")
            try:
                async with conn.execute(
                    "SELECT job_id, kind, app, environment, request_id, agent_id,"
                    " queue_expires_at FROM jobs WHERE status = ?"
                    " AND queue_expires_at IS NOT NULL AND queue_expires_at <= ?",
                    (JobStatus.QUEUED, now),
                ) as cursor:
                    rows = await cursor.fetchall()
                for row in rows:
                    await conn.execute(
                        "UPDATE jobs SET status = ?, finished_at = ?"
                        " WHERE job_id = ? AND status = ?",
                        (JobStatus.QUEUE_EXPIRED, now, row["job_id"], JobStatus.QUEUED),
                    )
                    await conn.execute(
                        "INSERT INTO events(ts, kind, request_id, job_id, app,"
                        " environment, agent_id, detail_json)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (
                            now,
                            "job_queue_expired",
                            row["request_id"],
                            row["job_id"],
                            row["app"],
                            row["environment"],
                            row["agent_id"],
                            dumps(
                                {
                                    "kind": row["kind"],
                                    "queue_expires_at": row["queue_expires_at"],
                                }
                            ),
                        ),
                    )
                    expired += 1
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return expired

    async def reconcile_stale_running(
        self, *, now: float, max_age_seconds: float
    ) -> list[str]:
        """Flip running jobs with dead heartbeats to needs_attention.

        MVP spec §8: an unknown scene is never re-run or silently taken
        over — the operator reconciles it.  ``heartbeat_at`` is the
        discriminator; jobs that never heartbeated fall back to
        ``started_at``.  Returns the reconciled job ids and appends one
        ``job_reconciled`` audit event per job.
        """
        cutoff = now - max_age_seconds
        async with self.db.conn.execute(
            "SELECT job_id, owner FROM jobs WHERE status = ?"
            " AND ((heartbeat_at IS NOT NULL AND heartbeat_at < ?)"
            " OR (heartbeat_at IS NULL AND started_at IS NOT NULL AND started_at < ?))",
            (JobStatus.RUNNING, cutoff, cutoff),
        ) as cursor:
            rows = await cursor.fetchall()
        reconciled: list[str] = []
        for row in rows:
            job_id = row["job_id"]
            await self.finish_job(
                job_id,
                status=JobStatus.NEEDS_ATTENTION,
                result={
                    "error": {
                        "code": "NEEDS_ATTENTION",
                        "message": (
                            "runner heartbeat timed out while the job was "
                            "running; verify the actual scene before any "
                            "further change"
                        ),
                    }
                },
            )
            await self.append_event(
                "job_reconciled",
                job_id=job_id,
                detail={
                    "reason": "stale_heartbeat",
                    "previous_status": "running",
                    "owner": row["owner"],
                },
            )
            reconciled.append(job_id)
        return reconciled

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    async def start_step(self, job_id: str, seq: int, name: str) -> str:
        step_id = new_id()
        async with self.db.write_lock():
            await self.db.conn.execute(
                "INSERT INTO steps(step_id, job_id, seq, name, status, started_at)"
                " VALUES(?,?,?,?,?,?)",
                (step_id, job_id, seq, name, "running", time.time()),
            )
            await self.db.conn.commit()
        return step_id

    async def finish_step(
        self,
        step_id: str,
        *,
        status: str,
        exit_code: int | None = None,
        termination_reason: str | None = None,
        log_ref: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        async with self.db.write_lock():
            await self.db.conn.execute(
                "UPDATE steps SET status = ?, finished_at = ?, exit_code = ?,"
                " termination_reason = ?, log_ref = ?, detail_json = ?"
                " WHERE step_id = ?",
                (
                    status,
                    time.time(),
                    exit_code,
                    termination_reason,
                    log_ref,
                    dumps(detail) if detail else None,
                    step_id,
                ),
            )
            await self.db.conn.commit()

    async def list_steps(self, job_id: str) -> list[StepRecord]:
        async with self.db.conn.execute(
            "SELECT * FROM steps WHERE job_id = ? ORDER BY seq", (job_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            StepRecord(
                step_id=r["step_id"],
                job_id=r["job_id"],
                seq=r["seq"],
                name=r["name"],
                status=r["status"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
                exit_code=r["exit_code"],
                termination_reason=r["termination_reason"],
                log_ref=r["log_ref"],
                detail=loads(r["detail_json"]) or {},
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Releases and artifacts
    # ------------------------------------------------------------------

    async def record_release(self, record: ReleaseRecord) -> None:
        async with self.db.write_lock():
            await self.db.conn.execute(
                "UPDATE releases SET status = ? WHERE app = ? AND environment = ?"
                " AND status IN (?, ?)",
                ("superseded", record.app, record.environment, "succeeded", "rollback"),
            )
            await self.db.conn.execute(
                "INSERT INTO releases(release_id, app, environment, plan_id, job_id,"
                " commit_sha, image_id, image_tag, config_digest, simulated, status,"
                " rollback_of, compose_path, deploy_dir, evidence_json, created_at,"
                " verified_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.release_id,
                    record.app,
                    record.environment,
                    record.plan_id,
                    record.job_id,
                    record.commit_sha,
                    record.image_id,
                    record.image_tag,
                    record.config_digest,
                    1 if record.simulated else 0,
                    record.status,
                    record.rollback_of,
                    record.compose_path,
                    record.deploy_dir,
                    dumps(record.evidence),
                    record.created_at,
                    record.verified_at,
                ),
            )
            await self.db.conn.commit()

    async def get_release(self, release_id: str) -> ReleaseRecord:
        async with self.db.conn.execute(
            "SELECT * FROM releases WHERE release_id = ?", (release_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            from drawbridge.errors import UnknownReleaseError

            raise UnknownReleaseError(f"release {release_id} does not exist")
        return row_to_release(row)

    async def get_current_release(
        self, app: str, environment: str
    ) -> ReleaseRecord | None:
        async with self.db.conn.execute(
            "SELECT * FROM releases WHERE app = ? AND environment = ?"
            " AND status IN ('succeeded', 'rollback')"
            " ORDER BY created_at DESC LIMIT 1",
            (app, environment),
        ) as cursor:
            row = await cursor.fetchone()
        return row_to_release(row) if row is not None else None

    async def list_releases(
        self,
        app: str,
        environment: str,
        limit: int = 20,
        *,
        before_created_at: float | None = None,
    ) -> list[ReleaseRecord]:
        """Newest-first bounded release history for one target (D8: the
        ``before_created_at`` cursor mirrors the jobs/events pagination)."""
        query = (
            "SELECT * FROM releases WHERE app = ? AND environment = ?"
            " AND (? IS NULL OR created_at < ?)"
            " ORDER BY created_at DESC LIMIT ?"
        )
        async with self.db.conn.execute(
            query,
            (app, environment, before_created_at, before_created_at, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row_to_release(r) for r in rows]

    async def record_artifact(self, record: ArtifactRecord) -> None:
        async with self.db.write_lock():
            await self.db.conn.execute(
                "INSERT INTO artifacts(artifact_id, app, environment, kind, ref,"
                " release_id, size_bytes, sha256, created_at, retention_class)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    record.artifact_id,
                    record.app,
                    record.environment,
                    record.kind,
                    record.ref,
                    record.release_id,
                    record.size_bytes,
                    record.sha256,
                    record.created_at,
                    record.retention_class,
                ),
            )
            await self.db.conn.commit()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def append_event(
        self,
        kind: str,
        *,
        job_id: str | None = None,
        release_id: str | None = None,
        app: str | None = None,
        environment: str | None = None,
        request_id: str | None = None,
        agent_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        async with self.db.write_lock():
            await self.db.conn.execute(
                "INSERT INTO events(ts, kind, request_id, job_id, release_id, app,"
                " environment, agent_id, detail_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    kind,
                    request_id,
                    job_id,
                    release_id,
                    app,
                    environment,
                    agent_id,
                    dumps(detail or {}),
                ),
            )
            await self.db.conn.commit()

    # ------------------------------------------------------------------
    # Retention enforcement (single transaction; OPERATIONS.md §5)
    # ------------------------------------------------------------------

    async def retention_cleanup(
        self,
        *,
        now: float,
        idempotency_key_seconds: float,
        plan_seconds: float,
        diagnostic_job_seconds: float,
        job_record_seconds: float,
    ) -> dict[str, Any]:
        """Delete expired records; never touches releases/artifacts/events.

        Protected invariants: jobs referenced by releases and jobs in
        blocking statuses (rollback_failed / needs_attention) are never
        removed; idempotency keys and steps of a doomed job are removed
        first so foreign keys stay satisfied.  Everything happens in one
        ``BEGIN IMMEDIATE`` transaction.  Returns per-class counts and the
        removed job ids (callers use them to drop spooled log dirs).
        """
        conn = self.db.conn
        async with self.db.write_lock():
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await conn.execute(
                    "DELETE FROM idempotency_keys WHERE created_at < ?",
                    (now - idempotency_key_seconds,),
                )
                expired_keys = cursor.rowcount or 0

                cursor = await conn.execute(
                    "DELETE FROM plans WHERE expires_at < ?"
                    " OR (status IN ('expired', 'rejected') AND created_at < ?)",
                    (now - plan_seconds, now - plan_seconds),
                )
                expired_plans = cursor.rowcount or 0

                # Terminal diagnostic jobs past the diagnostic retention.
                diag_cutoff = now - diagnostic_job_seconds
                cursor = await conn.execute(
                    "SELECT job_id FROM jobs WHERE kind = ? AND finished_at < ?",
                    (JobKind.DIAGNOSTIC, diag_cutoff),
                )
                doomed = [r["job_id"] for r in await cursor.fetchall()]

                # Other terminal jobs past the record retention, unless a
                # release references them or they block their target.
                placeholders = ",".join("?" * len(JobStatus.TERMINAL))
                blocking = ",".join("?" * len(BLOCKING_STATUSES))
                cursor = await conn.execute(
                    "SELECT j.job_id FROM jobs j WHERE j.status IN (" + placeholders + ")"  # noqa: S608
                    " AND j.kind != ? AND j.finished_at < ?"
                    " AND j.status NOT IN (" + blocking + ")"
                    " AND NOT EXISTS (SELECT 1 FROM releases r WHERE r.job_id = j.job_id)",
                    (
                        *JobStatus.TERMINAL,
                        JobKind.DIAGNOSTIC,
                        now - job_record_seconds,
                        *BLOCKING_STATUSES,
                    ),
                )
                doomed.extend(r["job_id"] for r in await cursor.fetchall())

                removed_steps = 0
                removed_keys = 0
                removed_jobs = 0
                for job_id in doomed:
                    cursor = await conn.execute(
                        "DELETE FROM steps WHERE job_id = ?", (job_id,)
                    )
                    removed_steps += cursor.rowcount or 0
                    cursor = await conn.execute(
                        "DELETE FROM idempotency_keys WHERE job_id = ?", (job_id,)
                    )
                    removed_keys += cursor.rowcount or 0
                    cursor = await conn.execute(
                        "DELETE FROM jobs WHERE job_id = ?", (job_id,)
                    )
                    removed_jobs += cursor.rowcount or 0
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return {
            "idempotency_keys": expired_keys + removed_keys,
            "plans": expired_plans,
            "jobs": removed_jobs,
            "steps": removed_steps,
            "removed_job_ids": doomed,
        }
