"""ExecutionSpec and environment construction tests."""

from __future__ import annotations

import sys

import pytest

from drawbridge.config.models import ExecutionProfile, ProfileEnvConfig
from drawbridge.errors import DrawbridgeError
from drawbridge.executor.spec import build_environment, git_environment


class TestBuildEnvironment:
    def test_empty_baseline(self) -> None:
        env = build_environment(ExecutionProfile.SOURCE_MANAGE, {}, platform="linux")
        assert env["LANG"] == "C.UTF-8"
        assert env["TZ"] == "UTC"
        assert "/usr/bin" in env["PATH"]
        # Built from an empty set: nothing leaks from os.environ.
        assert "BASH_ENV" not in env
        assert "LD_PRELOAD" not in env
        assert "PYTHONPATH" not in env

    def test_does_not_inherit_process_environment(self) -> None:
        import os

        os.environ["DRAWBRIDGE_TEST_LEAK"] = "1"
        env = build_environment(ExecutionProfile.HOST_OBSERVE, {}, platform="linux")
        assert "DRAWBRIDGE_TEST_LEAK" not in env

    def test_profile_home(self) -> None:
        env_map = {
            ExecutionProfile.HOST_OBSERVE: ProfileEnvConfig(home="/home/dw-observe")
        }
        env = build_environment(ExecutionProfile.HOST_OBSERVE, env_map, platform="linux")
        assert env["HOME"] == "/home/dw-observe"

    def test_forbidden_extra_key_rejected(self) -> None:
        with pytest.raises(DrawbridgeError):
            build_environment(
                ExecutionProfile.HOST_OBSERVE,
                {},
                extra={"LD_PRELOAD": "/tmp/evil.so"},
                platform="linux",
            )

    def test_extra_allowed_keys(self) -> None:
        env = build_environment(
            ExecutionProfile.SOURCE_MANAGE,
            {},
            extra={"GIT_TERMINAL_PROMPT": "0"},
            platform="linux",
        )
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_windows_dev_path(self) -> None:
        env = build_environment(ExecutionProfile.HOST_OBSERVE, {})
        assert sys.executable.rsplit("\\", 1)[0] in env["PATH"] or "System32" in env["PATH"]


class TestGitEnvironment:
    def test_git_hardening(self) -> None:
        result = git_environment({}, platform="linux")
        assert result["GIT_TERMINAL_PROMPT"] == "0"
        assert result["GIT_CONFIG_NOSYSTEM"] == "1"
        assert result["GIT_CONFIG_GLOBAL"] == "/etc/drawbridge/gitconfig"
        assert result["GIT_SSH"] == "/etc/drawbridge/ssh-wrapper"

    def test_git_env_has_base(self) -> None:
        result = git_environment({}, platform="linux")
        assert result["LANG"] == "C.UTF-8"
        assert "BASH_ENV" not in result


class TestExecutionSpecValidation:
    def make_spec(self, **overrides: object):
        from tests.conftest import make_spec

        return make_spec(**overrides)  # type: ignore[call-arg]

    def test_relative_executable_rejected(self) -> None:
        with pytest.raises(DrawbridgeError, match="absolute"):
            self.make_spec(executable="python", argv=("-c", "print(1)"))

    def test_empty_argv_rejected(self) -> None:
        with pytest.raises(DrawbridgeError, match="argv"):
            self.make_spec(argv=())

    def test_nul_in_argv_rejected(self) -> None:
        with pytest.raises(DrawbridgeError, match="NUL"):
            self.make_spec(argv=("-c", "a\x00b"))

    def test_relative_cwd_rejected(self) -> None:
        with pytest.raises(DrawbridgeError, match="cwd"):
            self.make_spec(argv=("-c", "pass"), cwd="relative/path")

    def test_hard_limit_below_soft_rejected(self) -> None:
        with pytest.raises(DrawbridgeError, match="hard output limit"):
            self.make_spec(argv=("-c", "pass"), max_output_bytes=1000, hard_limit=500)
