"""HTTP-mode simulation scenario test (optimization plan B).

Drives ``run_http_scenario`` — the real network stack: EdgeMiddleware
(CIDR/Host/Origin + bearer token with a negative anonymous check), the MCP
Streamable HTTP protocol app under uvicorn, and the official MCP SDK
client — against the generated simulation fixture.  Requires only Python
and git, so it runs on the Windows development hosts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drawbridge.entries.simulate_main import (
    _default_source_config_dir,
    create_fixture,
    run_http_scenario,
)

pytestmark = pytest.mark.skipif(
    _default_source_config_dir() is None,
    reason="repository config bundle not available",
)


@pytest.fixture()
def fixture_dir(tmp_path: Path) -> Path:
    source = _default_source_config_dir()
    assert source is not None
    return create_fixture(tmp_path, source)


async def test_http_scenario_with_token(fixture_dir: Path) -> None:
    report = await run_http_scenario(
        fixture_dir, app="demo", environment="staging", job_timeout=120.0
    )
    assert report["ok"] is True, report["steps"]
    assert report["mode"] == "http"
    assert report["auth"] == "token"

    step_names = {s["name"] for s in report["steps"]}
    assert {
        "edge_auth#anonymous",
        "mcp_initialize",
        "tools_list",
        "ops_catalog",
        "ops_history",
        "deploy_job#one",
        "deploy_job#two",
        "ops_logs#after_deploy",
        "rollback_job",
    } <= step_names

    # The anonymous request was refused by the edge middleware.
    anon = next(s for s in report["steps"] if s["name"] == "edge_auth#anonymous")
    assert anon["detail"]["rejected"] is True

    # Two deploys + explicit rollback: the rollback release is current.
    assert len(report["releases"]) == 2
    assert report["current_release"]["status"] == "rollback"

    # Logs came back over HTTP from the simulated application log.
    logs = next(s for s in report["steps"] if s["name"] == "ops_logs#after_deploy")
    assert logs["detail"]["lines"]


async def test_http_scenario_without_token(fixture_dir: Path) -> None:
    report = await run_http_scenario(
        fixture_dir,
        app="demo",
        environment="staging",
        job_timeout=120.0,
        use_token=False,
    )
    assert report["ok"] is True, report["steps"]
    assert report["auth"] == "none"
    assert not any(s["name"] == "edge_auth#anonymous" for s in report["steps"])
