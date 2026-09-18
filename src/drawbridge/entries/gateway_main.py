"""drawbridge-gateway entry point.

Serves the Streamable HTTP MCP endpoint behind the edge middleware
(IP allowlist, optional bearer token, Origin/Host checks).  The Gateway
never touches Docker sockets, Git credentials or deployment secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from drawbridge.config.loader import load_config_from_dir
from drawbridge.gateway.mcp_app import MCPAppFactory
from drawbridge.gateway.middleware import EdgeMiddleware, load_token_hash
from drawbridge.gateway.service import GatewayService
from drawbridge.logsetup import configure_logging, get_logger
from drawbridge.state.db import Database
from drawbridge.state.store import Store


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drawbridge-gateway")
    parser.add_argument(
        "--config-dir",
        default="/etc/drawbridge",
        help="directory containing drawbridge/apps/operations/workflows YAML",
    )
    parser.add_argument("--dev", action="store_true", help="dev logging, local defaults")
    return parser


async def sync_maintenance_from_config(database: Any, config: Any) -> str:
    """Persist ``maintenance.enabled`` into the control record (tech design §12).

    Restarting the Gateway is the documented toggle: the config value is the
    authority at startup, the Runner re-reads the control record before every
    dispatch, and an unreadable record stops dispatch entirely (fail closed).
    """
    value = "true" if config.main.maintenance.enabled else "false"
    await database.set_control("maintenance", value)
    return value


async def serve(config_dir: str, *, dev: bool = False) -> int:
    config = load_config_from_dir(config_dir)
    configure_logging(dev_mode=dev)
    log = get_logger("drawbridge.gateway")

    database = Database(f"{config.main.paths.state_dir}/state.db")
    await database.connect()
    await database.initialize()
    maintenance = await sync_maintenance_from_config(database, config)
    if maintenance == "true":
        log.warning("maintenance mode is ENABLED; writes are rejected")
    store = Store(database)
    service = GatewayService(config, store)
    factory = MCPAppFactory(service)
    asgi_app = factory.build_asgi_app()

    token_hash = None
    if config.main.server.auth.mode == "token":
        if not config.main.server.auth.token_file:
            log.error("auth.mode=token requires auth.token_file")
            return 2
        token_hash = load_token_hash(config.main.server.auth.token_file)
        if token_hash is None:
            log.error("cannot read token file", file=config.main.server.auth.token_file)
            return 2

    edge = EdgeMiddleware(asgi_app, config.main.server, token_hash)

    import uvicorn

    uv_config = uvicorn.Config(
        edge,
        host=config.main.server.bind_address,
        port=config.main.server.port,
        log_config=None,
        # Never trust proxy headers: the allowlist judges the socket peer.
        proxy_headers=False,
        forwarded_allow_ips=[],
        server_header=False,
        date_header=False,
        lifespan="on",
    )
    server = uvicorn.Server(uv_config)
    log.info(
        "gateway listening",
        bind=config.main.server.bind_address,
        port=config.main.server.port,
        auth=config.main.server.auth.mode,
        config_digest=config.digest[:12],
    )
    await server.serve()
    await database.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return asyncio.run(serve(args.config_dir, dev=args.dev))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"gateway failed to start: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
