from __future__ import annotations

import json
from pathlib import Path

from omr_grader.application.dto import CollisionPolicy, ProfileImportRequest
from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure.capabilities import CapabilityToken
from omr_grader.infrastructure.paths import ManagedPaths
from omr_grader.infrastructure.profile_store import ProfileStore


def _payload(name: str = "OMR") -> bytes:
    regions: list[dict[str, object]] = [
        {
            "name": "id",
            "type": "id",
            "bbox_ratio": {"x": 0, "y": 0, "w": 0.1, "h": 0.1},
            "grid": {"cols": 8, "rows": 10},
        }
    ]
    regions.extend(
        {
            "name": f"a{index}",
            "type": "answer",
            "bbox_ratio": {"x": index / 5, "y": 0.2, "w": 0.1, "h": 0.7},
            "grid": {"cols": 5, "rows": 20},
        }
        for index in range(5)
    )
    return json.dumps(
        {
            "schema_version": 1,
            "profile_name": name,
            "page": {
                "orientation": "landscape",
                "aspect_ratio": 1.4,
                "source_width": 1400,
                "source_height": 1000,
            },
            "regions": regions,
        }
    ).encode()


def _store(tmp_path: Path) -> ProfileStore:
    paths = ManagedPaths.from_root(tmp_path)
    paths.profiles_dir.mkdir()
    return ProfileStore(paths, CapabilityToken.for_testing(tmp_path))


def test_import_validates_then_atomically_stores_and_discovers(tmp_path: Path) -> None:
    source = tmp_path / "external.omrtemplate"
    source.write_bytes(_payload())
    store = _store(tmp_path)

    imported = store.import_profile(
        ProfileImportRequest(str(source), CollisionPolicy.ERROR, None, "capability")
    )

    assert isinstance(imported, Ok)
    assert imported.value.stored_name == "external.omrtemplate"
    assert (tmp_path / "Profiles" / "external.omrtemplate").read_bytes() == source.read_bytes()
    assert store.discover() == Ok(("external.omrtemplate",))


def test_collision_policies_and_safe_rename_are_enforced(tmp_path: Path) -> None:
    source = tmp_path / "external.omrtemplate"
    source.write_bytes(_payload("fresh"))
    store = _store(tmp_path)
    destination = tmp_path / "Profiles" / source.name
    destination.write_bytes(_payload("old"))

    rejected = store.import_profile(
        ProfileImportRequest(str(source), CollisionPolicy.ERROR, None, "capability")
    )
    replaced = store.import_profile(
        ProfileImportRequest(str(source), CollisionPolicy.REPLACE, None, "capability")
    )
    renamed = store.import_profile(
        ProfileImportRequest(
            str(source), CollisionPolicy.RENAME, "renamed.omrtemplate", "capability"
        )
    )
    unsafe = store.import_profile(
        ProfileImportRequest(
            str(source), CollisionPolicy.RENAME, "../outside.omrtemplate", "capability"
        )
    )

    assert isinstance(rejected, Err)
    assert isinstance(replaced, Ok)
    assert destination.read_bytes() == source.read_bytes()
    assert isinstance(renamed, Ok)
    assert (tmp_path / "Profiles" / "renamed.omrtemplate").exists()
    assert isinstance(unsafe, Err)


def test_invalid_source_preserves_existing_destination_and_invalid_default_warns(
    tmp_path: Path,
) -> None:
    source = tmp_path / "external.omrtemplate"
    source.write_bytes(b"not json")
    store = _store(tmp_path)
    destination = tmp_path / "Profiles" / source.name
    original = _payload("existing")
    destination.write_bytes(original)

    imported = store.import_profile(
        ProfileImportRequest(str(source), CollisionPolicy.REPLACE, None, "capability")
    )
    default = store.default_profile(destination.name)

    assert isinstance(imported, Err)
    assert destination.read_bytes() == original
    assert isinstance(default, Ok)
    assert default.value is not None


def test_discovery_excludes_invalid_profiles(tmp_path: Path) -> None:
    store = _store(tmp_path)
    (tmp_path / "Profiles" / "valid.omrtemplate").write_bytes(_payload())
    (tmp_path / "Profiles" / "invalid.omrtemplate").write_bytes(b"[]")

    discovered = store.discover()

    assert discovered == Ok(("valid.omrtemplate",))
