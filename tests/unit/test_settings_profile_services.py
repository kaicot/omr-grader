from __future__ import annotations

import json
from pathlib import Path

from omr_grader.application.dto import (
    CollisionPolicy,
    ProfileImportRequest,
    Settings,
    SettingsSaveCommand,
)
from omr_grader.application.profile_use_case import ProfileApplicationService
from omr_grader.application.settings_use_case import SettingsApplicationService
from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure.capabilities import CapabilityToken
from omr_grader.infrastructure.config_store import AppConfig, load_config_snapshot, save_config
from omr_grader.infrastructure.paths import ManagedPaths
from omr_grader.infrastructure.profile_store import ProfileStore


def _profile() -> bytes:
    regions: list[dict[str, object]] = [
        {
            "name": "id",
            "type": "id",
            "bbox_ratio": {"x": 0, "y": 0, "w": 0.1, "h": 0.1},
            "grid": {"cols": 8, "rows": 10},
        }
    ]
    regions += [
        {
            "name": f"answer{index}",
            "type": "answer",
            "bbox_ratio": {"x": index / 5, "y": 0.2, "w": 0.1, "h": 0.7},
            "grid": {"cols": 5, "rows": 20},
        }
        for index in range(5)
    ]
    return json.dumps(
        {
            "schema_version": 1,
            "profile_name": "test",
            "page": {
                "orientation": "landscape",
                "aspect_ratio": 1.4,
                "source_width": 1400,
                "source_height": 1000,
            },
            "regions": regions,
        }
    ).encode()


def _services(tmp_path: Path) -> tuple[ManagedPaths, SettingsApplicationService]:
    paths = ManagedPaths.from_root(tmp_path)
    paths.profiles_dir.mkdir()
    token = CapabilityToken.for_testing(paths.root)
    profiles = ProfileApplicationService(ProfileStore(paths, token))
    return paths, SettingsApplicationService(paths, token, profiles)


def test_shared_catalog_keeps_invalid_candidates_but_screen_one_stays_valid_only(
    tmp_path: Path,
) -> None:
    paths, settings = _services(tmp_path)
    (paths.profiles_dir / "valid.omrtemplate").write_bytes(_profile())
    (paths.profiles_dir / "invalid.omrtemplate").write_bytes(b"[]")

    catalog = settings.profiles.profile_catalog()
    assert isinstance(catalog, Ok)
    assert [(item.filename, item.is_valid) for item in catalog.value] == [
        ("invalid.omrtemplate", False),
        ("valid.omrtemplate", True),
    ]
    assert catalog.value[0].diagnostics
    assert settings.profiles.discover_profiles() == Ok(("valid.omrtemplate",))


def test_import_visible_to_shared_catalog_without_stale_cache(tmp_path: Path) -> None:
    paths, settings = _services(tmp_path)
    source = tmp_path / "new.omrtemplate"
    source.write_bytes(_profile())

    imported = settings.profiles.import_profile(
        ProfileImportRequest(str(source), CollisionPolicy.ERROR, None, "import")
    )
    catalog = settings.profiles.profile_catalog()
    assert isinstance(imported, Ok)
    assert isinstance(catalog, Ok)
    assert [item.filename for item in catalog.value] == ["new.omrtemplate"]


def test_missing_configured_default_warns_and_is_not_selected(tmp_path: Path) -> None:
    paths, settings = _services(tmp_path)
    paths.config_path.write_text(json.dumps(AppConfig("gone.omrtemplate", 4, False).to_dict()))

    loaded = settings.load_settings()
    assert isinstance(loaded, Ok)
    assert loaded.value.settings == Settings("gone.omrtemplate", 4, False)
    assert [warning.code for warning in loaded.warnings] == ["DEFAULT_PROFILE_UNAVAILABLE"]


def test_settings_save_uses_full_content_revision_and_persists_only_three_keys(
    tmp_path: Path,
) -> None:
    paths, settings = _services(tmp_path)
    paths.config_path.write_text(
        json.dumps(AppConfig("", 3, True).to_dict()), encoding="utf-8"
    )
    initial = settings.load_settings()
    assert isinstance(initial, Ok)
    stale_revision = initial.value.revision
    first = settings.save_settings(
        SettingsSaveCommand(Settings("", 5, False), stale_revision, "first")
    )
    assert isinstance(first, Ok)
    assert set(json.loads(paths.config_path.read_text(encoding="utf-8"))) == {
        "default_profile",
        "default_sensitivity",
        "use_multiprocessing",
    }
    stale = settings.save_settings(
        SettingsSaveCommand(Settings("", 6, True), stale_revision, "stale")
    )
    assert isinstance(stale, Err)
    assert stale.errors[0].code == "CONFIG_REVISION_CONFLICT"


def test_config_snapshot_relocates_with_portable_root(tmp_path: Path) -> None:
    root = tmp_path / "original"
    root.mkdir()
    original, _ = _services(root)
    token = CapabilityToken.for_testing(original.root)
    assert isinstance(save_config(original, AppConfig("", 8, True), token), Ok)
    moved = tmp_path / "moved"
    original.root.rename(moved)

    relocated = ManagedPaths.from_root(moved)
    loaded = load_config_snapshot(relocated)
    assert isinstance(loaded, Ok)
    assert loaded.value.config == AppConfig("", 8, True)
