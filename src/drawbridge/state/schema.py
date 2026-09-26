"""SQLite schema (version 2) — MVP spec §8.

Tables: plans, jobs, steps, releases, artifacts, idempotency_keys, events,
control_state.  The database holds metadata and bounded summaries only;
full job output lives in the registered log directory.

Version 2 (plan D3+D12): ``plans`` gains ``compose_template_digest`` and
``plan_schema_version`` (template fingerprint frozen into every new plan);
``releases`` gains the ``simulated`` flag (cross-runtime baseline identity).
Existing version-1 databases migrate in place via ``MIGRATIONS_V1_TO_V2``.
"""

from __future__ import annotations

SCHEMA_VERSION = 2

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS control_state (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plans (
        plan_id                  TEXT PRIMARY KEY,
        app                      TEXT NOT NULL,
        environment              TEXT NOT NULL,
        workflow                 TEXT NOT NULL,
        source_mode              TEXT NOT NULL,
        git_ref                  TEXT NOT NULL,
        commit_sha               TEXT,
        config_digest            TEXT NOT NULL,
        compose_template_digest  TEXT,
        plan_schema_version      INTEGER,
        baseline_release_id      TEXT,
        status                   TEXT NOT NULL,
        created_at               REAL NOT NULL,
        expires_at               REAL NOT NULL,
        params_json              TEXT NOT NULL,
        request_id               TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plans_target
        ON plans(app, environment, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id                 TEXT PRIMARY KEY,
        kind                   TEXT NOT NULL,
        action                 TEXT NOT NULL,
        app                    TEXT NOT NULL,
        environment            TEXT NOT NULL,
        plan_id                TEXT UNIQUE,
        status                 TEXT NOT NULL,
        owner                  TEXT,
        heartbeat_at           REAL,
        deadline_at            REAL,
        queue_expires_at       REAL,
        config_digest          TEXT,
        params_json            TEXT NOT NULL,
        result_json            TEXT,
        recovery_json          TEXT,
        runtime_change_started INTEGER NOT NULL DEFAULT 0,
        queued_at              REAL NOT NULL,
        started_at             REAL,
        finished_at            REAL,
        request_id             TEXT,
        agent_id               TEXT,
        parent_task_id         TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_target ON jobs(app, environment, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS steps (
        step_id            TEXT PRIMARY KEY,
        job_id             TEXT NOT NULL REFERENCES jobs(job_id),
        seq                INTEGER NOT NULL,
        name               TEXT NOT NULL,
        status             TEXT NOT NULL,
        started_at         REAL,
        finished_at        REAL,
        exit_code          INTEGER,
        termination_reason TEXT,
        log_ref            TEXT,
        detail_json        TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_steps_job ON steps(job_id, seq)
    """,
    """
    CREATE TABLE IF NOT EXISTS releases (
        release_id      TEXT PRIMARY KEY,
        app             TEXT NOT NULL,
        environment     TEXT NOT NULL,
        plan_id         TEXT,
        job_id          TEXT,
        commit_sha      TEXT,
        image_id        TEXT,
        image_tag       TEXT,
        config_digest   TEXT NOT NULL,
        simulated       INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL,
        rollback_of     TEXT,
        compose_path    TEXT,
        deploy_dir      TEXT,
        evidence_json   TEXT,
        created_at      REAL NOT NULL,
        verified_at     REAL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_releases_target
        ON releases(app, environment, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        artifact_id     TEXT PRIMARY KEY,
        app             TEXT NOT NULL,
        environment     TEXT NOT NULL,
        kind            TEXT NOT NULL,
        ref             TEXT NOT NULL,
        release_id      TEXT,
        size_bytes      INTEGER,
        sha256          TEXT,
        created_at      REAL NOT NULL,
        retention_class TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artifacts_target
        ON artifacts(app, environment, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        key            TEXT PRIMARY KEY,
        action         TEXT NOT NULL,
        app            TEXT NOT NULL,
        environment    TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        job_id         TEXT NOT NULL REFERENCES jobs(job_id),
        created_at     REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_idem_digest
        ON idempotency_keys(action, app, environment, request_digest)
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          REAL NOT NULL,
        kind        TEXT NOT NULL,
        request_id  TEXT,
        job_id      TEXT,
        release_id  TEXT,
        app         TEXT,
        environment TEXT,
        agent_id    TEXT,
        detail_json TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_target ON events(app, environment, ts)
    """,
)

#: In-place upgrade of an existing version-1 database (single transaction).
#: The releases backfill flags pre-D12 simulation releases via their evidence
#: marker so cross-runtime baseline filtering (D12) sees them correctly.
MIGRATIONS_V1_TO_V2 = (
    "ALTER TABLE plans ADD COLUMN compose_template_digest TEXT",
    "ALTER TABLE plans ADD COLUMN plan_schema_version INTEGER",
    "ALTER TABLE releases ADD COLUMN simulated INTEGER NOT NULL DEFAULT 0",
    "UPDATE releases SET simulated = 1"
    " WHERE evidence_json LIKE '%\"validation_level\":\"simulation\"%'",
)
