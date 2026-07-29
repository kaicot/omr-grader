from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure import paths as paths_module
from omr_grader.infrastructure.paths import ManagedPaths, resolve_portable_root


def test_resolve_portable_root_uses_injected_source_entry(tmp_path: Path) -> None:
    entry = tmp_path / "source" / "main.py"

    assert resolve_portable_root(frozen=False, source_entry=entry) == entry.parent.resolve()


def test_resolve_portable_root_uses_injected_executable_when_frozen(tmp_path: Path) -> None:
    executable = tmp_path / "package" / "OMR_Grader.exe"

    assert resolve_portable_root(frozen=True, executable=executable) == executable.parent.resolve()


def test_resolve_portable_root_uses_runtime_entry_when_not_injected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry = tmp_path / "runtime" / "main.py"
    monkeypatch.setattr(paths_module.sys, "argv", [str(entry)])

    assert resolve_portable_root(frozen=False) == entry.parent.resolve()


def test_resolve_portable_root_uses_runtime_executable_when_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "packaged" / "OMR_Grader.exe"
    monkeypatch.setattr(paths_module.sys, "executable", str(executable))

    assert resolve_portable_root(frozen=True) == executable.parent.resolve()


def test_managed_paths_stay_beneath_portable_root(tmp_path: Path) -> None:
    paths = ManagedPaths.from_root(tmp_path / "portable")
    paths.root.mkdir()

    profile = paths.profile_path("시험지.omrtemplate")
    data = paths.data_path("session", "answers.json")

    assert isinstance(profile, Ok)
    assert profile.value == (paths.root / "Profiles" / "시험지.omrtemplate").resolve()
    assert isinstance(data, Ok)
    assert data.value.is_relative_to(paths.root)


@pytest.mark.parametrize(
    ("profile", "expected_code"),
    [
        ("../outside.omrtemplate", "INVALID_MANAGED_PATH"),
        ("nested/profile.omrtemplate", "INVALID_MANAGED_PATH"),
        ("C:\\outside.omrtemplate", "INVALID_MANAGED_PATH"),
        ("profile.txt", "INVALID_DEFAULT_PROFILE"),
        (".omrtemplate", "INVALID_DEFAULT_PROFILE"),
        ("NUL.omrtemplate", "INVALID_MANAGED_PATH"),
    ],
)
def test_profile_path_rejects_unsafe_or_non_profile_names(
    tmp_path: Path, profile: str, expected_code: str
) -> None:
    result = ManagedPaths.from_root(tmp_path).profile_path(profile)

    assert isinstance(result, Err)
    assert result.errors[0].code == expected_code


@pytest.mark.parametrize(
    ("profile", "expected_code"),
    [
        ("profile.omrtemplate.txt", "INVALID_DEFAULT_PROFILE"),
        ("profile\x1f.omrtemplate", "INVALID_MANAGED_PATH"),
        ("CON.omrtemplate", "INVALID_MANAGED_PATH"),
        ("LPT9.backup.omrtemplate", "INVALID_MANAGED_PATH"),
        ("profile.omrtemplate:archive", "INVALID_MANAGED_PATH"),
        (r"\\server\share\profile.omrtemplate", "INVALID_MANAGED_PATH"),
    ],
)
def test_profile_path_rejects_portable_windows_filename_hazards(
    tmp_path: Path, profile: str, expected_code: str
) -> None:
    result = ManagedPaths.from_root(tmp_path).profile_path(profile)

    assert isinstance(result, Err)
    assert result.errors[0].code == expected_code


@pytest.mark.parametrize(
    "component", ["..", "child/grandchild", "child\\grandchild", "C:drive", "name."]
)
def test_data_path_rejects_unsafe_components(tmp_path: Path, component: str) -> None:
    result = ManagedPaths.from_root(tmp_path).data_path(component)

    assert isinstance(result, Err)
    assert result.errors[0].code == "INVALID_MANAGED_PATH"


@pytest.mark.parametrize(
    "component",
    ["data\x00", "AUX.backup", "answers.json:stream", r"\\server\share", "name "],
)
def test_data_path_rejects_portable_windows_filename_hazards(
    tmp_path: Path, component: str
) -> None:
    result = ManagedPaths.from_root(tmp_path).data_path(component)

    assert isinstance(result, Err)
    assert result.errors[0].code == "INVALID_MANAGED_PATH"


@pytest.mark.parametrize("managed_entry", ["Profiles", "OMR_Grader"])
def test_managed_paths_reject_escaping_symlink_entries(tmp_path: Path, managed_entry: str) -> None:
    root = tmp_path / "portable"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / managed_entry
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EPERM} or getattr(exc, "winerror", None) == 1314:
            pytest.skip(f"directory symlinks require unavailable host privileges: {exc}")
        raise

    paths = ManagedPaths.from_root(root)
    result = (
        paths.profile_path("profile.omrtemplate")
        if managed_entry == "Profiles"
        else paths.data_path("session.json")
    )

    assert isinstance(result, Err)
    assert result.errors[0].code == "MANAGED_PATH_INVALID"


def test_managed_paths_reject_reparse_point_managed_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = ManagedPaths.from_root(tmp_path)
    monkeypatch.setattr(
        paths_module,
        "_is_reparse_point",
        lambda path: path == paths.profiles_dir,
    )

    result = paths.profile_path("profile.omrtemplate")

    assert isinstance(result, Err)
    assert result.errors[0].code == "MANAGED_PATH_INVALID"
