from __future__ import annotations

import json
from pathlib import Path

from omr_grader.application.dto import CollisionPolicy, ProfileImportRequest
from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.profile import parse_profile_bytes
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


def _form_payload(name: str, answer_rows: tuple[int, ...]) -> bytes:
    """A generated-style profile: an ID grid and answer blocks of the given row counts."""
    regions: list[dict[str, object]] = [
        {
            "name": "id",
            "type": "id",
            "bbox_ratio": {"x": 0.05, "y": 0.1, "w": 0.15, "h": 0.5},
            "grid": {"cols": 8, "rows": 10},
        }
    ]
    start = 1
    for index, rows in enumerate(answer_rows):
        regions.append(
            {
                "name": f"q{start:03d}_{start + rows - 1:03d}",
                "type": "answer",
                "bbox_ratio": {"x": 0.25 + index * 0.15, "y": 0.1, "w": 0.12, "h": 0.8},
                "grid": {"cols": 5, "rows": rows},
                "question_start": start,
            }
        )
        start += rows
    page = {"orientation": "landscape", "aspect_ratio": 1.4, "source_width": 1400}
    page["source_height"] = 1000
    return json.dumps(
        {"schema_version": 1, "profile_name": name, "page": page, "regions": regions}
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


def test_save_generated_stores_a_validated_profile_under_the_requested_name(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    payload = _form_payload("자동 인식 양식", (20, 20, 20, 20, 20))
    name = "자동양식_객관식100문항_abc123.omrtemplate"

    saved = store.save_generated(payload, name)

    assert isinstance(saved, Ok)
    assert saved.value.stored_name == name
    parsed = parse_profile_bytes(payload)
    assert isinstance(parsed, Ok)
    assert saved.value.profile_sha256 == parsed.value.sha256
    assert (tmp_path / "Profiles" / name).read_bytes() == payload
    assert store.discover() == Ok((name,))
    loaded = store.load(name)
    assert isinstance(loaded, Ok) and loaded.value == parsed.value


def test_save_generated_keeps_forms_with_fewer_than_one_hundred_questions(tmp_path: Path) -> None:
    store = _store(tmp_path)

    saved = store.save_generated(_form_payload("fifty", (20, 20, 10)), "fifty.omrtemplate")

    assert isinstance(saved, Ok)
    loaded = store.load("fifty.omrtemplate")
    assert isinstance(loaded, Ok)
    assert [region.grid.rows for region in loaded.value.answer_regions] == [20, 20, 10]


def test_save_generated_never_overwrites_and_numbers_the_next_free_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    profiles = tmp_path / "Profiles"
    (profiles / "auto_2.omrtemplate").write_bytes(b"someone else's file")
    payloads = [_form_payload(f"form {index}", (20,) * 5) for index in range(4)]

    names = []
    for payload in payloads:
        saved = store.save_generated(payload, "auto.omrtemplate")
        assert isinstance(saved, Ok)
        names.append(saved.value.stored_name)

    # auto_2 was taken by another file, so it is skipped and left as it was.
    assert names == [
        "auto.omrtemplate",
        "auto_3.omrtemplate",
        "auto_4.omrtemplate",
        "auto_5.omrtemplate",
    ]
    assert (profiles / "auto_2.omrtemplate").read_bytes() == b"someone else's file"
    for name, payload in zip(names, payloads, strict=True):
        assert (profiles / name).read_bytes() == payload


def test_save_generated_adds_the_extension_when_it_is_missing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = _form_payload("auto", (20,) * 5)

    first = store.save_generated(payload, "auto")
    second = store.save_generated(payload, "auto.omrtemplate")

    assert isinstance(first, Ok) and isinstance(second, Ok)
    assert first.value.stored_name == "auto.omrtemplate"
    assert second.value.stored_name == "auto_2.omrtemplate"


def test_save_generated_gives_up_when_every_numbered_name_is_taken(tmp_path: Path) -> None:
    store = _store(tmp_path)
    profiles = tmp_path / "Profiles"
    (profiles / "auto.omrtemplate").write_bytes(b"1")
    for number in range(2, 100):
        (profiles / f"auto_{number}.omrtemplate").write_bytes(b"1")

    saved = store.save_generated(_form_payload("auto", (20,) * 5), "auto.omrtemplate")

    assert isinstance(saved, Err)
    assert saved.errors[0].code == "PROFILE_COLLISION"
    assert len(list(profiles.iterdir())) == 99


def test_save_generated_rejects_invalid_profiles_and_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    too_many_questions = _form_payload("long", (20,) * 6)
    gap = json.loads(_form_payload("gap", (20, 10)))
    gap["regions"][2]["question_start"] = 22

    for payload in (b"not json", too_many_questions, json.dumps(gap).encode()):
        saved = store.save_generated(payload, "auto.omrtemplate")
        assert isinstance(saved, Err)
        assert saved.errors[0].code == "INVALID_PROFILE"
    assert list((tmp_path / "Profiles").iterdir()) == []


def test_save_generated_rejects_unsafe_names(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = _form_payload("auto", (20,) * 5)

    for name in ("../outside.omrtemplate", "a/b.omrtemplate", "..\\outside", "", "con"):
        saved = store.save_generated(payload, name)
        assert isinstance(saved, Err), name
    assert list((tmp_path / "Profiles").iterdir()) == []
    assert not (tmp_path / "outside.omrtemplate").exists()


def test_save_generated_needs_write_capability_and_a_prepared_profiles_folder(
    tmp_path: Path,
) -> None:
    payload = _form_payload("auto", (20,) * 5)
    paths = ManagedPaths.from_root(tmp_path)

    unprepared = ProfileStore(paths, CapabilityToken.for_testing(tmp_path))
    paths.profiles_dir.mkdir()
    unauthorized = ProfileStore(paths)

    denied = unauthorized.save_generated(payload, "auto.omrtemplate")
    assert isinstance(denied, Err) and denied.errors[0].code == "ROOT_WRITE_DENIED"
    assert list(paths.profiles_dir.iterdir()) == []
    paths.profiles_dir.rmdir()
    missing = unprepared.save_generated(payload, "auto.omrtemplate")
    assert isinstance(missing, Err) and missing.errors[0].code == "MANAGED_PATH_INVALID"
