from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure import capabilities
from omr_grader.infrastructure import paths as paths_module
from omr_grader.infrastructure.capabilities import bootstrap_managed_paths, probe_root_capability
from omr_grader.infrastructure.paths import ManagedPaths


def test_importing_capabilities_does_not_write_to_user_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    before = tuple(home.iterdir())
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    importlib.reload(capabilities)

    assert tuple(home.iterdir()) == before


def test_probe_then_bootstrap_creates_only_managed_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "portable"
    root.mkdir()
    paths = ManagedPaths.from_root(root)
    monkeypatch.setattr(capabilities, "is_path_writable", lambda root: True)
    capability = probe_root_capability(paths)

    assert isinstance(capability, Ok)
    assert capability.value.write_enabled is True
    assert capability.value.token is not None
    assert not paths.profiles_dir.exists()
    assert not paths.data_dir.exists()
    assert not paths.logs_dir.exists()

    bootstrapped = bootstrap_managed_paths(paths, capability.value.token)

    assert isinstance(bootstrapped, Ok)
    assert paths.profiles_dir.is_dir()
    assert paths.data_dir.is_dir()
    assert paths.logs_dir.is_dir()
    assert {path.name for path in root.iterdir()} == {"Profiles", "OMR_Grader", "logs"}


def test_probe_returns_read_only_capability_when_root_is_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(capabilities, "is_path_writable", lambda root: False)
    capability = probe_root_capability(ManagedPaths.from_root(tmp_path))

    assert isinstance(capability, Ok)
    assert capability.value.write_enabled is False
    assert capability.value.token is None
    assert capability.value.read_only_reason is not None
    assert "읽기 전용" in capability.value.read_only_reason
    assert len(capability.warnings) == 1
    assert capability.warnings[0].code == "ROOT_WRITE_DENIED"


def test_bootstrap_rejects_token_for_another_portable_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    monkeypatch.setattr(capabilities, "is_path_writable", lambda root: True)
    first = probe_root_capability(ManagedPaths.from_root(first_root))
    assert isinstance(first, Ok)
    assert first.value.token is not None

    result = bootstrap_managed_paths(ManagedPaths.from_root(second_root), first.value.token)

    assert isinstance(result, Err)
    assert result.errors[0].code == "INVALID_CAPABILITY_TOKEN"


def test_probe_rejects_reparse_point_managed_entry_without_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = ManagedPaths.from_root(tmp_path)
    monkeypatch.setattr(
        paths_module,
        "_is_reparse_point",
        lambda path: path == paths.data_dir,
    )

    result = probe_root_capability(paths)

    assert isinstance(result, Err)
    assert result.errors[0].code == "MANAGED_PATH_INVALID"
    assert tuple(tmp_path.iterdir()) == ()
