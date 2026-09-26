"""Duplicate-key-rejecting YAML loading shared by all config-side parsers.

Both the four-file config bundle (``config/loader.py``) and the compose
template reader (``config/compose_template.py``) must reject duplicate
mapping keys: silent last-wins overrides inside an administrator authority
file are exactly the class of error the fail-fast load path exists to catch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from drawbridge.errors import ConfigInvalidError


class StrictLoader(yaml.SafeLoader):  # type: ignore[misc]  # yaml is untyped
    """SafeLoader that rejects duplicate mapping keys."""


def _no_duplicates_constructor(
    loader: yaml.Loader, node: yaml.Node, deep: bool = False
) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError:
            raise yaml.constructor.ConstructorError(
                None, None, f"unhashable mapping key: {key!r}", key_node.start_mark
            ) from None
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate mapping key: {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates_constructor
)


def load_yaml_text(text: str, source: str) -> Any:
    """Parse YAML text with the strict loader; config errors become CONFIG_INVALID."""
    try:
        return yaml.load(text, Loader=StrictLoader)  # noqa: S506 - strict SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ConfigInvalidError(f"invalid YAML in {source}: {exc}") from None


def load_yaml_file(path: str | Path) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            return load_yaml_text(fh.read(), str(path))
    except FileNotFoundError:
        raise ConfigInvalidError(f"configuration file not found: {path}") from None
    except OSError as exc:
        raise ConfigInvalidError(f"cannot read {path}: {exc}") from None
