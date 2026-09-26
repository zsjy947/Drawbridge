"""One-command local quality gate (plan D0, docs/VERIFICATION_RECORD.md).

Runs ruff / mypy / pytest in order against the same interpreter that started
this script, captures each tool's combined output under
``var/verification/<UTC timestamp>/`` and writes a machine-readable
``report.json`` (tool, exit code, duration, commit).  Any tool failing makes
the whole run exit non-zero.

Works unchanged on the Windows development host and on the 910B target
(``uv run scripts/verify.py``): it shells out only to ``sys.executable`` and
git, never to a platform-specific tool.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: (tool name, argv appended to sys.executable, output file name)
CHECKS: tuple[tuple[str, list[str], str], ...] = (
    ("ruff", ["-m", "ruff", "check", "src", "tests"], "ruff.log"),
    ("mypy", ["-m", "mypy"], "mypy.log"),
    ("pytest", ["-m", "pytest", "-q"], "pytest.log"),
)


def _commit() -> str:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, repo-local
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def run_verification() -> int:
    started = time.monotonic()
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = REPO_ROOT / "var" / "verification" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, object]] = []
    overall = 0
    for name, argv_tail, log_name in CHECKS:
        print(f"[verify] {name}: {' '.join(argv_tail)}")
        t0 = time.monotonic()
        proc = subprocess.run(  # noqa: S603 - fixed argv from CHECKS
            [sys.executable, *argv_tail],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        duration = round(time.monotonic() - t0, 3)
        (out_dir / log_name).write_text(
            proc.stdout + proc.stderr, encoding="utf-8", errors="replace"
        )
        status = "pass" if proc.returncode == 0 else "fail"
        overall |= proc.returncode
        results.append(
            {
                "tool": name,
                "argv": [sys.executable, *argv_tail],
                "status": status,
                "exit_code": proc.returncode,
                "duration_seconds": duration,
                "log": log_name,
            }
        )
        print(f"[verify] {name}: {status} (exit {proc.returncode}, {duration}s)")

    report = {
        "timestamp_utc": stamp,
        "commit": _commit(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "overall": "pass" if overall == 0 else "fail",
        "duration_seconds": round(time.monotonic() - started, 3),
        "results": results,
    }
    report_path = out_dir / "report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    print(f"[verify] report: {report_path} (overall: {report['overall']})")
    return overall


if __name__ == "__main__":
    raise SystemExit(run_verification())
