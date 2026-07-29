from __future__ import annotations

import json
from pathlib import Path

import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure.capabilities import CapabilityToken
from omr_grader.infrastructure.config_store import (
    AppConfig,
    default_config,
    load_config,
    validate_config,
)
from omr_grader.infrastructure.paths import ManagedPaths


@pytest.mark.parametrize(
    ("contents", "warning_code"),
    [
        (None, "CONFIG_MISSING"),
        (b"{not json", "CONFIG_INVALID"),
        (b"[]", "CONFIG_INVALID"),
        (
            b'{"default_profile":"","default_sensitivity":true,"use_multiprocessing":true}',
            "CONFIG_INVALID",
        ),
    ],
)
def test_load_config_uses_safe_defaults_for_missing_damaged_or_wrong_type_json(
    tmp_path: Path, contents: bytes | None, warning_code: str
) -> None:
    paths = ManagedPaths.from_root(tmp_path)
    if contents is not None:
        paths.config_path.write_bytes(contents)

    result = load_config(paths)

    assert isinstance(result, Ok)
    assert result.value == default_config()
    assert len(result.warnings) == 1
    assert result.warnings[0].code == warning_code
    assert "안전한 기본 설정" in str(result.warnings[0].context["reason"])
    if warning_code == "CONFIG_INVALID":
        assert result.warnings[0].cause_type is None


@pytest.mark.parametrize(
    ("failure", "cause_type"),
    [
        (PermissionError("access denied"), "PermissionError"),
        (OSError("sharing violation"), "OSError"),
    ],
)
def test_load_config_reports_io_failures_distinctly_from_malformed_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: OSError, cause_type: str
) -> None:
    paths = ManagedPaths.from_root(tmp_path)
    paths.config_path.write_text("{}", encoding="utf-8")

    def fail_open(path: Path, *args: object, **kwargs: object):
        if path == paths.config_path:
            raise failure
        return original_open(path, *args, **kwargs)

    original_open = Path.open
    monkeypatch.setattr(Path, "open", fail_open)

    result = load_config(paths)

    assert isinstance(result, Ok)
    assert result.value == default_config()
    assert len(result.warnings) == 1
    assert result.warnings[0].code == "CONFIG_IO_FAILED"
    assert result.warnings[0].cause_type == cause_type


def test_validate_config_rejects_boolean_sensitivity() -> None:
    result = validate_config(AppConfig("", True, True))

    assert isinstance(result, Err)
    assert result.errors[0].code == "INVALID_CONFIG"
    assert "민감도" in str(result.errors[0].context["reason"])


def test_load_config_ignores_unknown_keys_for_forward_compatibility(tmp_path: Path) -> None:
    paths = ManagedPaths.from_root(tmp_path)
    expected = AppConfig("시험지.omrtemplate", 7, False)
    payload = expected.to_dict() | {"future_option": {"enabled": True}}
    paths.config_path.write_text(json.dumps(payload), encoding="utf-8")

    result = load_config(paths)

    assert isinstance(result, Ok)
    assert result.value == expected
    assert result.warnings == ()


@pytest.mark.parametrize(
    "default_profile",
    ["../outside.omrtemplate", "folder/profile.omrtemplate", "C:\\outside.omrtemplate"],
)
def test_validate_config_rejects_traversal_or_path_default_profile(default_profile: str) -> None:
    result = validate_config(AppConfig(default_profile, 3, True))

    assert isinstance(result, Err)
    assert result.errors[0].code == "INVALID_CONFIG"
    assert "프로필" in str(result.errors[0].context["reason"])


def test_missing_config_is_created_only_under_injected_portable_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    portable_root = tmp_path / "portable"
    outside_root = tmp_path / "outside"
    portable_root.mkdir()
    outside_root.mkdir()
    paths = ManagedPaths.from_root(portable_root)
    monkeypatch.chdir(outside_root)

    result = load_config(paths, CapabilityToken.for_testing(paths.root))

    assert isinstance(result, Ok)
    assert result.value == default_config()
    assert paths.config_path.is_file()
    assert not (outside_root / "config.json").exists()
    assert json.loads(paths.config_path.read_text(encoding="utf-8")) == default_config().to_dict()
