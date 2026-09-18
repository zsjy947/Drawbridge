"""End-to-end functional flows over a live HTTP gateway.

Exercises the features introduced by the deploy_verify runtime commit as
they behave together in a running system:

* the catalog exposes the new diagnostic operations;
* internal operations stay forbidden through the public tool;
* maintenance mode blocks writes at the gateway while reads keep working;
* a needs_attention/rollback_failed job blocks further mutations on its
  target until reconciled;
* ops_logs arguments are validated strictly at the edge (no coercion);
* admission writes append-only audit events.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

from drawbridge.config.loader import load_config_from_dir
from drawbridge.state.db import Database
from drawbridge.state.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
async def gateway(tmp_path: Path):
    """Live gateway stack (edge middleware + MCP app) plus its state DB."""
    config = load_config_from_dir(CONFIG_DIR)
    config.main.paths.state_dir = str(tmp_path / "state")
    config.main.paths.lock_dir = str(tmp_path / "locks")
    config.main.paths.log_dir = str(tmp_path / "logs")
    config.main.concurrency.min_deploy_interval_seconds = 0
    port = _free_port()
    config.main.server.allowed_cidrs = ["127.0.0.0/8"]
    config.main.server.allowed_hosts = [f"127.0.0.1:{port}"]

    from drawbridge.gateway.mcp_app import MCPAppFactory
    from drawbridge.gateway.middleware import EdgeMiddleware
    from drawbridge.gateway.service import GatewayService

    database = Database(tmp_path / "state" / "state.db")
    await database.connect()
    await database.initialize()
    store = Store(database)
    service = GatewayService(config, store)
    factory = MCPAppFactory(service)
    edge = EdgeMiddleware(factory.build_asgi_app(), config.main.server, None)

    import uvicorn

    uv_config = uvicorn.Config(
        edge,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        proxy_headers=False,
        lifespan="on",
    )
    server = uvicorn.Server(uv_config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert server.started, "gateway did not start in time"
    yield {"url": f"http://127.0.0.1:{port}/mcp", "db": tmp_path / "state" / "state.db"}
    server.should_exit = True
    thread.join(timeout=10)
    await database.close()


class MCPClient:
    """Minimal MCP Streamable HTTP client for functional assertions."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._id = 0
        self._headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

    async def post(self, client: httpx.AsyncClient, method: str, params: dict) -> dict:
        self._id += 1
        response = await client.post(
            self.url,
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
            headers=self._headers,
        )
        assert response.status_code == 200, response.text
        body = response.text
        if body.startswith("event:") or "\ndata:" in body:
            body = next(
                line[5:].strip()
                for line in body.splitlines()
                if line.startswith("data:")
            )
        parsed = json.loads(body)
        return parsed["result"]

    async def call(self, client: httpx.AsyncClient, name: str, arguments: dict) -> dict:
        result = await self.post(client, "tools/call", {"name": name, "arguments": arguments})
        if isinstance(result, dict) and result.get("isError"):
            payload = json.loads(result["content"][0]["text"])
            return {"is_error": True, **payload}
        payload = result.get("structured_content")
        if payload is None:
            payload = json.loads(result["content"][0]["text"])
        return {"is_error": False, **payload}

    async def list_tools(self, client: httpx.AsyncClient) -> list[str]:
        result = await self.post(client, "tools/list", {})
        return [t["name"] for t in result["tools"]]


async def _set_maintenance(db_path: Path, value: bool) -> None:
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "UPDATE control_state SET value=?, updated_at=strftime('%s','now')*1.0 "
            "WHERE key='maintenance'",
            ("true" if value else "false",),
        )
        await conn.commit()


async def _insert_blocking_job(db_path: Path, status: str) -> None:
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO jobs(job_id, kind, action, app, environment, status,"
            " params_json, queued_at)"
            " VALUES('blocking-job-0001', 'deploy', 'deploy_verify', 'demo',"
            " 'staging', ?, '{}', ?)",
            (status, time.time()),
        )
        await conn.commit()


async def _remove_blocking_jobs(db_path: Path) -> None:
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("DELETE FROM jobs WHERE job_id='blocking-job-0001'")
        await conn.commit()


