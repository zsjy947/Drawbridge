"""Gateway edge middleware (tech design §8).

ASGI-pure, no trust in proxy headers:

* client source IP must fall inside one of the configured CIDRs (default
  192.168.0.0/16) — the address is taken from the ASGI scope, never from
  X-Forwarded-For; uvicorn must run with ``proxy_headers=False``;
* optional bearer token compared in constant time; audit sees only a hash;
* Origin (when present) and Host are checked against explicit allow lists;
* everything outside the rules is answered with a bare 403, no body echo.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from collections.abc import Awaitable, Callable
from typing import Any

from drawbridge.config.models import ServerConfig

ASGIScope = dict[str, Any]
ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]

_FORBIDDEN_BODY = b'{"error":"forbidden"}'


class GatewayDeny(Exception):
    pass


class EdgeMiddleware:
    """Rejects disallowed peers before MCP routing sees the request."""

    def __init__(self, app: ASGIApp, server: ServerConfig, token_sha256: str | None) -> None:
        self.app = app
        self.networks = [ipaddress.ip_network(c, strict=False) for c in server.allowed_cidrs]
        self.server = server
        self.token_sha256 = token_sha256
        self.allowed_origins = {o.lower() for o in server.allowed_origins}
        self.allowed_hosts = {h.lower() for h in server.allowed_hosts}

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = _header_map(scope)
        peer = scope.get("client")
        peer_ip = peer[0] if peer else ""

        if not self._ip_allowed(peer_ip):
            await _forbidden(send)
            return
        if not self._host_allowed(headers.get("host", "")):
            await _forbidden(send)
            return
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in self.allowed_origins:
            await _forbidden(send)
            return
        if self.token_sha256 is not None and not self._token_ok(headers.get("authorization", "")):
            await _forbidden(send)
            return
        await self.app(scope, receive, send)

    def _ip_allowed(self, peer_ip: str) -> bool:
        try:
            address = ipaddress.ip_address(peer_ip)
        except ValueError:
            return False
        return any(address in net for net in self.networks)

    def _host_allowed(self, host: str) -> bool:
        host = host.strip().lower()
        if not host:
            return False
        # Entries may be given with or without the port; match either form.
        hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return host in self.allowed_hosts or hostname in self.allowed_hosts

    def _token_ok(self, authorization: str) -> bool:
        if not authorization.startswith("Bearer "):
            return False
        presented = authorization[len("Bearer "):].strip()
        if not presented:
            return False
        presented_sha = hashlib.sha256(presented.encode("utf-8")).digest()
        expected_sha = bytes.fromhex(self.token_sha256)  # type: ignore[arg-type]
        return hmac.compare_digest(presented_sha, expected_sha)


def token_sha256(token: str) -> str:
    """Audit/config store only this hash of the bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_token_hash(token_file: str) -> str | None:
    """Read the token from the admin-controlled file (mode 600 expected)."""
    from pathlib import Path

    path = Path(token_file)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not token:
        return None
    return token_sha256(token)


def _header_map(scope: ASGIScope) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1").lower()
        if name not in result:
            result[name] = raw_value.decode("latin-1")
    return result


async def _forbidden(send: ASGISend) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_FORBIDDEN_BODY)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _FORBIDDEN_BODY})
