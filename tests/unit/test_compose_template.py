"""Compose template fingerprinting and structural validation tests (plan D3).

The template digest follows the config_digest canonicalization rule: comment
and key-order changes leave it stable; any semantic edit changes it.  Bad
structure (token count, service drift, broken YAML, duplicate keys) is
rejected at config-load time whenever the template file exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drawbridge.config.compose_template import (
    normalized_template_digest,
    parse_template,
    read_compose_template,
    validate_structure,
)
from drawbridge.errors import ConfigInvalidError

SERVICES = ["api"]

VALID = """\
# admin comment
services:
  api:
    image: REPLACE_BY_DRAWBRIDGE
    ports:
      - "18080:8080"
"""


def _digest_of(text: str) -> str:
    parsed = parse_template(text, "test")
    return normalized_template_digest(parsed)


class TestDigestStability:
    def test_comment_and_blank_changes_keep_digest(self) -> None:
        variant = "\n# another comment\n\nservices:\n  api:\n    image: REPLACE_BY_DRAWBRIDGE\n    ports:\n      - \"18080:8080\"\n"
        assert _digest_of(VALID) == _digest_of(variant)

    def test_key_order_changes_keep_digest(self) -> None:
        reordered = """\
services:
  api:
    ports:
      - "18080:8080"
    image: REPLACE_BY_DRAWBRIDGE
"""
        assert _digest_of(VALID) == _digest_of(reordered)

    def test_semantic_change_alters_digest(self) -> None:
        changed = VALID.replace("18080", "18081")
        assert _digest_of(VALID) != _digest_of(changed)

    def test_service_rename_alters_digest(self) -> None:
        changed = VALID.replace("\n  api:", "\n  web:")
        assert _digest_of(VALID) != _digest_of(changed)


class TestStructuralValidation:
    def test_valid_template_passes(self) -> None:
        parsed = parse_template(VALID, "t")
        names = validate_structure(VALID, parsed, SERVICES, "t")
        assert names == ["api"]

    def test_token_count_zero_and_two_rejected(self) -> None:
        for text in (
            "services:\n  api:\n    image: demo:latest\n",
            "services:\n  api:\n    image: REPLACE_BY_DRAWBRIDGE\n"
            "    label: REPLACE_BY_DRAWBRIDGE\n",
        ):
            parsed = parse_template(text, "t")
            with pytest.raises(ConfigInvalidError, match="exactly one"):
                validate_structure(text, parsed, SERVICES, "t")

    def test_service_set_drift_rejected_with_names(self) -> None:
        text = "services:\n  api:\n    image: REPLACE_BY_DRAWBRIDGE\n  worker:\n    image: demo:latest\n"
        parsed = parse_template(text, "t")
        with pytest.raises(ConfigInvalidError, match="worker"):
            validate_structure(text, parsed, SERVICES, "t")

    def test_missing_services_mapping_rejected(self) -> None:
        with pytest.raises(ConfigInvalidError, match="services"):
            parse_template("version: '3'\n", "t")

    def test_duplicate_service_key_rejected(self) -> None:
        text = "services:\n  api:\n    image: REPLACE_BY_DRAWBRIDGE\n  api:\n    image: demo:latest\n"
        with pytest.raises(ConfigInvalidError):
            parse_template(text, "t")

    def test_broken_yaml_rejected_without_native_exception(self) -> None:
        with pytest.raises(ConfigInvalidError, match="invalid YAML"):
            parse_template("services: [unclosed\n", "t")

    def test_missing_file_rejected_on_read(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigInvalidError, match="cannot read"):
            read_compose_template(tmp_path / "nope.yaml", SERVICES)


class TestLoadTimeValidation:
    """Bundle-level: a bad template on disk fails config loading (fail fast)."""

    @pytest.fixture()
    def bundle(self, tmp_path: Path) -> Path:
        import shutil

        repo = Path(__file__).resolve().parents[2]
        for name in ("drawbridge.yaml", "apps.yaml", "operations.yaml", "workflows.yaml"):
            shutil.copyfile(repo / "configs" / name, tmp_path / name)
        compose_dir = tmp_path / "etc" / "compose"
        compose_dir.mkdir(parents=True)
        (compose_dir / "demo.staging.yaml").write_text(VALID, encoding="utf-8")
        # apps.yaml points at /etc/drawbridge — rewrite to the tmp bundle
        apps = (tmp_path / "apps.yaml").read_text(encoding="utf-8")
        apps = apps.replace(
            "/etc/drawbridge/compose/demo.staging.yaml",
            (compose_dir / "demo.staging.yaml").as_posix(),
        )
        (tmp_path / "apps.yaml").write_text(apps, encoding="utf-8")
        return tmp_path

    def _load(self, bundle: Path):  # type: ignore[no-untyped-def]
        from drawbridge.config.loader import load_config_from_dir

        return load_config_from_dir(bundle)

    def test_valid_bundle_loads(self, bundle: Path) -> None:
        config = self._load(bundle)
        assert "demo" in config.apps

    def test_two_token_template_fails_load(self, bundle: Path) -> None:
        target = bundle / "etc" / "compose" / "demo.staging.yaml"
        target.write_text(
            "services:\n  api:\n    image: REPLACE_BY_DRAWBRIDGE\n"
            "    label: REPLACE_BY_DRAWBRIDGE\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigInvalidError, match="exactly one"):
            self._load(bundle)

    def test_service_drift_fails_load(self, bundle: Path) -> None:
        target = bundle / "etc" / "compose" / "demo.staging.yaml"
        target.write_text(
            "services:\n  web:\n    image: REPLACE_BY_DRAWBRIDGE\n", encoding="utf-8"
        )
        with pytest.raises(ConfigInvalidError, match="do not match"):
            self._load(bundle)
