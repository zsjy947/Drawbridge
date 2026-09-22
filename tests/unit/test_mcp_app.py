"""Unit tests for the MCP transport security mirroring (mcp_app)."""

from drawbridge.config.models import ServerConfig
from drawbridge.gateway.mcp_app import _transport_security


def test_transport_security_mirrors_edge_allowlist() -> None:
    server = ServerConfig(
        allowed_origins=["http://demo.example:8787", "http://admin.example"],
        allowed_hosts=["Demo.Example:8787", "10.0.0.1:8787"],
    )
    settings = _transport_security(server)
    assert settings.enable_dns_rebinding_protection is True
    # Host headers are case-insensitive; the edge middleware lowercases its
    # own allowlist, so the SDK layer must match that normalization.
    assert settings.allowed_hosts == ["demo.example:8787", "10.0.0.1:8787"]
    assert settings.allowed_origins == ["http://demo.example:8787", "http://admin.example"]
