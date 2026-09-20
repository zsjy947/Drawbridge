"""Simulation runtime adapter: full-state-machine deploys without containers.

The tech design (§4) fixes a Runtime Module seam — ``inspect / deploy /
restart / rollback`` behind one interface with replaceable adapters.  This
module provides the second adapter: every deploy_verify step keeps its
validation, budget, persistence and recovery semantics, but the steps that
would touch BuildKit / Docker / Compose record simulated evidence instead.

What stays REAL in simulation mode:

* plan-time ref resolution and reachability (git);
* the source snapshot (``git archive`` + safe extraction) — the release
  directory really contains the frozen commit's files;
* the compose template rendering contract (single ``REPLACE_BY_DRAWBRIDGE``
  token replaced by a synthetic immutable image id);
* every SQLite transition: plan → job admission → steps → release record →
  rollback chain, all under the same locks and idempotency rules.

What is SIMULATED: the build/import/identify round-trip (a synthetic
``sha256:…`` image id derived from commit + job), ``compose up``/``restart``
(no containers are created), health probes and the test container (they
report ``validation_level: simulation`` evidence instead of probing).

The adapter exists so the Gateway↔Runner communication channel, the queue
semantics and the workflow state machine can be exercised end-to-end on any
host that has Python and git — including the Windows development machines —
without systemd, Docker or BuildKit (see ``drawbridge-simulate``).
Simulated releases must never be presented as production evidence; every
result marks itself ``simulated``.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from drawbridge.config.models import DrawbridgeConfig, EnvironmentConfig
from drawbridge.errors import DrawbridgeError, ErrorCode
from drawbridge.executor.process import ProcessManager
from drawbridge.runner.deploy import DeployState
from drawbridge.runner.runtime import DeployRuntime, unique_image_tag
from drawbridge.state.records import ReleaseRecord
from drawbridge.state.store import Store

#: Name of the synthetic application log written at deploy time; ops_logs
#: serves pages from this file in simulation mode.
SIMULATION_LOG_NAME = "simulation.log"

#: Lines appended to the synthetic application log per deploy.
_SIMULATION_LOG_LINES = (
    "starting drawbridge simulation service",
    "loaded application configuration",
    "listening on configured port (simulated)",
    "health probe succeeded (simulated)",
    "ready to serve requests (simulated)",
)


def synthetic_image_id(commit_sha: str, job_id: str) -> str:
    """Deterministic immutable-style id for a simulated release.

    Format-compatible with the real pipeline's ``image inspect`` output so
    the compose rendering contract (``sha256:<64 hex>``) is exercised.
    """
    digest = hashlib.sha256(f"drawbridge-simulation:{commit_sha}:{job_id}".encode()).hexdigest()
    return f"sha256:{digest}"


def simulation_checks(env_cfg: EnvironmentConfig) -> list[dict[str, Any]]:
    """Health evidence shaped like :func:`probe_health_checks` output."""
    return [
        {
            "url": hc.url,
            "passed": True,
            "last_status": hc.expected_status,
            "consecutive_successes": hc.consecutive_successes,
            "validation_level": "simulation",
        }
        for hc in env_cfg.health_checks
    ]


class SimulationRuntime(DeployRuntime):
    """deploy_verify step executor that records evidence instead of running.

    Inherits the portable plumbing from :class:`DeployRuntime` (job
    directories, git client, compose rendering, disk preflight) and replaces
    every container-touching step.  Unlike the production runtime there is
    no Linux gate: the whole point is running the communication channel on
    hosts without Docker.
    """

    def __init__(
        self,
        *,
        config: DrawbridgeConfig,
        store: Store,
        process_manager: ProcessManager,
        log_dir: str,
    ) -> None:
        super().__init__(
            config=config,
            store=store,
            process_manager=process_manager,
            log_dir=log_dir,
        )
        # Rebind the step registry to the overridden (simulated) methods.
        self._steps = {
            "release_preflight": self.step_release_preflight,
            "source_snapshot": self.step_source_snapshot,
            "image_build": self.step_image_build,
            "image_import": self.step_image_import,
            "image_identify": self.step_image_identify,
            "compose_deploy": self.step_compose_deploy,
            "health_check": self.step_health_check,
            "test_suite": self.step_test_suite,
            "restore_previous": self.step_restore_previous,
            "stop_initial": self.step_stop_initial,
            "release_finalize": self.step_release_finalize,
        }

    async def __call__(
        self, operation: str, state: DeployState, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        step = self._steps.get(operation)
        if step is None:
            raise DrawbridgeError(
                f"unknown internal deploy operation {operation!r}",
                code=ErrorCode.UNKNOWN_OPERATION,
            )
        result: dict[str, Any] = await step(state, params or {})
        return result

    # -- simulated steps ----------------------------------------------------

    async def step_image_build(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        env_cfg = self._env_cfg(state)
        profile = self.config.build_profile(env_cfg.build_profile)
        if not state.source_dir:
            raise DrawbridgeError(
                "image_build ran without a source snapshot", code=ErrorCode.INTERNAL
            )
        # Real check on the frozen snapshot: the declared Dockerfile must
        # exist exactly like the BuildKit frontend would require.
        context_dir = Path(state.source_dir) / profile.context
        dockerfile = context_dir / profile.dockerfile_basename
        if not dockerfile.is_file():
            raise DrawbridgeError(
                f"dockerfile {profile.dockerfile_basename!r} missing in snapshot",
                code=ErrorCode.CONFIG_INVALID,
            )
        tag = unique_image_tag(state.app, state.environment, state.job.job_id)
        manifest = self._jobs_dir(state) / "image.simulation.json"
        manifest.write_text(
            json.dumps(
                {
                    "simulated": True,
                    "commit_sha": state.commit_sha,
                    "job_id": state.job.job_id,
                    "image_tag": tag,
                    "platform": profile.platform,
                    "generated_at": time.time(),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return {"image_tag": tag, "image_archive": str(manifest), "simulated": True}

    async def step_image_import(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        manifest = self._jobs_dir(state) / "image.simulation.json"
        if not manifest.is_file():
            raise DrawbridgeError(
                "image_import ran before image_build produced a manifest",
                code=ErrorCode.INTERNAL,
            )
        return {"loaded": True, "image_archive": str(manifest), "simulated": True}

    async def step_image_identify(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not state.image_tag:
            raise DrawbridgeError(
                "image_identify ran without a candidate tag", code=ErrorCode.INTERNAL
            )
        sha = state.commit_sha or ""
        return {
            "image_id": synthetic_image_id(sha, state.job.job_id),
            "simulated": True,
        }

    async def step_compose_deploy(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        env_cfg = self._env_cfg(state)
        image_id = state.image_id
        if not image_id:
            raise DrawbridgeError(
                "compose_deploy ran without a verified image id",
                code=ErrorCode.INTERNAL,
            )
        # Real rendering contract: single token, immutable id, atomic write.
        rendered = self._render_compose(env_cfg, image_id)
        self._write_simulation_log(env_cfg, image_id)
        return {
            "compose_file": str(rendered),
            "image_id": image_id,
            "simulated": True,
        }

    async def step_health_check(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {"checks": simulation_checks(self._env_cfg(state)), "simulated": True}

    async def step_test_suite(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        env_cfg = self._env_cfg(state)
        suite_name = str(params.get("suite") or "smoke")
        # The suite must still be registered — simulation does not widen the
        # catalog, it only skips the container lifecycle.
        self._suite_for(env_cfg, suite_name)
        return {
            "suite": suite_name,
            "container": "simulation",
            "exit_code": 0,
            "log_excerpt": (f"[simulation] suite {suite_name} passed without running containers"),
            "cleanup_warnings": [],
            "simulated": True,
        }

    async def step_restore_previous(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        baseline = state.baseline
        if baseline is None or not baseline.image_id:
            raise DrawbridgeError(
                "restore_previous ran without a baseline artifact",
                code=ErrorCode.ROLLBACK_FAILED,
            )
        env_cfg = self._env_cfg(state)
        rendered = self._render_compose(env_cfg, baseline.image_id)
        self._write_simulation_log(env_cfg, baseline.image_id)
        return {
            "restored_release_id": baseline.release_id,
            "restored_image_id": baseline.image_id,
            "compose_file": str(rendered),
            "checks": simulation_checks(env_cfg),
            "test": None,
            "simulated": True,
        }

    async def step_stop_initial(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        env_cfg = self._env_cfg(state)
        rendered = Path(env_cfg.deploy_root) / "compose.rendered.yaml"
        if not rendered.is_file():
            return {"stopped": False, "reason": "no rendered compose file"}
        return {"stopped": True, "simulated": True}

    async def step_release_finalize(
        self, state: DeployState, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        image_id = state.image_id
        if not image_id:
            raise DrawbridgeError(
                "release_finalize ran without an image id", code=ErrorCode.INTERNAL
            )
        env_cfg = self._env_cfg(state)
        rendered = Path(env_cfg.deploy_root) / "compose.rendered.yaml"
        if not rendered.is_file():
            raise DrawbridgeError(
                "drift detected: the rendered compose file is missing",
                code=ErrorCode.DRIFT_DETECTED,
            )
        # Drift check adapted to the simulated runtime: the deployed image
        # id must be the one referenced by the rendered compose file.
        text = rendered.read_text(encoding="utf-8", errors="replace")
        if image_id not in text:
            raise DrawbridgeError(
                f"drift detected: rendered compose does not reference the release image {image_id}",
                code=ErrorCode.DRIFT_DETECTED,
            )
        log_file = Path(env_cfg.deploy_root) / SIMULATION_LOG_NAME
        if not log_file.is_file():
            raise DrawbridgeError(
                "drift detected: the simulation application log is missing",
                code=ErrorCode.DRIFT_DETECTED,
            )
        return {
            "running_images": [image_id],
            "verified_image": image_id,
            "simulated": True,
        }

    # -- reusable flows for job-kind handlers (ops_test / rollback) ---------

    async def run_test_container(
        self,
        *,
        app: str,
        environment: str,
        suite: Any,
        job_id: str,
    ) -> dict[str, Any]:
        return {
            "container": "simulation",
            "exit_code": 0,
            "log_excerpt": (
                f"[simulation] suite {suite.image_id[:24]}… passed without running containers"
            ),
            "cleanup_warnings": [],
            "simulated": True,
            "observed_at": time.time(),
        }

    async def restore_to_release(
        self, app: str, environment: str, release: ReleaseRecord
    ) -> dict[str, Any]:
        if not release.image_id:
            raise DrawbridgeError(
                f"release {release.release_id} has no retained image artifact",
                code=ErrorCode.ROLLBACK_FAILED,
            )
        env_cfg = self.config.environment(app, environment)
        rendered = self._render_compose(env_cfg, release.image_id)
        self._write_simulation_log(env_cfg, release.image_id)
        return {
            "compose_file": str(rendered),
            "checks": simulation_checks(env_cfg),
            "simulated": True,
        }

    # -- helpers -------------------------------------------------------------

    def _write_simulation_log(self, env_cfg: EnvironmentConfig, image_id: str) -> None:
        """Append a bounded synthetic application log for ops_logs paging."""
        root = Path(env_cfg.deploy_root)
        root.mkdir(parents=True, exist_ok=True)
        log_file = root / SIMULATION_LOG_NAME
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        lines = [f"{stamp} api [{image_id[:19]}] {message}" for message in _SIMULATION_LOG_LINES]
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


class RuntimeSelector:
    """Dispatches each target to its configured runtime adapter.

    The Runner and the job handlers only ever see this object: per the tech
    design the runtime is a seam, and ``environments.<env>.runtime`` selects
    the adapter (``compose`` → production executor, ``simulation`` → this
    module).  Unknown values cannot occur — the config model fixes the
    Literal.
    """

    def __init__(
        self,
        *,
        config: DrawbridgeConfig,
        compose: DeployRuntime,
        simulation: SimulationRuntime,
    ) -> None:
        self.config = config
        self.compose = compose
        self.simulation = simulation

    def adapter_for(self, app: str, environment: str) -> DeployRuntime:
        env = self.config.environment(app, environment)
        if env.runtime == "simulation":
            return self.simulation
        return self.compose

    async def __call__(
        self, operation: str, state: DeployState, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        adapter = self.adapter_for(state.app, state.environment)
        return await adapter(operation, state, params)

    async def run_test_container(
        self,
        *,
        app: str,
        environment: str,
        suite: Any,
        job_id: str,
    ) -> dict[str, Any]:
        adapter = self.adapter_for(app, environment)
        return await adapter.run_test_container(
            app=app, environment=environment, suite=suite, job_id=job_id
        )

    async def restore_to_release(
        self, app: str, environment: str, release: ReleaseRecord
    ) -> dict[str, Any]:
        adapter = self.adapter_for(app, environment)
        return await adapter.restore_to_release(app, environment, release)


def build_runtime(
    *,
    config: DrawbridgeConfig,
    store: Store,
    process_manager: ProcessManager,
    log_dir: str,
) -> RuntimeSelector:
    """The single construction point for the runtime adapter stack."""
    return RuntimeSelector(
        config=config,
        compose=DeployRuntime(
            config=config,
            store=store,
            process_manager=process_manager,
            log_dir=log_dir,
        ),
        simulation=SimulationRuntime(
            config=config,
            store=store,
            process_manager=process_manager,
            log_dir=log_dir,
        ),
    )


__all__ = [
    "SIMULATION_LOG_NAME",
    "RuntimeSelector",
    "SimulationRuntime",
    "build_runtime",
    "simulation_checks",
    "synthetic_image_id",
]
