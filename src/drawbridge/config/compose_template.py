"""Compose template parsing, structural validation and fingerprinting (plan D3).

The admin-side compose template is an external file outside the four-YAML
``config_digest``; before D3 it took part in no plan re-validation at all, so
an administrator editing the template between ``plan`` and ``apply`` (the
910B cutoff does exactly this to register ``/dev/davinci2``) silently changed
what an already-queued plan would deploy.

This module gives the template three properties at every load site:

* **structure** — strict YAML, non-empty top-level ``services`` mapping,
  exactly one ``REPLACE_BY_DRAWBRIDGE`` image token, and a service-name set
  equal to the environment's registered ``services``;
* **fingerprint** — a SHA-256 over the normalized JSON of the parsed tree
  (same canonicalization rule as ``config_digest``: comments, whitespace and
  key order changes do not invalidate plans);
* **fail-closed errors** — every failure surfaces as ``CONFIG_INVALID``
  without echoing native parser exceptions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from drawbridge.config.yamlstrict import load_yaml_text
from drawbridge.errors import ConfigInvalidError

#: Placeholder token in the admin compose template replaced by the frozen
#: immutable image ID of the release (apps.yaml documents this contract).
#: Canonical home; ``runner/runtime.py`` re-exports it for the renderer.
COMPOSE_IMAGE_TOKEN = "REPLACE_BY_DRAWBRIDGE"  # noqa: S105 - template marker, not a secret


@dataclass(frozen=True)
class ComposeTemplateInfo:
    """Validated template projection: service topology + fingerprint."""

    services: list[str]
    digest: str


def parse_template(text: str, source: str) -> dict[str, Any]:
    """Strict-parse template text; require a non-empty ``services`` mapping."""
    parsed = load_yaml_text(text, source)
    if not isinstance(parsed, dict):
        raise ConfigInvalidError(
            f"compose template {source} must be a YAML mapping at the top level"
        )
    services = parsed.get("services")
    if not isinstance(services, dict) or not services:
        raise ConfigInvalidError(
            f"compose template {source} must declare a non-empty 'services' mapping"
        )
    if any(not isinstance(name, str) or not name for name in services):
        raise ConfigInvalidError(
            f"compose template {source} has non-string service names"
        )
    return parsed


def validate_structure(
    text: str,
    parsed: dict[str, Any],
    declared_services: list[str],
    source: str,
) -> list[str]:
    """Token count + service-set match; returns the sorted service names."""
    count = text.count(COMPOSE_IMAGE_TOKEN)
    if count != 1:
        raise ConfigInvalidError(
            f"compose template {source} must contain exactly one "
            f"{COMPOSE_IMAGE_TOKEN} image token (found {count})"
        )
    names = sorted(parsed["services"])
    declared = sorted(declared_services)
    if names != declared:
        only_template = sorted(set(names) - set(declared))
        only_registry = sorted(set(declared) - set(names))
        raise ConfigInvalidError(
            f"compose template {source} services do not match the registered "
            f"environment services (only in template: {only_template}, "
            f"only in apps.yaml: {only_registry})"
        )
    return names


def normalized_template_digest(parsed: Any, source: str = "compose template") -> str:
    """SHA-256 over the canonical JSON of the parsed tree (D3 rule)."""
    try:
        payload = json.dumps(
            parsed, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise ConfigInvalidError(
            f"compose template {source} contains values that cannot be "
            f"normalized into a stable fingerprint: {exc}"
        ) from exc
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_compose_template(
    path: str | Path, declared_services: list[str]
) -> ComposeTemplateInfo:
    """Read + structurally validate + fingerprint one admin compose template."""
    template_path = Path(path)
    try:
        text = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigInvalidError(
            f"cannot read compose template {template_path}: {exc}"
        ) from exc
    parsed = parse_template(text, str(template_path))
    services = validate_structure(text, parsed, declared_services, str(template_path))
    return ComposeTemplateInfo(
        services=services, digest=normalized_template_digest(parsed, str(template_path))
    )


__all__ = [
    "COMPOSE_IMAGE_TOKEN",
    "ComposeTemplateInfo",
    "normalized_template_digest",
    "parse_template",
    "read_compose_template",
    "validate_structure",
]
