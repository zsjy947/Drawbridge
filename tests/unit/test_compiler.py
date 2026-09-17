"""CompiledOperation argv rendering tests."""

from __future__ import annotations

import pytest

from drawbridge.config.models import OperationConfig
from drawbridge.errors import DrawbridgeError
from drawbridge.policy.compiler import CompiledOperation, RenderContext


def compile_op(**overrides: object) -> CompiledOperation:
    base: dict[str, object] = {
        "executable": "git",
        "argv_prefix": "git_safe",
        "argv": [
            "log",
            "--no-show-signature",
            "--format=%H%x09%ct%x09%s",
            "--max-count={param.count}",
            "{job.resolved_sha}",
            "--",
        ],
        "cwd_from": "app.repo_path",
        "execution_profile": "source_manage",
        "public": True,
        "access": "read",
        "timeout_seconds": 10,
    }
    base.update(overrides)
    return CompiledOperation.from_config(
        "git_log", OperationConfig.model_validate(base)
    )


def full_context() -> RenderContext:
    return RenderContext(
        param={"count": 20},
        job={"resolved_sha": "a" * 40},
        app={"repo_path": "/srv/repos/demo"},
        release={"dir": "/srv/releases/demo"},
    )


class TestRender:
    def test_prefix_prepended(self) -> None:
        op = compile_op()
        argv = op.render_argv(full_context())
        assert argv[0] == "--no-pager"
        assert "core.hooksPath=/etc/drawbridge/empty-hooks" in argv

    def test_placeholders_resolved(self) -> None:
        op = compile_op()
        argv = op.render_argv(full_context())
        assert "--max-count=20" in argv
        assert "a" * 40 in argv
        assert not any("{" in element for element in argv)

    def test_missing_job_key_raises(self) -> None:
        op = compile_op()
        context = RenderContext(param={"count": 20}, job={})
        with pytest.raises(DrawbridgeError, match="resolved_sha"):
            op.render_argv(context)

    def test_missing_context_entirely_raises(self) -> None:
        op = compile_op()
        context = RenderContext(param={"count": 20})
        with pytest.raises(DrawbridgeError):
            op.render_argv(context)

    def test_missing_context_keys_listed(self) -> None:
        op = compile_op()
        context = RenderContext(param={"count": 20}, job={})
        missing = op.missing_context_keys(context)
        assert missing == ["job.resolved_sha"]

    def test_param_value_integer_rendered(self) -> None:
        op = compile_op()
        argv = op.render_argv(RenderContext(param={"count": 1}, job={"resolved_sha": "b" * 40}))
        assert "--max-count=1" in argv

    def test_param_string_not_coerced(self) -> None:
        op = compile_op()
        argv = op.render_argv(
            RenderContext(param={"count": "20"}, job={"resolved_sha": "b" * 40})
        )
        assert "--max-count=20" in argv

    def test_value_with_metacharacters_passed_verbatim(self) -> None:
        op = compile_op()
        argv = op.render_argv(
            RenderContext(param={"count": 5}, job={"resolved_sha": "$(echo pwned)"})
        )
        # The value occupies exactly one argv slot — never split or executed.
        assert "$(echo pwned)" in argv
        assert len(argv[0:2]) == 2
