"""Edge middleware and end-to-end HTTP gateway tests.

The end-to-end test starts a real uvicorn server on loopback and speaks
MCP Streamable HTTP to it — this is the connectivity smoke test that can
run on any dev host without Docker.
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
from drawbridge.config.models import ServerConfig
from drawbridge.gateway.middleware import EdgeMiddleware

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"


def make_server_config(**overrides) -> ServerConfig:
    data: dict = {
        "allowed_cidrs": ["127.0.0.0/8", "::1/128", "192.168.0.0/16"],
        "allowed_origins": ["http://localhost:8787"],
        "allowed_hosts": ["testserver", "127.0.0.1:8787", "localhost:8787"],
    }
    data.update(overrides)
    return ServerConfig.model_validate(data)


class TestEdgeMiddleware:
    def make_capturing_app(self) -> tuple:
        captured: dict = {}

        async def app(scope, receive, send):
            captured["scope"] = scope

            async def send_ok(message):
                pass

            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        return app, captured

    async def call(self, middleware: EdgeMiddleware, *, client=("127.0.0.1", 1), headers=None):
        messages: list[dict] = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            messages.append(message)

        merged_headers = {"Host": "testserver"}
        merged_headers.update(headers or {})
        scope = {
            "type": "http",
            "client": client,
            "headers": [
                (k.lower().encode(), v.encode())
                for k, v in merged_headers.items()
            ],
            "path": "/mcp",
        }
        await middleware(scope, receive, send)
        return messages

    async def test_ip_in_allowlist_passes(self) -> None:
        app, _captured = self.make_capturing_app()
        mw = EdgeMiddleware(app, make_server_config(), None)
        messages = await self.call(mw, client=("192.168.1.10", 5))
        assert messages[0]["status"] == 200

    async def test_ip_outside_allowlist_403(self) -> None:
        app, _ = self.make_capturing_app()
        mw = EdgeMiddleware(app, make_server_config(), None)
        messages = await self.call(mw, client=("10.1.2.3", 5))
        assert messages[0]["status"] == 403

    async def test_proxy_headers_are_ignored(self) -> None:
        app, _ = self.make_capturing_app()
        mw = EdgeMiddleware(app, make_server_config(), None)
        # spoofed X-Forwarded-For must not rescue a bad peer address
        messages = await self.call(
            mw,
            client=("10.1.2.3", 5),
            headers={"X-Forwarded-For": "192.168.1.10"},
        )
        assert messages[0]["status"] == 403

    async def test_host_allowlist(self) -> None:
        app, _ = self.make_capturing_app()
        mw = EdgeMiddleware(app, make_server_config(), None)
        ok = await self.call(mw, headers={"Host": "127.0.0.1:8787"})
        assert ok[0]["status"] == 200
        bad = await self.call(mw, headers={"Host": "evil.example"})
        assert bad[0]["status"] == 403

    async def test_origin_allowlist(self) -> None:
        app, _ = self.make_capturing_app()
        mw = EdgeMiddleware(app, make_server_config(), None)
        ok = await self.call(mw, headers={"Origin": "http://localhost:8787"})
        assert ok[0]["status"] == 200
        bad = await self.call(mw, headers={"Origin": "http://evil.example"})
        assert bad[0]["status"] == 403

    async def test_bearer_token_constant_time_path(self) -> None:
        import hashlib

        token = "drawbridge-token-123"
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        app, _ = self.make_capturing_app()
        mw = EdgeMiddleware(app, make_server_config(), token_hash)
        good = await self.call(
            mw, headers={"Authorization": f"Bearer {token}"}
        )
        assert good[0]["status"] == 200
        missing = await self.call(mw)
        assert missing[0]["status"] == 403
        wrong = await self.call(mw, headers={"Authorization": "Bearer nope-nope"})
        assert wrong[0]["status"] == 403


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
async def live_gateway(tmp_path: Path):
    """Full gateway stack (edge middleware + MCP app) on loopback."""
    config = load_config_from_dir(CONFIG_DIR)
    config.main.paths.state_dir = str(tmp_path / "state")
    config.main.paths.lock_dir = str(tmp_path / "locks")
    config.main.paths.log_dir = str(tmp_path / "logs")

    import uvicorn

    port = free_port()
    config.main.server.allowed_cidrs = ["127.0.0.0/8"]
    config.main.server.allowed_hosts = [f"127.0.0.1:{port}"]

    from drawbridge.gateway.mcp_app import MCPAppFactory
    from drawbridge.gateway.service import GatewayService
    from drawbridge.state.db import Database
    from drawbridge.state.store import Store

    database = Database(tmp_path / "state" / "state.db")
    await database.connect()
    await database.initialize()
    store = Store(database)
    service = GatewayService(config, store)
    factory = MCPAppFactory(service)
    asgi_app = factory.build_asgi_app()
    edge = EdgeMiddleware(asgi_app, config.main.server, None)

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
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=10)
    await database.close()


class TestLiveGatewayHTTP:
    async def test_mcp_initialize_over_streamable_http(self, live_gateway) -> None:
        headers = {
            "Host": f"127.0.0.1:{live_gateway.split(':')[-1].split('/')[0]}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "drawbridge-test", "version": "0.0.1"},
            },
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                live_gateway, json=payload, headers=headers
            )
            assert response.status_code == 200, response.text
            body = response.text
            # JSON or SSE framing both carry the same JSON-RPC result
            if body.startswith("event:") or "\ndata:" in body:
                data_lines = [
                    line[5:].strip()
                    for line in body.splitlines()
                    if line.startswith("data:")
                ]
                body = data_lines[0]
            result = json.loads(body)
            assert result["result"]["serverInfo"]["name"] == "drawbridge"
            tools = await client.post(
                live_gateway,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                },
                headers=headers,
            )
            assert tools.status_code == 200
            tools_body = tools.text
            if tools_body.startswith("event:") or "\ndata:" in tools_body:
                data_lines = [
                    line[5:].strip()
                    for line in tools_body.splitlines()
                    if line.startswith("data:")
                ]
                tools_body = data_lines[0]
            parsed = json.loads(tools_body)
            names = {t["name"] for t in parsed["result"]["tools"]}
            assert {
                "ops_catalog",
                "ops_release_plan",
                "ops_release_apply",
                "ops_operation_run",
            } <= names

    async def test_tools_call_unknown_operation_is_tool_error(
        self, live_gateway
    ) -> None:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "drawbridge-test", "version": "0.0.1"},
            },
        }
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "ops_operation_run",
                "arguments": {
                    "operation": "release_preflight",  # internal → forbidden
                    "app": "demo",
                    "environment": "staging",
                },
            },
        }
        async with httpx.AsyncClient(timeout=20) as client:
            await client.post(live_gateway, json=init, headers=headers)
            response = await client.post(live_gateway, json=call, headers=headers)
            assert response.status_code == 200
            body = response.text
            if body.startswith("event:") or "\ndata:" in body:
                data_lines = [
                    line[5:].strip()
                    for line in body.splitlines()
                    if line.startswith("data:")
                ]
                body = data_lines[0]
            parsed = json.loads(body)
            result = parsed["result"]
            assert result.get("isError") is True
            text = result["content"][0]["text"]
            assert "FORBIDDEN_OPERATION" in text
