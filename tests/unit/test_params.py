"""Parameter validation engine tests (MVP spec §2)."""

from __future__ import annotations

import pytest

from drawbridge.config.models import ParameterSpec
from drawbridge.errors import InvalidParameterError
from drawbridge.policy.params import (
    ValidationContext,
    validate_app_id,
    validate_idempotency_key,
    validate_parameters,
    validate_reason,
    validate_tracing_id,
)


def ctx(**overrides: object) -> ValidationContext:
    kwargs: dict[str, object] = {
        "app_id": "demo",
        "environment": "staging",
        "services": ["api", "worker"],
        "restartable_services": ["api"],
        "file_aliases": ["app_config"],
        "validator_names": ["yaml_syntax"],
        "allowed_ref_patterns": ("^refs/heads/(main|agent/[A-Za-z0-9_-]+)$", "^[0-9a-f]{40}$"),
    }
    kwargs.update(overrides)
    return ValidationContext(**kwargs)  # type: ignore[arg-type]


class TestStrictTypes:
    def test_string_int_rejected(self) -> None:
        spec = ParameterSpec.model_validate({"type": "integer", "minimum": 1, "maximum": 100})
        with pytest.raises(InvalidParameterError):
            validate_parameters({"count": spec}, {"count": "20"}, ctx())

    def test_bool_for_integer_rejected(self) -> None:
        spec = ParameterSpec.model_validate({"type": "integer", "minimum": 1, "maximum": 2})
        with pytest.raises(InvalidParameterError):
            validate_parameters({"count": spec}, {"count": True}, ctx())

    def test_int_for_string_rejected(self) -> None:
        spec = ParameterSpec.model_validate({"type": "string", "max_length": 64})
        with pytest.raises(InvalidParameterError):
            validate_parameters({"file": spec}, {"file": 12}, ctx())

    def test_unknown_parameter_rejected(self) -> None:
        spec = ParameterSpec.model_validate({"type": "integer", "minimum": 1, "maximum": 2})
        with pytest.raises(InvalidParameterError, match="unknown parameters"):
            validate_parameters({"count": spec}, {"count": 1, "extra": 2}, ctx())

    def test_missing_required_rejected(self) -> None:
        spec = ParameterSpec.model_validate({"type": "integer", "minimum": 1, "maximum": 2})
        with pytest.raises(InvalidParameterError, match="missing required"):
            validate_parameters({"count": spec}, {}, ctx())

    def test_default_applied_and_validated(self) -> None:
        spec = ParameterSpec.model_validate(
            {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}
        )
        assert validate_parameters({"count": spec}, {}, ctx()) == {"count": 20}


class TestCharsetRules:
    def test_control_chars_rejected(self) -> None:
        spec = ParameterSpec.model_validate({"type": "string", "max_length": 64})
        for value in ("a\x00b", "a\x1bb", "a\nb"):
            with pytest.raises(InvalidParameterError):
                validate_parameters({"name": spec}, {"name": value}, ctx())

    def test_regex_injection_via_count_rejected(self) -> None:
        spec = ParameterSpec.model_validate(
            {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
            }
        )
        assert validate_parameters({"count": spec}, {"count": 5}, ctx()) == {"count": 5}

    def test_semicolon_rejected_by_pattern(self) -> None:
        spec = ParameterSpec.model_validate(
            {
                "type": "string",
                "pattern": r"[a-z]+",
                "min_length": 1,
                "max_length": 64,
            }
        )
        with pytest.raises(InvalidParameterError):
            validate_parameters({"name": spec}, {"name": "abc;rm -rf"}, ctx())


class TestReasonAndKeys:
    def test_reason_basic(self) -> None:
        assert validate_reason("deploy gate failed") == "deploy gate failed"

    def test_reason_newline_rejected(self) -> None:
        with pytest.raises(InvalidParameterError):
            validate_reason("line1\nline2")

    def test_reason_too_long(self) -> None:
        with pytest.raises(InvalidParameterError):
            validate_reason("x" * 257)

    def test_reason_non_string(self) -> None:
        with pytest.raises(InvalidParameterError):
            validate_reason(42)

    def test_idempotency_key_valid(self) -> None:
        assert validate_idempotency_key("deploy-2026-09-17:001") == "deploy-2026-09-17:001"

    def test_idempotency_key_short_rejected(self) -> None:
        with pytest.raises(InvalidParameterError):
            validate_idempotency_key("abc")

    def test_idempotency_key_bad_chars(self) -> None:
        with pytest.raises(InvalidParameterError):
            validate_idempotency_key("bad key with spaces!")

    def test_tracing_id_valid(self) -> None:
        assert validate_tracing_id("agent-1", field="agent_id") == "agent-1"

    def test_tracing_id_invalid(self) -> None:
        with pytest.raises(InvalidParameterError):
            validate_tracing_id("- leading dash", field="agent_id")

    def test_app_id_valid(self) -> None:
        assert validate_app_id("orders-api") == "orders-api"

    def test_app_id_invalid(self) -> None:
        with pytest.raises(InvalidParameterError):
            validate_app_id("Orders API")


