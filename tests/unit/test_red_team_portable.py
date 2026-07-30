"""Adversarial checks for portable paths, durable writes, and config failures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure import atomic_io
from omr_grader.infrastructure import paths as paths_module
from omr_grader.infrastructure.atomic_io import atomic_write_bytes
from omr_grader.infrastructure.capabilities import CapabilityToken
from omr_grader.infrastructure.config_store import AppConfig, load_config, save_config
from omr_grader.infrastructure.paths import ManagedPaths, validate_component


@pytest.mark.parametrize(
    "component",
    [
        "NUL.txt",
        "con ",
        "profile.",
        "file.txt:stream",
        "..\\outside",
        "../outside",
        "C:relative-drive",
        "\\\\server\\share",
        "name\x00with-nul",
        "name\x1fwith-control",
        "report\u0301.",
    ],
)
def test_portable_components_reject_windows_aliases_and_escape_forms(component: str) -> None:
    result = validate_component(component)

    assert isinstance(result, Err)
    assert result.errors[0].code == "INVALID_MANAGED_PATH"


def test_portable_component_normalizes_unicode_without_changing_root_containment(
    tmp_path: Path,
) -> None:
    paths = ManagedPaths.from_root(tmp_path / "포터블")
    paths.root.mkdir()
    decomposed = "e\u0301.omrtemplate"

    result = paths.profile_path(decomposed)

    assert isinstance(result, Ok)
    assert result.value.name == "é.omrtemplate"
    assert result.value.is_relative_to(paths.root)


def test_portable_profile_path_rejects_an_injected_link_without_following_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = ManagedPaths.from_root(tmp_path / "portable")
    paths.root.mkdir()
    paths.profiles_dir.mkdir()

    monkeypatch.setattr(paths_module, "_is_reparse_point", lambda path: path == paths.profiles_dir)

    result = paths.profile_path("safe.omrtemplate")

    assert isinstance(result, Err)
    assert result.errors[0].code == "MANAGED_PATH_INVALID"


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_atomic_write_preserves_existing_destination_and_cleans_temp_on_durability_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    destination = tmp_path / "config.json"
    temporary = tmp_path / ".config.json.injected.tmp"
    destination.write_bytes(b"known-good")
    monkeypatch.setattr(atomic_io, "_temp_path", lambda _: temporary)

    if failure == "replace":
        monkeypatch.setattr(
            atomic_io,
            "_replace_durably",
            lambda *_: (_ for _ in ()).throw(OSError("denied")),
        )
    else:
        monkeypatch.setattr(
            atomic_io.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("sync failed"))
        )

    result = atomic_write_bytes(destination, b"uncommitted")

    assert isinstance(result, Err)
    assert result.errors[0].code == "ATOMIC_WRITE_FAILED"
    assert destination.read_bytes() == b"known-good"
    assert not temporary.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"default_profile": 1, "default_sensitivity": 3, "use_multiprocessing": True},
        {"default_profile": "", "default_sensitivity": "3", "use_multiprocessing": True},
        {"default_profile": "", "default_sensitivity": 3, "use_multiprocessing": 1},
        {"default_profile": "", "default_sensitivity": 3, "use_multiprocessing": None},
    ],
)
def test_config_scalar_type_confusion_falls_back_without_rewriting_file(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    paths = ManagedPaths.from_root(tmp_path)
    original = json.dumps(payload).encode("utf-8")
    paths.config_path.write_bytes(original)

    result = load_config(paths)

    assert isinstance(result, Ok)
    assert result.value == AppConfig("", 5, True)
    assert result.warnings[0].code == "CONFIG_INVALID"
    assert paths.config_path.read_bytes() == original


def test_config_write_denied_by_wrong_root_token_preserves_existing_config(tmp_path: Path) -> None:
    paths = ManagedPaths.from_root(tmp_path / "portable")
    paths.root.mkdir()
    paths.config_path.write_text('{"preserve":true}', encoding="utf-8")
    foreign_token = CapabilityToken.for_testing(tmp_path / "other-root")

    result = save_config(paths, AppConfig("", 3, True), foreign_token)

    assert isinstance(result, Err)
    assert result.errors[0].code == "ROOT_WRITE_DENIED"
    assert paths.config_path.read_text(encoding="utf-8") == '{"preserve":true}'
