"""Asynchronous process executor with bounded output and group cleanup.

Design constraints (tech design §10, §13 and MVP spec §3):

* ``asyncio.create_subprocess_exec`` only — never a shell;
* stdout/stderr are drained concurrently and byte-accounted as they are
  read, so a process flooding both pipes cannot complete ``communicate()``
  before limits are enforced;
* output policies: ``terminate`` (diagnostics — over budget kills the
  process) and ``spool`` (change steps — raw output streams to a protected
  log file with a hard per-step budget; summaries stay bounded);
* the child starts in its own process group / process tree; termination is
  TERM → grace → KILL → reclaim of the whole group, covering timeouts,
  output overruns, coroutine cancellation and start-up failures;
* UTF-8 is decoded tolerantly for previews only; budgets count raw bytes.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from typing import BinaryIO

from drawbridge.executor.spec import (
    TERMINATION_COMPLETED,
    TERMINATION_OUTPUT_LIMIT,
    TERMINATION_START_ERROR,
    TERMINATION_TIMEOUT,
    ExecutionResult,
    ExecutionSpec,
)
from drawbridge.logsetup import get_logger

_CHUNK = 65536
_POSIX = sys.platform != "win32"

log = get_logger(__name__)


class _StreamLedger:
    """Shared byte accounting, per-stream summaries and optional spool."""

    def __init__(self, spec: ExecutionSpec) -> None:
        self.spec = spec
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.total_bytes = 0
        self.limit_exceeded = False
        self._summary_half = max(spec.summary_bytes // 2, 1)
        self._stdout_head: bytearray = bytearray()
        self._stdout_tail: bytearray = bytearray()
        self._stderr_head: bytearray = bytearray()
        self._stderr_tail: bytearray = bytearray()
        self._spool_fh: BinaryIO | None = None
        self._spool_error: str | None = None
        if spec.output_policy.value == "spool" and spec.log_path:
            parent = os.path.dirname(spec.log_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._spool_fh = open(spec.log_path, "ab", buffering=0)  # noqa: SIM115

    def record(self, stream_name: str, chunk: bytes) -> None:
        if stream_name == "stdout":
            self.stdout_bytes += len(chunk)
            head, tail = self._stdout_head, self._stdout_tail
        else:
            self.stderr_bytes += len(chunk)
            head, tail = self._stderr_head, self._stderr_tail
        self.total_bytes += len(chunk)

        limit = (
            self.spec.hard_output_limit
            if self.spec.output_policy.value == "spool"
            else self.spec.max_output_bytes
        )
        overflow = self.total_bytes - limit
        if overflow >= len(chunk):
            # Entire chunk beyond the budget: accounted, never kept.
            self.limit_exceeded = True
            return
        if overflow > 0:
            self.limit_exceeded = True
            # Keep the deterministic prefix that still fits — chunk sizes
            # vary by platform, the summary must not depend on them.
            chunk = chunk[: len(chunk) - overflow]

        _append_ring(head, self._summary_half, chunk)
        _append_ring(tail, self._summary_half, chunk)

        if self._spool_fh is not None:
            try:
                self._spool_fh.write(chunk)
            except OSError as exc:
                self._spool_error = f"spool write failed: {exc}"
                self.limit_exceeded = True

    @property
    def spooling(self) -> bool:
        return self._spool_fh is not None

    def close(self) -> None:
        if self._spool_fh is not None:
            try:
                self._spool_fh.close()
            finally:
                self._spool_fh = None

    def _decode(self, head: bytearray, tail: bytearray, stream_bytes: int) -> str:
        # Small outputs are fully contained in `head`; the tail ring would
        # only duplicate them.
        if stream_bytes <= self._summary_half or not tail:
            data = bytes(head)
        else:
            data = bytes(head) + b"\n[...]\n" + bytes(tail)
        return data.decode("utf-8", errors="replace")

    def stdout_preview(self) -> str:
        return self._decode(self._stdout_head, self._stdout_tail, self.stdout_bytes)

    def stderr_preview(self) -> str:
        return self._decode(self._stderr_head, self._stderr_tail, self.stderr_bytes)


def _append_ring(ring: bytearray, half: int, chunk: bytes) -> None:
    if half <= 0:
        return
    if len(chunk) >= half:
        ring[:] = chunk[-half:]
        return
    ring.extend(chunk)
    overflow = len(ring) - half
    if overflow > 0:
        del ring[:overflow]


async def _drain_stream(
    stream: asyncio.StreamReader | None, name: str, ledger: _StreamLedger
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(_CHUNK)
        if not chunk:
            return
        ledger.record(name, chunk)
        if ledger.limit_exceeded:
            # Stop reading; the supervisor terminates the process group.
            return


def _terminate_group_posix(pid: int, sig: int) -> None:
    """TERM/KILL a whole process group; getattr keeps Windows type checks happy."""
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    kill = getattr(os, "kill", None)
    if killpg is None or getpgid is None or kill is None:  # pragma: no cover
        return
    try:
        killpg(getpgid(pid), sig)
        return
    except (ProcessLookupError, PermissionError):
        pass
    # Already gone or already re-parented; direct kill as fallback.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        kill(pid, sig)


async def _terminate_tree_windows(pid: int) -> None:
    """Dev/test fallback: kill the whole process tree via taskkill."""
    process = await asyncio.create_subprocess_exec(
        "taskkill",
        "/F",
        "/T",
        "/PID",
        str(pid),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await process.wait()
    _close_transport(process)


def _close_transport(proc: asyncio.subprocess.Process) -> None:
    """Release a subprocess transport deterministically.

    On Windows dev hosts the Proactor transports otherwise get collected
    after the event loop closes and surface as unraisable 'Event loop is
    closed' warnings at interpreter shutdown (plan D11 test hygiene).
    """
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        with contextlib.suppress(OSError, RuntimeError):
            transport.close()


class ProcessManager:
    """Executes :class:`ExecutionSpec` instances; injectable in tests."""

    def __init__(self, *, term_grace_seconds: float = 5.0) -> None:
        self._term_grace = term_grace_seconds

    async def execute(self, spec: ExecutionSpec) -> ExecutionResult:
        started = time.monotonic()
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            proc = await asyncio.create_subprocess_exec(
                spec.executable,
                *spec.argv,
                stdin=subprocess.PIPE if spec.stdin_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=spec.cwd or None,
                env=dict(spec.env),
                start_new_session=_POSIX,
                creationflags=creationflags,
            )
        except OSError as exc:
            return spec.result(
                exit_code=None,
                termination_reason=TERMINATION_START_ERROR,
                duration_ms=_elapsed_ms(started),
                stdout_bytes=0,
                stderr_bytes=0,
                truncated=False,
                stdout_preview="",
                stderr_preview="",
                log_path=spec.log_path,
                start_error=f"{type(exc).__name__}: {exc}",
            )

        ledger = _StreamLedger(spec)
        stdin_task: asyncio.Task[None] | None = None
        limit_event = asyncio.Event()
        limit_task: asyncio.Task[bool] | None = None
        try:
            if spec.stdin_data is not None and proc.stdin is not None:
                stdin_task = asyncio.create_task(_write_stdin(proc, spec))

            async def drain_and_signal(
                stream: asyncio.StreamReader | None, name: str
            ) -> None:
                await _drain_stream(stream, name, ledger)
                if ledger.limit_exceeded:
                    limit_event.set()

            readers = [
                asyncio.create_task(drain_and_signal(proc.stdout, "stdout")),
                asyncio.create_task(drain_and_signal(proc.stderr, "stderr")),
            ]
            exit_task: asyncio.Task[int | None] = asyncio.create_task(proc.wait())
            limit_task = asyncio.create_task(limit_event.wait())
            done, _pending = await asyncio.wait(
                {exit_task, limit_task},
                timeout=spec.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if exit_task in done:
                # Process exited on its own; an overrun still marks the result.
                reason = (
                    TERMINATION_OUTPUT_LIMIT if ledger.limit_exceeded else None
                )
            else:
                reason = (
                    TERMINATION_OUTPUT_LIMIT
                    if limit_task in done
                    else TERMINATION_TIMEOUT
                )
                await self._terminate_group(proc)
                try:
                    await asyncio.wait_for(asyncio.shield(exit_task), timeout=10.0)
                except TimeoutError:
                    log.error(
                        "process did not report exit after group termination",
                        operation=spec.operation,
                        pid=proc.pid,
                    )

            await asyncio.gather(*readers, return_exceptions=True)
            if stdin_task is not None:
                await asyncio.gather(stdin_task, return_exceptions=True)

            if reason is None:
                reason = TERMINATION_COMPLETED
            exit_code = exit_task.result() if exit_task.done() else None
            result = spec.result(
                exit_code=exit_code,
                termination_reason=reason,
                duration_ms=_elapsed_ms(started),
                stdout_bytes=ledger.stdout_bytes,
                stderr_bytes=ledger.stderr_bytes,
                truncated=ledger.limit_exceeded,
                stdout_preview=ledger.stdout_preview(),
                stderr_preview=ledger.stderr_preview(),
                log_path=spec.log_path if ledger.spooling else None,
                start_error=ledger._spool_error,
            )
            return result
        except asyncio.CancelledError:
            await self._terminate_group(proc, grace=min(self._term_grace, 1.0))
            raise
        finally:
            if limit_task is not None and not limit_task.done():
                limit_task.cancel()
            ledger.close()
            _close_transport(proc)

    async def _terminate_group(
        self, proc: asyncio.subprocess.Process, *, grace: float | None = None
    ) -> None:
        grace = self._term_grace if grace is None else grace
        if proc.returncode is not None:
            return
        if sys.platform == "win32":
            await _terminate_tree_windows(proc.pid)
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=grace)
            except TimeoutError:
                # Windows Proactor quirk: a child killed while blocked
                # writing into a full pipe never signals wait() although the
                # process is gone.  Closing the transport releases the
                # pending wait with the real return code.  Dev/test hosts
                # only — production kills the POSIX process group instead.
                transport = getattr(proc, "_transport", None)
                if transport is not None:
                    transport.close()
                try:
                    await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=2.0)
                except TimeoutError:
                    log.error("windows process wait unresolved after kill", pid=proc.pid)
        else:
            _terminate_group_posix(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=grace)
            except TimeoutError:
                _terminate_group_posix(proc.pid, getattr(signal, "SIGKILL", 9))
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            log.error("process.wait did not finish after kill", pid=proc.pid)


async def _write_stdin(proc: asyncio.subprocess.Process, spec: ExecutionSpec) -> None:
    assert spec.stdin_data is not None and proc.stdin is not None
    try:
        proc.stdin.write(spec.stdin_data)
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError):
            proc.stdin.close()


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
