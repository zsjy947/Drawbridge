"""Typed records exchanged with the state store."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    FAILED_NO_BASELINE = "failed_no_baseline"
    NEEDS_ATTENTION = "needs_attention"
    QUEUE_EXPIRED = "queue_expired"

    TERMINAL = frozenset(
        {
            SUCCEEDED,
            FAILED,
            ROLLED_BACK,
            ROLLBACK_FAILED,
            FAILED_NO_BASELINE,
            NEEDS_ATTENTION,
            QUEUE_EXPIRED,
        }
    )
    RUNNING_STATES = frozenset({QUEUED, RUNNING})


class PlanStatus:
    PLANNED = "planned"
    APPLIED = "applied"
    EXPIRED = "expired"
    REJECTED = "rejected"


class StepStatus:
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ReleaseStatus:
    SUCCEEDED = "succeeded"    # produced by a successful deploy
    ROLLBACK = "rollback"      # produced by a rollback run (points back)
    SUPERSEDED = "superseded"  # an older release no longer deployed


class JobKind:
    DEPLOY = "deploy"
    TEST = "test"
    RESTART = "restart"
    ROLLBACK = "rollback"
    DIAGNOSTIC = "diagnostic"


def dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def loads(raw: str | None) -> Any:
    if not raw:
        return None
    return json.loads(raw)


@dataclass(frozen=True)
class PlanRecord:
    plan_id: str
    app: str
    environment: str
    workflow: str
    source_mode: str
    git_ref: str
    commit_sha: str | None
    config_digest: str
    baseline_release_id: str | None
    status: str
    created_at: float
    expires_at: float
    params: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    #: Fingerprint of the compose template the plan was validated against
    #: (schema v2, plan D3).  Legacy rows have NULL — always STALE_PLAN.
    compose_template_digest: str | None = None
    #: Plan record shape version; new plans are always 2.
    plan_schema_version: int | None = None


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    kind: str
    action: str
    app: str
    environment: str
    plan_id: str | None
    status: str
    params: dict[str, Any]
    queued_at: float
    owner: str | None = None
    heartbeat_at: float | None = None
    deadline_at: float | None = None
    queue_expires_at: float | None = None
    config_digest: str | None = None
    result: dict[str, Any] | None = None
    recovery: dict[str, Any] | None = None
    runtime_change_started: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    request_id: str | None = None
    agent_id: str | None = None
    parent_task_id: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        """Client-visible projection (no internals, bounded detail)."""
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "action": self.action,
            "app": self.app,
            "environment": self.environment,
            "status": self.status,
            "plan_id": self.plan_id,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "runtime_change_started": self.runtime_change_started,
        }


@dataclass(frozen=True)
class StepRecord:
    step_id: str
    job_id: str
    seq: int
    name: str
    status: str
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    termination_reason: str | None = None
    log_ref: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReleaseRecord:
    release_id: str
    app: str
    environment: str
    plan_id: str | None
    job_id: str | None
    commit_sha: str | None
    image_id: str | None
    image_tag: str | None
    config_digest: str
    status: str
    rollback_of: str | None
    compose_path: str | None
    deploy_dir: str | None
    evidence: dict[str, Any]
    created_at: float
    verified_at: float | None = None
    #: True when produced by the simulation runtime adapter (schema v2,
    #: plan D12): never a valid baseline/current release on a compose target.
    simulated: bool = False


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    app: str
    environment: str
    kind: str
    ref: str
    release_id: str | None
    size_bytes: int | None
    sha256: str | None
    created_at: float
    retention_class: str


def row_to_plan(row: Any) -> PlanRecord:
    keys = row.keys()
    return PlanRecord(
        plan_id=row["plan_id"],
        app=row["app"],
        environment=row["environment"],
        workflow=row["workflow"],
        source_mode=row["source_mode"],
        git_ref=row["git_ref"],
        commit_sha=row["commit_sha"],
        config_digest=row["config_digest"],
        baseline_release_id=row["baseline_release_id"],
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        params=loads(row["params_json"]) or {},
        request_id=row["request_id"],
        compose_template_digest=(
            row["compose_template_digest"] if "compose_template_digest" in keys else None
        ),
        plan_schema_version=(
            row["plan_schema_version"] if "plan_schema_version" in keys else None
        ),
    )


def row_to_job(row: Any) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        kind=row["kind"],
        action=row["action"],
        app=row["app"],
        environment=row["environment"],
        plan_id=row["plan_id"],
        status=row["status"],
        params=loads(row["params_json"]) or {},
        queued_at=row["queued_at"],
        owner=row["owner"],
        heartbeat_at=row["heartbeat_at"],
        deadline_at=row["deadline_at"],
        queue_expires_at=row["queue_expires_at"],
        config_digest=row["config_digest"],
        result=loads(row["result_json"]),
        recovery=loads(row["recovery_json"]),
        runtime_change_started=bool(row["runtime_change_started"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        request_id=row["request_id"],
        agent_id=row["agent_id"],
        parent_task_id=row["parent_task_id"],
    )


def row_to_release(row: Any) -> ReleaseRecord:
    keys = row.keys()
    return ReleaseRecord(
        release_id=row["release_id"],
        app=row["app"],
        environment=row["environment"],
        plan_id=row["plan_id"],
        job_id=row["job_id"],
        commit_sha=row["commit_sha"],
        image_id=row["image_id"],
        image_tag=row["image_tag"],
        config_digest=row["config_digest"],
        status=row["status"],
        rollback_of=row["rollback_of"],
        compose_path=row["compose_path"],
        deploy_dir=row["deploy_dir"],
        evidence=loads(row["evidence_json"]) or {},
        created_at=row["created_at"],
        verified_at=row["verified_at"],
        simulated=bool(row["simulated"]) if "simulated" in keys else False,
    )


def record_to_dict(record: Any) -> dict[str, Any]:
    return asdict(record)