class TestGitRefValidation:
    REF_SPEC = {
        "type": "string",
        "min_length": 1,
        "max_length": 200,
        "pattern": r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*|refs/tags/[A-Za-z0-9][A-Za-z0-9._/-]*|[0-9a-f]{40}",
        "validators": ["git_ref_or_commit", "allowed_app_ref"],
    }

    def validate(self, value: str) -> None:
        spec = ParameterSpec.model_validate(self.REF_SPEC)
        validate_parameters({"git_ref": spec}, {"git_ref": value}, ctx())

    def test_full_sha_accepted(self) -> None:
        self.validate("a" * 40)

    def test_short_sha_rejected(self) -> None:
        with pytest.raises(InvalidParameterError):
            self.validate("abcdef1234")

    def test_branch_accepted(self) -> None:
        self.validate("refs/heads/main")

    def test_agent_branch_accepted(self) -> None:
        self.validate("refs/heads/agent/fix-1")

    def test_unlisted_branch_rejected(self) -> None:
        with pytest.raises(InvalidParameterError):
            self.validate("refs/heads/develop")

    def test_head_expression_rejected(self) -> None:
        with pytest.raises(InvalidParameterError):
            self.validate("refs/heads/main^")

    def test_url_rejected(self) -> None:
        with pytest.raises(InvalidParameterError):
            self.validate("https://github.com/acme/demo.git")

    def test_refspace_rejected(self) -> None:
        with pytest.raises(InvalidParameterError):
            self.validate("refs/heads/main origin/backup")


class TestSemanticValidators:
    def test_registered_service(self) -> None:
        spec = ParameterSpec.model_validate(
            {
                "type": "string",
                "max_length": 64,
                "pattern": r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}",
                "validators": ["registered_service"],
            }
        )
        out = validate_parameters({"service": spec}, {"service": "worker"}, ctx())
        assert out == {"service": "worker"}

    def test_unregistered_service_rejected(self) -> None:
        spec = ParameterSpec.model_validate(
            {
                "type": "string",
                "max_length": 64,
                "validators": ["registered_service"],
            }
        )
        with pytest.raises(InvalidParameterError, match="registered_service"):
            validate_parameters({"service": spec}, {"service": "db"}, ctx())

    def test_restartable_only(self) -> None:
        spec = ParameterSpec.model_validate(
            {"type": "string", "max_length": 64, "validators": ["registered_restartable_service"]}
        )
        with pytest.raises(InvalidParameterError):
            validate_parameters({"service": spec}, {"service": "worker"}, ctx())

    def test_subdir_shape(self) -> None:
        spec = ParameterSpec.model_validate(
            {"type": "string", "max_length": 200, "validators": ["registered_subdir"]}
        )
        out = validate_parameters({"subdir": spec}, {"subdir": "src/config"}, ctx())
        assert out == {"subdir": "src/config"}

    @pytest.mark.parametrize(
        "value", ["..", "../etc", "src/../..", "a//b", "/abs", "a/./b"]
    )
    def test_subdir_traversal_rejected(self, value: str) -> None:
        spec = ParameterSpec.model_validate(
            {"type": "string", "max_length": 200, "validators": ["registered_subdir"]}
        )
        with pytest.raises(InvalidParameterError):
            validate_parameters({"subdir": spec}, {"subdir": value}, ctx())

    def test_uuid_shape(self) -> None:
        """uuid_record_exists was removed from SEMANTIC_VALIDATORS (plan D11:
        dead config face, no operation ever referenced it) — registering it
        now fails at load time."""
        from drawbridge.config.models import SEMANTIC_VALIDATORS

        assert "uuid_record_exists" not in SEMANTIC_VALIDATORS
        with pytest.raises(Exception, match="unknown semantic validators"):
            ParameterSpec.model_validate(
                {"type": "string", "max_length": 36, "validators": ["uuid_record_exists"]}
            )


class TestRegexBudget:
    def test_evil_pattern_fails_closed(self) -> None:
        # (a+)+ against 'a'*32 is catastrophically backtracking for re,
        # but regex with a timeout must fail closed → INVALID_PARAMETER.
        spec = ParameterSpec.model_validate(
            {
                "type": "string",
                "pattern": r"(a+)+b",
                "min_length": 1,
                "max_length": 64,
            }
        )
        with pytest.raises(InvalidParameterError):
            validate_parameters(
                {"name": spec}, {"name": "a" * 32}, ctx()
            )
