"""Fixed HTTP health gates shared by deploy steps and service restart.

Implements the MVP spec §8 default policy: single probe timeout,
fixed interval, N consecutive successes inside the configured budget,
no redirects, no TLS.  A health gate distinguishes "container running"
(compose --wait) from "application ready" (this probe).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from http.client import HTTPConnection
from typing import Any
from urllib.parse import urlparse

from drawbridge.config.models import HealthCheckConfig
from drawbridge.errors import DrawbridgeError, ErrorCode


def _probe_once(parsed: Any, hc: HealthCheckConfig) -> int:
    conn = HTTPConnection(
        parsed.hostname, parsed.port or 80, timeout=hc.single_timeout_seconds
    )
    try:
        conn.request("GET", parsed.path or "/", headers={"Host": parsed.netloc})
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


async def probe_health_checks(
    health_checks: Sequence[HealthCheckConfig],
) -> list[dict[str, Any]]:
    """Run every registered gate; returns per-gate evidence dicts."""
    results: list[dict[str, Any]] = []
    for hc in health_checks:
        parsed = urlparse(hc.url)
        if parsed.scheme != "http":
            raise DrawbridgeError(
                f"health check URL must be http (no TLS in MVP): {hc.url!r}",
                code=ErrorCode.CONFIG_INVALID,
            )

        deadline = time.monotonic() + hc.timeout_seconds
        consecutive = 0
        last_status: int | None = None
        while time.monotonic() < deadline:
            try:
                last_status = await asyncio.to_thread(_probe_once, parsed, hc)
            except OSError:
                last_status = None
            if last_status == hc.expected_status:
                consecutive += 1
                if consecutive >= hc.consecutive_successes:
                    break
            else:
                consecutive = 0
            await asyncio.sleep(hc.interval_seconds)
        results.append(
            {
                "url": hc.url,
                "passed": consecutive >= hc.consecutive_successes,
                "last_status": last_status,
                "consecutive_successes": consecutive,
            }
        )
    return results


def require_healthy(results: Sequence[dict[str, Any]]) -> None:
    """Raise VERIFY_FAILED when any gate did not pass."""
    failed = [r["url"] for r in results if not r.get("passed")]
    if failed:
        raise DrawbridgeError(
            f"health gate failed: {failed}", code=ErrorCode.VERIFY_FAILED
        )
