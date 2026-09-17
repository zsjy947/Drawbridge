"""ProcessManager integration tests with real subprocesses.

These run on every dev platform (the executor itself is cross-platform);
process-group semantics are additionally asserted on Linux.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from tests.conftest import make_spec, pid_alive

from drawbridge.config.models import OutputPolicyKind
from drawbridge.executor.process import ProcessManager
from drawbridge.executor.spec import (
    TERMINATION_COMPLETED,
    TERMINATION_OUTPUT_LIMIT,
    TERMINATION_START_ERROR,
    TERMINATION_TIMEOUT,
)

PY = sys.executable


def py_script(code: str) -> tuple[str, ...]:
    return ("-c", code)


class TestNormalExecution:
    async def test_exit_zero_accepted(self) -> None:
        pm = ProcessManager()
        result = await pm.execute(make_spec(py_script("print('hello')")))
        assert result.termination_reason == TERMINATION_COMPLETED
        assert result.exit_code == 0
        assert result.accepted
        assert "hello" in result.stdout_preview

    async def test_nonzero_exit_not_accepted(self) -> None:
        pm = ProcessManager()
        result = await pm.execute(make_spec(py_script("import sys; sys.exit(3)")))
        assert result.exit_code == 3
        assert not result.accepted
        assert result.termination_reason == TERMINATION_COMPLETED

    async def test_nonzero_accepted_exit_codes(self) -> None:
        pm = ProcessManager()
        spec = make_spec(
            py_script("import sys; sys.exit(2)"), accepted=frozenset({0, 2})
        )
        result = await pm.execute(spec)
        assert result.accepted

    async def test_stderr_accounted(self) -> None:
        pm = ProcessManager()
        result = await pm.execute(
            make_spec(py_script("import sys; sys.stderr.write('boom')"))
        )
        assert "boom" in result.stderr_preview
        assert result.stderr_bytes == 4

    async def test_invalid_utf8_tolerated(self) -> None:
        pm = ProcessManager()
        code = "import sys; sys.stdout.buffer.write(b'\\xff\\xfe\\x80ok')"
        result = await pm.execute(make_spec(py_script(code)))
        assert result.stdout_bytes == 5
        assert "ok" in result.stdout_preview

    async def test_environment_isolation(self) -> None:
        os.environ["BASH_ENV"] = "/tmp/evil"
        os.environ["DRAWBRIDGE_LEAK_PROBE"] = "leaked"
        try:
            pm = ProcessManager()
            code = (
                "import os, json;"
                "print(json.dumps({'b': os.environ.get('BASH_ENV'),"
                " 'l': os.environ.get('DRAWBRIDGE_LEAK_PROBE'),"
                " 'lang': os.environ.get('LANG')}))"
            )
            result = await pm.execute(make_spec(py_script(code)))
            assert result.accepted
            import json

            payload = json.loads(result.stdout_preview)
            assert payload["b"] is None
            assert payload["l"] is None
            assert payload["lang"] == "C.UTF-8"
        finally:
            os.environ.pop("BASH_ENV", None)
            os.environ.pop("DRAWBRIDGE_LEAK_PROBE", None)

    async def test_start_error(self) -> None:
        pm = ProcessManager()
        result = await pm.execute(
            make_spec(
                executable="/nonexistent/drawbridge-no-such-binary",
                argv=("x",),
            )
        )
        assert result.termination_reason == TERMINATION_START_ERROR
        assert not result.accepted
        assert result.exit_code is None


class TestTimeout:
    async def test_timeout_kills_process(self) -> None:
        pm = ProcessManager(term_grace_seconds=1.0)
        started = time.monotonic()
        result = await pm.execute(
            make_spec(py_script("import time; time.sleep(60)"), timeout=1.0)
        )
        elapsed = time.monotonic() - started
        assert result.termination_reason == TERMINATION_TIMEOUT
        assert not result.accepted
        assert elapsed < 15

    async def test_timeout_reaps_child_tree(self, tmp_path: Path) -> None:
        """The spawned grandchild must be gone after the group kill."""
        mid_pid_file = tmp_path / "mid.pid"
        leaf_pid_file = tmp_path / "leaf.pid"

        leaf_script = tmp_path / "leaf.py"
        leaf_script.write_text(
            "import os, time\n"
            f"open({str(leaf_pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )
        mid_script = tmp_path / "mid.py"
        mid_script.write_text(
            "import subprocess, sys, os, time\n"
            f"leaf = subprocess.Popen([sys.executable, {str(leaf_script)!r}])\n"
            f"open({str(mid_pid_file)!r}, 'w').write(str(leaf.pid))\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )
        pm = ProcessManager(term_grace_seconds=1.0)
        result = await pm.execute(
            make_spec((str(mid_script),), timeout=1.5)
        )
        assert result.termination_reason == TERMINATION_TIMEOUT

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            mid = (
                int(mid_pid_file.read_text())
                if mid_pid_file.exists() and mid_pid_file.read_text().strip()
                else None
            )
            leaf = (
                int(leaf_pid_file.read_text())
                if leaf_pid_file.exists() and leaf_pid_file.read_text().strip()
                else None
            )
            if mid is not None and leaf is not None:
                if not pid_alive(mid) and not pid_alive(leaf):
                    return
            await asyncio.sleep(0.2)
        pytest.fail("child tree survived the group termination")


class TestOutputLimits:
    async def test_terminate_policy_overrun(self) -> None:
        pm = ProcessManager()
        code = "print('x' * 1000000)"
        result = await pm.execute(
            make_spec(py_script(code), max_output_bytes=4096)
        )
        assert result.termination_reason == TERMINATION_OUTPUT_LIMIT
        assert result.truncated
        assert result.stdout_bytes <= 4096 + 65536 + 1024  # chunk slack

    async def test_summary_bounded(self) -> None:
        pm = ProcessManager()
        # 2 x 40 KiB: total exceeds the 64 KiB summary, so the preview must
        # be capped at head half + marker + tail half.
        code = "print('A' * 40000); print('B' * 40000)"
        result = await pm.execute(make_spec(py_script(code)))
        assert len(result.stdout_preview) <= 64 * 1024 + len("\n[...]\n") + 4
        assert result.stdout_preview.startswith("A")
        assert result.stdout_preview.rstrip().endswith("B")

    async def test_stderr_flood_also_limited(self) -> None:
        pm = ProcessManager()
        code = "import sys; sys.stderr.write('e' * 1000000)"
        result = await pm.execute(
            make_spec(py_script(code), max_output_bytes=4096)
        )
        assert result.termination_reason == TERMINATION_OUTPUT_LIMIT

    async def test_spool_policy_writes_log_and_enforces_hard_budget(
        self, tmp_path: Path
    ) -> None:
        pm = ProcessManager(term_grace_seconds=1.0)
        log_path = tmp_path / "job.log"
        code = "print('x' * 200000)"
        result = await pm.execute(
            make_spec(
                py_script(code),
                policy=OutputPolicyKind.SPOOL,
                max_output_bytes=65536,
                hard_limit=100_000,
                log_path=str(log_path),
            )
        )
        assert result.termination_reason == TERMINATION_OUTPUT_LIMIT
        assert result.log_path == str(log_path)
        assert log_path.exists()
        assert log_path.stat().st_size <= 100_000 + 65536 + 8192

    async def test_spool_policy_under_limit_keeps_going(self, tmp_path: Path) -> None:
        pm = ProcessManager()
        log_path = tmp_path / "job.log"
        result = await pm.execute(
            make_spec(
                py_script("print('done')"),
                policy=OutputPolicyKind.SPOOL,
                max_output_bytes=65536,
                hard_limit=1_000_000,
                log_path=str(log_path),
            )
        )
        assert result.termination_reason == TERMINATION_COMPLETED
        assert result.accepted
        assert log_path.read_text().strip() == "done"


class TestStdin:
    async def test_stdin_passed_and_closed(self) -> None:
        pm = ProcessManager()
        code = "import sys; data = sys.stdin.read(); print('got:' + data)"
        result = await pm.execute(
            make_spec(py_script(code), stdin_data=b"hello-stdin")
        )
        assert "got:hello-stdin" in result.stdout_preview

    async def test_stdin_default_closed(self) -> None:
        pm = ProcessManager()
        # stdin=DEVNULL: reads return EOF immediately, no data ever arrives.
        code = "import sys; print('stdin_data' if sys.stdin.read() else 'stdin_empty')"
        result = await pm.execute(make_spec(py_script(code)))
        assert result.accepted
        assert "stdin_empty" in result.stdout_preview


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
class TestPosixProcessGroup:
    async def test_child_in_own_session(self) -> None:
        pm = ProcessManager()
        code = "import os; print(os.getsid(0) == os.getpid())"
        result = await pm.execute(make_spec(py_script(code)))
        assert result.stdout_preview.strip() == "True"
