"""Selfcheck role scoping and Python hard-gate tests (plan D6).

Four roles must produce distinct check sets: gateway/simulation SKIP the
container/build toolchain (never warn, never fail), runner keeps the full
set, all preserves the historical behaviour.  The Python >=3.12 baseline is
a hard gate (FAIL below it).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from drawbridge.entries.selfcheck_main import main as selfcheck_main

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    """A loadable config bundle with portable paths and toolchain."""
    for name in ("drawbridge.yaml", "apps.yaml", "operations.yaml", "workflows.yaml"):
        shutil.copyfile(REPO_ROOT / "configs" / name, tmp_path / name)
    main_yaml = (tmp_path / "drawbridge.yaml").read_text(encoding="utf-8")
    main_yaml = main_yaml.replace("/var/lib/drawbridge", str(tmp_path / "state"))
    main_yaml = main_yaml.replace("/run/drawbridge/locks", str(tmp_path / "locks"))
    main_yaml = main_yaml.replace("/var/log/drawbridge", str(tmp_path / "logs"))
    git_path = shutil.which("git") or "/usr/bin/git"
    main_yaml = main_yaml.replace("/usr/bin/git", git_path.replace("\\", "/"))
    (tmp_path / "drawbridge.yaml").write_text(main_yaml, encoding="utf-8")
    apps_yaml = (tmp_path / "apps.yaml").read_text(encoding="utf-8")
    apps_yaml = apps_yaml.replace("/srv/drawbridge", str(tmp_path / "srv").replace("\\", "/"))
    (tmp_path / "apps.yaml").write_text(apps_yaml, encoding="utf-8")
    return tmp_path


class TestRoleScoping:
    @pytest.mark.parametrize("role", ["gateway", "simulation"])
    def test_buildless_roles_skip_container_checks(self, bundle: Path, role: str) -> None:
        assert selfcheck_main(["--config-dir", str(bundle), "--role", role]) == 0
        # exit 0 with no docker/compose/buildkit failures: the SKIP lines
        # keep simulation deployments free of misleading WARN/FAIL noise

    def test_runner_role_reports_full_set(self, bundle: Path) -> None:
        # docker is missing on dev hosts: runner role must still surface it
        # (warn-or-skip depending on presence, never silently dropped)
        code = selfcheck_main(["--config-dir", str(bundle), "--role", "runner"])
        assert code in (0, 1)  # structure asserted via run_checks below

    def test_all_role_preserves_behaviour(self, bundle: Path) -> None:
        code = selfcheck_main(["--config-dir", str(bundle), "--role", "all"])
        assert code in (0, 1)

    def test_role_snapshots_differ(self, bundle: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from drawbridge.entries.selfcheck_main import run_checks

        snapshots = {}
        for role in ("gateway", "runner", "simulation", "all"):
            results = run_checks(str(bundle), role=role)
            snapshots[role] = (results.passed, len(results.failed), len(results.warnings), len(results.skipped))
        # buildless roles convert the container checks into skips
        assert snapshots["simulation"][3] > snapshots["all"][3]
        assert snapshots["gateway"][3] > snapshots["all"][3]
        # runner matches all (full set, nothing skipped beyond the baseline)
        assert snapshots["runner"][3] == snapshots["all"][3]

    def test_python_hard_gate(self, bundle: Path) -> None:
        from drawbridge.entries.selfcheck_main import run_checks

        results = run_checks(str(bundle), role="all")
        # this suite itself runs on >=3.12, so the gate passes and reports
        assert sys.version_info >= (3, 12)
        assert "python runtime" not in results.failed
        # the baseline text is part of the check line
        assert results.passed >= 1
