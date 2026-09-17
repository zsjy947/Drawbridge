"""Operation compiler and argv renderer.

The compiler turns a validated :class:`OperationConfig` into an immutable
:class:`CompiledOperation` at load time.  Rendering later accepts typed,
already-validated inputs only:

* ``param.*``   — validated request parameters (typed str/int);
* ``app.*``     — administrator configuration fields;
* ``job.*``     — server-generated typed state (re-verified by the Runner);
* ``release.*`` — server release record fields.

The three contexts can never override each other: rendering looks each
placeholder up in exactly one context, in that order of declaration, and a
missing key is a hard error rather than an empty string (MVP spec §3).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from drawbridge.config.models import (
    ARGV_PREFIX_PRESETS,
    Access,
    ExecutionProfile,
    OperationConfig,
    OutputBudget,
)
from drawbridge.errors import DrawbridgeError, ErrorCode

_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\}")


class RenderContext(Mapping[tuple[str, str], str]):
    """Ordered lookup over (context, key) pairs with per-context dicts."""

    def __init__(self, **contexts: Mapping[str, Any]) -> None:
        self._contexts = {
            name: dict(mapping) for name, mapping in contexts.items()
        }

    def __getitem__(self, key: tuple[str, str]) -> str:
        ctx_name, field_name = key
        try:
            mapping = self._contexts[ctx_name]
        except KeyError:
            raise KeyError(f"{ctx_name}.{field_name}") from None
        if field_name not in mapping:
            raise KeyError(f"{ctx_name}.{field_name}")
        return str(mapping[field_name])

    def __iter__(self) -> Any:  # pragma: no cover - not used in rendering
        return iter(())

    def __len__(self) -> int:  # pragma: no cover
        return 0


@dataclass(frozen=True)
class CompiledOperation:
    """Immutable, load-time-compiled form of one registered operation."""

    name: str
    executable: str | None
    argv_prefix: tuple[str, ...]
    argv_template: tuple[str, ...]
    handler: str | None
    prepare: str
    parameter_specs: dict[str, Any]
    cwd_from: str | None
    execution_profile: ExecutionProfile
    public: bool
    access: Access
    timeout_seconds: int
    accepted_exit_codes: frozenset[int]
    output: OutputBudget
    lock: str | None
    placeholders: tuple[tuple[str, str], ...] = field(default=())

    @classmethod
    def from_config(cls, name: str, config: OperationConfig) -> CompiledOperation:
        placeholders: list[tuple[str, str]] = []
        for element in config.argv or ():
            for match in _PLACEHOLDER_RE.finditer(element):
                placeholders.append((match.group(1), match.group(2)))
        return cls(
            name=name,
            executable=config.executable,
            argv_prefix=tuple(ARGV_PREFIX_PRESETS.get(config.argv_prefix or "", ())),
            argv_template=tuple(config.argv or ()),
            handler=config.handler,
            prepare=config.prepare,
            parameter_specs=dict(config.parameters),
            cwd_from=config.cwd_from.value if config.cwd_from else None,
            execution_profile=config.execution_profile,
            public=config.public,
            access=config.access,
            timeout_seconds=config.timeout_seconds,
            accepted_exit_codes=frozenset(config.accepted_exit_codes),
            output=config.output,
            lock=config.lock,
            placeholders=tuple(placeholders),
        )

    def render_argv(self, context: RenderContext) -> list[str]:
        """Render one argv array; raises on any unresolved placeholder."""
        argv: list[str] = list(self.argv_prefix)
        for element in self.argv_template:
            argv.append(_render_element(element, context))
        return argv

    def missing_context_keys(self, context: RenderContext) -> list[str]:
        missing: list[str] = []
        for ctx_name, field_name in self.placeholders:
            try:
                context[(ctx_name, field_name)]
            except KeyError:
                missing.append(f"{ctx_name}.{field_name}")
        return missing


def _render_element(element: str, context: RenderContext) -> str:
    match = _PLACEHOLDER_RE.search(element)
    if match is None:
        return element
    key = (match.group(1), match.group(2))
    try:
        value = context[key]
    except KeyError:
        raise DrawbridgeError(
            f"placeholder {key[0]}.{key[1]} missing from execution context",
            code=ErrorCode.INTERNAL,
        ) from None
    return element[: match.start()] + value + element[match.end() :]