async def _event_count(db_path: Path, kind: str) -> int:
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn, conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = ?", (kind,)
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


class TestFunctionalFlows:
    async def test_catalog_includes_new_diagnostic_ops(self, gateway) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            mcp = MCPClient(gateway["url"])
            tools = await mcp.list_tools(client)
            assert {
                "ops_catalog",
                "ops_operation_run",
                "ops_release_apply",
                "ops_release_rollback",
            } <= set(tools)

            result = await mcp.call(client, "ops_catalog", {})
            assert not result["is_error"]
            ops = {o["name"] for o in result["data"]["operations"]}
            assert {"config_validate", "check_project_config", "config_read"} <= ops
            assert "release_preflight" not in ops  # internals stay hidden

    async def test_internal_operation_stays_forbidden(self, gateway) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            mcp = MCPClient(gateway["url"])
            result = await mcp.call(
                client,
                "ops_operation_run",
                {
                    "operation": "release_preflight",
                    "app": "demo",
                    "environment": "staging",
                },
            )
            assert result["is_error"]
            assert result["code"] == "FORBIDDEN_OPERATION"

    async def test_maintenance_blocks_writes_reads_pass(self, gateway) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            mcp = MCPClient(gateway["url"])

            ok = await mcp.call(
                client,
                "ops_service_restart",
                {
                    "app": "demo",
                    "environment": "staging",
                    "service": "api",
                    "reason": "functional test restart",
                    "idempotency_key": "func-restart-000001",
                },
            )
            assert not ok["is_error"], ok
            assert ok["status"] == "queued"

            # Admission left an append-only audit event.
            assert await _event_count(gateway["db"], "job_admitted") >= 1

            # Runtime toggle through the control record: writes rejected.
            await _set_maintenance(gateway["db"], True)
            blocked = await mcp.call(
                client,
                "ops_service_restart",
                {
                    "app": "demo",
                    "environment": "staging",
                    "service": "api",
                    "reason": "should be blocked",
                    "idempotency_key": "func-restart-000002",
                },
            )
            assert blocked["is_error"]
            assert blocked["code"] == "MAINTENANCE"

            # Reads are never maintenance-blocked: the diagnostic job is
            # admitted (its execution result on a dev host is irrelevant here).
            read = await mcp.call(
                client,
                "ops_operation_run",
                {"operation": "git_status", "app": "demo", "environment": "staging"},
            )
            assert not (
                read["is_error"] and read.get("code") == "MAINTENANCE"
            ), read

            await _set_maintenance(gateway["db"], False)

    async def test_needs_attention_blocks_target_mutations(self, gateway) -> None:
        await _insert_blocking_job(gateway["db"], "needs_attention")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                mcp = MCPClient(gateway["url"])
                blocked = await mcp.call(
                    client,
                    "ops_service_restart",
                    {
                        "app": "demo",
                        "environment": "staging",
                        "service": "api",
                        "reason": "blocked by needs_attention",
                        "idempotency_key": "func-restart-000003",
                    },
                )
                assert blocked["is_error"]
                assert blocked["code"] == "NEEDS_ATTENTION"

                # Reads are never blocked.
                read = await mcp.call(
                    client,
                    "ops_operation_run",
                    {
                        "operation": "config_read",
                        "app": "demo",
                        "environment": "staging",
                        "parameters": {"file": "app_config"},
                    },
                )
                assert not (
                    read["is_error"] and read.get("code") == "NEEDS_ATTENTION"
                ), read
        finally:
            await _remove_blocking_jobs(gateway["db"])

    async def test_ops_logs_rejects_coercible_arguments(self, gateway) -> None:
        async with httpx.AsyncClient(timeout=40) as client:
            mcp = MCPClient(gateway["url"])
            result = await mcp.call(
                client,
                "ops_logs",
                {
                    "app": "demo",
                    "environment": "staging",
                    "limit": "50",  # string that would coerce — must be rejected
                },
            )
            assert result["is_error"]
            assert result["code"] == "INVALID_PARAMETER"

            bad_service = await mcp.call(
                client,
                "ops_logs",
                {"app": "demo", "environment": "staging", "service": "ghost"},
            )
            assert bad_service["is_error"]
            assert bad_service["code"] == "INVALID_PARAMETER"
