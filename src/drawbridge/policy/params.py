"""Request parameter validation engine (MVP spec §2).

Every parameter passes through this module exactly once, at the Gateway,
before any argv is rendered:

* strict JSON types — a string ``"20"`` is never accepted for an integer,
  ``true`` is never accepted for ``1``;
* unknown parameter names are rejected (the envelope fields app /
  environment / idempotency_key / tracing fields never live in parameters);
* NUL and control characters are rejected; ``reason`` additionally rejects
  line breaks via the dedicated validator;
* administrator patterns run under ``regex.fullmatch`` with a 100 ms
  budget — pathological patterns fail closed with ``INVALID_PARAMETER``;
* semantic validators (registered service, allowed ref, ...) are a fixed
  code-level enum and receive already type-checked values only.
"""

from __future__ import annotations

import re
from typing import Any

import regex as regex_module

from drawbridge.config.models import ParameterSpec
from drawbridge.errors import DrawbridgeError, ErrorCode, InvalidParameterError

REGEX_TIMEOUT_SECONDS = 0.1

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_APP_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")

#: git_ref shapes (MVP spec §2).  A SHA is valid only as exactly 40 lowercase
#: hex characters; short SHAs, expressions and URLs never match.
_GIT_REF_SHAPES = (
    re.compile(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*"),
    re.compile(r"refs/tags/[A-Za-z0-9][A-Za-z0-9._/-]*"),
    re.compile(r"[0-9a-f]{40}"),
)


class ValidationContext:
    """What semantic validators may look at.

    Deliberately tiny: validators see admin-configured registries, never
    filesystem paths or raw requests.
    """

    def __init__(
        self,
        *,
        app_id: str,
        environment: str,
        services: frozenset[str] = frozenset(),
        restartable_services: frozenset[str] = frozenset(),
        file_aliases: frozenset[str] = frozenset(),
        validator_names: frozenset[str] = frozenset(),
        allowed_ref_patterns: tuple[str, ...] = (),
    ) -> None:
        self.app_id = app_id
        self.environment = environment
        self.services = services
        self.restartable_services = restartable_services
        self.file_aliases = file_aliases
        self.validator_names = validator_names
        self.allowed_ref_patterns = allowed_ref_patterns


def _fullmatch_with_budget(pattern: str, value: str) -> bool:
    """Admin-regex fullmatch under a hard time budget (fail closed)."""
    try:
        return regex_module.fullmatch(pattern, value, timeout=REGEX_TIMEOUT_SECONDS) is not None
    except TimeoutError:
        return False
    except regex_module.error:
        # Pattern was validated at config load; a runtime error means the
        # engine refuses to guess — reject the value.
        return False


def _reject_control_chars(name: str, value: str, *, allow_newline: bool = False) -> str:
    for ch in value:
        code = ord(ch)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            if allow_newline and ch in "\r\n":
                continue
            raise InvalidParameterError(f"parameter {name!r} contains control characters")
    if "\x00" in value:
        raise InvalidParameterError(f"parameter {name!r} contains NUL")
    return value


def _validate_string(name: str, value: str, spec: ParameterSpec) -> str:
    _reject_control_chars(name, value)
    if spec.min_length is not None and len(value) < spec.min_length:
        raise InvalidParameterError(f"parameter {name!r} is shorter than {spec.min_length}")
    if spec.max_length is not None and len(value) > spec.max_length:
        raise InvalidParameterError(f"parameter {name!r} is longer than {spec.max_length}")
    if spec.pattern is not None and not _fullmatch_with_budget(spec.pattern, value):
        raise InvalidParameterError(f"parameter {name!r} does not match the allowed shape")
    return value


def _validate_integer(name: str, value: int, spec: ParameterSpec) -> int:
    if isinstance(value, bool):
        raise InvalidParameterError(f"parameter {name!r} must be an integer, not bool")
    if spec.minimum is not None and value < spec.minimum:
        raise InvalidParameterError(f"parameter {name!r} below minimum {spec.minimum}")
    if spec.maximum is not None and value > spec.maximum:
        raise InvalidParameterError(f"parameter {name!r} above maximum {spec.maximum}")
    return value


def _apply_semantic_validators(
    name: str, value: str, spec: ParameterSpec, ctx: ValidationContext
) -> str:
    for validator in spec.validators:
        ok = _run_semantic_validator(validator, name, value, ctx)
        if not ok:
            raise InvalidParameterError(
                f"parameter {name!r} rejected by validator {validator!r}"
            )
    return value


def _run_semantic_validator(validator: str, name: str, value: str, ctx: ValidationContext) -> bool:
    if validator == "git_ref_or_commit":
        return any(shape.fullmatch(value) for shape in _GIT_REF_SHAPES)
    if validator == "allowed_app_ref":
        # Shape must hold, then the value must fully match one of the
        # admin-registered allow patterns for this app.
        if not any(shape.fullmatch(value) for shape in _GIT_REF_SHAPES):
            return False
        return any(
            _fullmatch_with_budget(pattern, value) for pattern in ctx.allowed_ref_patterns
        )
    if validator == "registered_service":
        return value in ctx.services
    if validator == "registered_restartable_service":
        return value in ctx.restartable_services
    if validator == "registered_file_alias":
        return value in ctx.file_aliases
    if validator == "registered_validator":
        return value in ctx.validator_names
    if validator == "environment_of_app":
        return value == ctx.environment
    if validator == "uuid_record_exists":
        # Format-level check only; existence is resolved by the store layer.
        return _UUID_RE.fullmatch(value) is not None
    if validator == "registered_subdir":
        return _SUBDIR_SHAPE.fullmatch(value) is not None
    if validator == "opaque_cursor":
        # Opaque server-generated value: only charset/length bounded here.
        return bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,512}", value))
    # Unknown validator names cannot appear: they are filtered at load time.
    raise DrawbridgeError(f"unknown semantic validator {validator!r}", code=ErrorCode.INTERNAL)


