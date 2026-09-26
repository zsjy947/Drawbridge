"""Retention enforcement orchestration (OPERATIONS.md §5, MVP spec §8).

The database half lives in :meth:`Store.retention_cleanup`; this module
adds the filesystem halves — spooled job log directories and per-job work
directories under ``deploy_root/jobs/<job_id>/`` (source tar + extracted
source + image tar; plan D15) — plus the audit event.  The Runner calls
:func:`run_retention` on a throttled schedule.

What is intentionally NOT cleaned here:

* releases and artifacts — audit history and rollback chains;
* jobs in blocking statuses (rollback_failed / needs_attention) — the
  target-blocking semantics outrank retention, and their work directories
  are preserved as incident evidence by the same list;
* jobs referenced by any release row — the audit chain stays complete;
* anything else under ``deploy_root`` (compose.rendered.yaml,
  simulation.log) — only the retention list's job ids are touched;
* actual container images — Docker-side reclamation stays a manual,
  target-host operation (OPERATIONS.md §5).
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from drawbridge.config.models import DrawbridgeConfig
from drawbridge.logsetup import get_logger
from drawbridge.state.store import Store

log = get_logger("drawbridge.retention")


def _remove_log_dirs(log_dir: Path, job_ids: list[str]) -> list[str]:
    """Best-effort removal of spooled log directories; failures log only."""
    removed: list[str] = []
    for job_id in job_ids:
        directory = log_dir / job_id
        try:
            if directory.is_dir():
                shutil.rmtree(directory)
                removed.append(job_id)
        except OSError as exc:
            log.warning("cannot remove job log directory", job_id=job_id, error=str(exc))
    return removed


def _remove_job_work_dirs(config: DrawbridgeConfig, job_ids: list[str]) -> list[str]:
    """Best-effort removal of ``deploy_root/jobs/<job_id>/`` work directories
    (plan D15): each retired deploy leaves behind source.tar, the extracted
    source tree and image.tar — the tar is redundant after ``docker load``
    (rollback reads the Engine image, never the tar), and GB-scale model
    images otherwise exhaust the disk after a few dozen deploys.  Only the
    retention deletion list is touched; blocking-status jobs are never on
    it, so incident scenes are preserved automatically."""
    removed: list[str] = []
    jobs_roots = {
        Path(env.deploy_root) / "jobs"
        for app in config.apps.values()
        for env in app.environments.values()
    }
    for jobs_root in jobs_roots:
        for job_id in job_ids:
            directory = jobs_root / job_id
            try:
                if directory.is_dir():
                    shutil.rmtree(directory)
                    removed.append(job_id)
            except OSError as exc:
                log.warning(
                    "cannot remove job work directory", job_id=job_id, error=str(exc)
                )
    return removed


async def run_retention(config: DrawbridgeConfig, store: Store) -> dict[str, Any]:
    """One retention pass; writes an audit event when anything was removed."""
    policy = config.main.retention
    now = time.time()
    counts = await store.retention_cleanup(
        now=now,
        idempotency_key_seconds=policy.idempotency_key_days * 86400,
        plan_seconds=policy.plan_days * 86400,
        diagnostic_job_seconds=float(config.main.diagnostics.retention_seconds),
        job_record_seconds=policy.job_record_days * 86400,
    )
    removed_job_ids = list(counts.pop("removed_job_ids"))
    counts["log_dirs"] = len(_remove_log_dirs(Path(config.main.paths.log_dir), removed_job_ids))
    counts["job_work_dirs"] = len(_remove_job_work_dirs(config, removed_job_ids))
    if any(counts.values()):
        await store.append_event("retention_cleanup", detail=dict(counts))
        log.info("retention cleanup", **counts)
    return counts
