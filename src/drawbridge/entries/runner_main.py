"""drawbridge-runner entry point.

Single-instance execution principal: claims jobs from the shared SQLite
queue and performs every controlled Git / Docker / build / test action.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from drawbridge.config.loader import load_config_from_dir
from drawbridge.logsetup import configure_logging, get_logger
from drawbridge.runner.loop import Runner
from drawbridge.state.db import Database
from drawbridge.state.store import Store


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drawbridge-runner")
    parser.add_argument("--config-dir", default="/etc/drawbridge")
    parser.add_argument("--dev", action="store_true")
    parser.add_argument(
        "--poll-interval", type=float, default=0.2, help="queue poll interval (s)"
    )
    return parser


def acquire_instance_lock(lock_path: Path) -> object | None:
    """Global process lock against duplicate Runners (no-op on Windows dev)."""
    import os

    if _RUNTIME_WINDOWS:
        return object()  # dev hosts: single-instance enforced by convention
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o660)
    try:
        fcntl_module = __import__("fcntl").flock
        fcntl_module(fd, 2 | 6)  # LOCK_EX | LOCK_NB
    except OSError:
        os.close(fd)
        return None
    return fd  # kept open for the process lifetime


_RUNTIME_WINDOWS = sys.platform == "win32"


async def run(config_dir: str, *, dev: bool, poll_interval: float) -> int:
    config = load_config_from_dir(config_dir)
    configure_logging(dev_mode=dev)
    log = get_logger("drawbridge.runner")

    lock_handle = acquire_instance_lock(Path(config.main.paths.lock_dir) / "runner.lock")
    if lock_handle is None:
        log.error("another runner instance already holds the lock")
        return 3

    database = Database(f"{config.main.paths.state_dir}/state.db")
    await database.connect()
    await database.initialize()
    store = Store(database)
    runner = Runner(config, store, poll_interval=poll_interval)
    log.info(
        "runner consuming queue",
        instance=runner.instance_id,
        config_digest=config.digest[:12],
    )
    try:
        await runner.run_forever()
    finally:
        await database.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return asyncio.run(run(args.config_dir, dev=args.dev, poll_interval=args.poll_interval))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"runner failed to start: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