_SUBDIR_SHAPE = re.compile(
    r"\.|[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*"
)


def validate_parameter(
    name: str,
    value: Any,
    spec: ParameterSpec,
    ctx: ValidationContext,
) -> str | int:
    """Validate one parameter against its spec; returns the typed value."""
    if spec.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidParameterError(
                f"parameter {name!r} must be an integer"
                + (" (received bool)" if isinstance(value, bool) else "")
            )
        typed: str | int = _validate_integer(name, value, spec)
        return typed
    if not isinstance(value, str):
        raise InvalidParameterError(
            f"parameter {name!r} must be a string (received {type(value).__name__})"
        )
    typed = _validate_string(name, value, spec)
    return _apply_semantic_validators(name, typed, spec, ctx)


def validate_parameters(
    specs: dict[str, ParameterSpec],
    provided: dict[str, Any],
    ctx: ValidationContext,
) -> dict[str, str | int]:
    """Validate a full parameter map, applying defaults for absent values."""
    unknown = set(provided) - set(specs)
    if unknown:
        raise InvalidParameterError(f"unknown parameters: {sorted(unknown)}")
    resolved: dict[str, str | int] = {}
    for name, spec in specs.items():
        if name in provided:
            resolved[name] = validate_parameter(name, provided[name], spec, ctx)
        elif spec.default is not None:
            resolved[name] = validate_parameter(name, spec.default, spec, ctx)
        else:
            raise InvalidParameterError(f"missing required parameter {name!r}")
    return resolved


def validate_reason(value: Any) -> str:
    """``reason`` fields: 1-256 chars, no control chars, no line breaks."""
    if not isinstance(value, str):
        raise InvalidParameterError("reason must be a string")
    if not (1 <= len(value) <= 256):
        raise InvalidParameterError("reason must be 1-256 characters")
    for ch in value:
        code = ord(ch)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            raise InvalidParameterError("reason must not contain control characters or line breaks")
    return value


def validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidParameterError("idempotency_key must be a string")
    if not (8 <= len(value) <= 128):
        raise InvalidParameterError("idempotency_key must be 8-128 characters")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", value):
        raise InvalidParameterError("idempotency_key does not match the allowed shape")
    return value


def validate_app_id(value: Any) -> str:
    if not isinstance(value, str) or not _APP_ID_RE.fullmatch(value):
        raise InvalidParameterError("app id does not match the allowed shape")
    return value


def validate_tracing_id(value: Any, *, field: str) -> str:
    """agent_id / parent_task_id: tracing only, never identity."""
    if not isinstance(value, str):
        raise InvalidParameterError(f"{field} must be a string")
    if not (1 <= len(value) <= 128):
        raise InvalidParameterError(f"{field} must be 1-128 characters")
    _reject_control_chars(field, value)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise InvalidParameterError(f"{field} does not match the allowed shape")
    return value


def build_validation_context(
    app_id: str,
    environment: str,
    *,
    services: list[str] | None = None,
    restartable_services: list[str] | None = None,
    file_aliases: list[str] | None = None,
    validator_names: list[str] | None = None,
    allowed_ref_patterns: list[str] | None = None,
) -> ValidationContext:
    return ValidationContext(
        app_id=app_id,
        environment=environment,
        services=frozenset(services or ()),
        restartable_services=frozenset(restartable_services or ()),
        file_aliases=frozenset(file_aliases or ()),
        validator_names=frozenset(validator_names or ()),
        allowed_ref_patterns=tuple(allowed_ref_patterns or ()),
    )
