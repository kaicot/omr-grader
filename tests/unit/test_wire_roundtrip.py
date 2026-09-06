from __future__ import annotations

import json
import unicodedata
from typing import Any

import pytest

from omr_grader.domain.enums import (
    AnswerKeySnapshotKind,
    AnswerStatus,
    ArchiveLineageMode,
    CreationKind,
    ExamTerm,
    KeyQuestionStatus,
    LineageState,
    OperationKind,
    RosterSnapshotKind,
    SessionState,
    TargetKind,
)
from omr_grader.domain.models import (
    AnswerKeyEntry,
    AnswerKeySnapshot,
    AnswerValue,
    ArchiveEntry,
    ArchiveManifest,
    CorrectionDraft,
    CurrentPointer,
    DashboardIndexEntry,
    DashboardIndexRecord,
    DeleteTombstone,
    IdentityRecord,
    ManifestFile,
    ManifestSummary,
    OmittedParent,
    RestoreProvenance,
    RosterSnapshot,
    SessionManifest,
    SessionRecord,
    SessionReservation,
)

TIMESTAMP = "2026-07-27T12:34:56.789012Z"
SHA256 = "a" * 64
PARENT_SHA256 = "b" * 64


def canonical_json(value: object) -> bytes:
    """The persisted JSON encoding: NFC UTF-8, sorted compact keys, one final LF."""
    text = unicodedata.normalize(
        "NFC", json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return text.encode("utf-8") + b"\n"


def unasked_key_entries() -> tuple[AnswerKeyEntry, ...]:
    return tuple(
        AnswerKeyEntry(
            question, AnswerValue((), AnswerStatus.UNASKED), "0", KeyQuestionStatus.UNASKED
        )
        for question in range(1, 101)
    )


def manifest(files: tuple[ManifestFile, ...] | None = None) -> SessionManifest:
    return SessionManifest(
        1,
        "session-1",
        1,
        "generation-1",
        None,
        None,
        None,
        "operation-1",
        OperationKind.CREATE,
        "0.1.0",
        TIMESTAMP,
        SessionState.CREATED,
        (),
        None,
        SHA256,
        SHA256,
        None,
        None,
        files or (ManifestFile("records/a.json", 0, SHA256, "application/json"),),
        ManifestSummary(0, 0, 0, None),
    )


def wire_instances() -> tuple[Any, ...]:
    entry = DashboardIndexEntry(
        "session-1",
        1,
        "generation-1",
        SHA256,
        "시험",
        "중간고사",
        2026,
        ExamTerm.FIRST,
        SessionState.CREATED,
        None,
        0,
        None,
        None,
        None,
        0,
    )
    omitted = OmittedParent(1, "generation-1", PARENT_SHA256)
    archive_entry = ArchiveEntry("generations/1/manifest.json", "application/json", 0, SHA256)
    return (
        RosterSnapshot(1, RosterSnapshotKind.NONE, None, None, None, "v1", (), ()),
        AnswerKeySnapshot(
            1, AnswerKeySnapshotKind.UNSET, None, None, None, "v1", unasked_key_entries(), ()
        ),
        manifest(),
        CurrentPointer(1, "session-1", 1, "generation-1", "generations/1", SHA256, TIMESTAMP),
        DashboardIndexRecord(1, TIMESTAMP, SHA256, (entry,)),
        SessionRecord(
            1,
            "session-1",
            1,
            SessionState.CREATED,
            "중간고사",
            2026,
            ExamTerm.FIRST,
            TIMESTAMP,
            None,
            TIMESTAMP,
        ),
        IdentityRecord(1, "session-1", TIMESTAMP, CreationKind.SCAN),
        SessionReservation(1, "session-1", "operation-1", CreationKind.SCAN, TIMESTAMP, "중간고사"),
        DeleteTombstone(1, "session-1", "operation-delete", TIMESTAMP, ("generation-1",)),
        RestoreProvenance(
            1,
            "session-1",
            SHA256,
            2,
            "generation-2",
            SHA256,
            omitted,
            TIMESTAMP,
            LineageState.VALID_TRUNCATED_ANCESTOR,
        ),
        archive_entry,
        ArchiveManifest(
            1,
            1,
            "0.1.0",
            TIMESTAMP,
            ArchiveLineageMode.CURRENT_ONLY,
            "session-1",
            2,
            "generation-2",
            SHA256,
            omitted,
            True,
            (archive_entry,),
        ),
        ManifestSummary(2, 2, 1, "100"),
    )


def expected_wire(instance: Any) -> dict[str, object]:
    key_entries = [
        {
            "question": question,
            "answer": {"choices": [], "status": "unasked"},
            "points": "0",
            "status": "unasked",
        }
        for question in range(1, 101)
    ]
    expected: dict[type[Any], dict[str, object]] = {
        RosterSnapshot: {
            "schema_version": 1,
            "snapshot_kind": "none",
            "source_name": None,
            "source_sha256": None,
            "sheet_name": None,
            "normalization_version": "v1",
            "rows": [],
            "validation_errors": [],
            "extensions": {},
        },
        AnswerKeySnapshot: {
            "schema_version": 1,
            "snapshot_kind": "unset",
            "source_name": None,
            "source_sha256": None,
            "sheet_name": None,
            "normalization_version": "v1",
            "entries": key_entries,
            "validation_errors": [],
            "extensions": {},
        },
        SessionManifest: {
            "schema_version": 1,
            "session_id": "session-1",
            "revision": 1,
            "generation_id": "generation-1",
            "parent_revision": None,
            "parent_generation_id": None,
            "parent_manifest_sha256": None,
            "operation_id": "operation-1",
            "operation_kind": "create",
            "app_version": "0.1.0",
            "created_at": TIMESTAMP,
            "state": "created",
            "base_response_ids": [],
            "profile_sha256": None,
            "roster_sha256": SHA256,
            "key_sha256": SHA256,
            "threshold_version": None,
            "threshold_sha256": None,
            "files": [
                {
                    "path": "records/a.json",
                    "size": 0,
                    "sha256": SHA256,
                    "media_type": "application/json",
                }
            ],
            "summary": {
                "work_items": 0,
                "processable": 0,
                "manual_review": 0,
                "maximum_score": None,
            },
        },
        CurrentPointer: {
            "schema_version": 1,
            "session_id": "session-1",
            "revision": 1,
            "generation_id": "generation-1",
            "generation_relpath": "generations/1",
            "manifest_sha256": SHA256,
            "committed_at": TIMESTAMP,
        },
        DashboardIndexRecord: {
            "schema_version": 1,
            "built_at": TIMESTAMP,
            "source_digest": SHA256,
            "entries": [
                {
                    "session_id": "session-1",
                    "revision": 1,
                    "generation_id": "generation-1",
                    "manifest_sha256": SHA256,
                    "display_folder": "시험",
                    "exam_name": "중간고사",
                    "exam_year": 2026,
                    "exam_term": "first",
                    "state": "created",
                    "graded_at": None,
                    "participant_count": 0,
                    "average_score": None,
                    "highest_score": None,
                    "lowest_score": None,
                    "needs_review_count": 0,
                }
            ],
        },
        SessionRecord: {
            "schema_version": 1,
            "session_id": "session-1",
            "revision": 1,
            "state": "created",
            "exam_name": "중간고사",
            "exam_year": 2026,
            "exam_term": "first",
            "created_at": TIMESTAMP,
            "graded_at": None,
            "updated_at": TIMESTAMP,
        },
        IdentityRecord: {
            "schema_version": 1,
            "session_id": "session-1",
            "created_at": TIMESTAMP,
            "creation_kind": "scan",
        },
        SessionReservation: {
            "schema_version": 1,
            "session_id": "session-1",
            "operation_id": "operation-1",
            "creation_kind": "scan",
            "created_at": TIMESTAMP,
            "display_name": "중간고사",
        },
        DeleteTombstone: {
            "schema_version": 1,
            "session_id": "session-1",
            "operation_id": "operation-delete",
            "committed_at": TIMESTAMP,
            "generation_ids": ["generation-1"],
        },
        RestoreProvenance: {
            "schema_version": 1,
            "session_id": "session-1",
            "archive_sha256": SHA256,
            "boundary_revision": 2,
            "boundary_generation_id": "generation-2",
            "boundary_manifest_sha256": SHA256,
            "omitted_parent": {
                "revision": 1,
                "generation_id": "generation-1",
                "manifest_sha256": PARENT_SHA256,
            },
            "restored_at": TIMESTAMP,
            "lineage_state": "valid_truncated_ancestor",
        },
        ArchiveEntry: {
            "path": "generations/1/manifest.json",
            "media_type": "application/json",
            "size": 0,
            "sha256": SHA256,
        },
        ArchiveManifest: {
            "schema_version": 1,
            "format_version": 1,
            "app_version": "0.1.0",
            "exported_at": TIMESTAMP,
            "lineage_mode": "current_only",
            "session_id": "session-1",
            "revision": 2,
            "generation_id": "generation-2",
            "manifest_sha256": SHA256,
            "omitted_parent": {
                "revision": 1,
                "generation_id": "generation-1",
                "manifest_sha256": PARENT_SHA256,
            },
            "contains_personal_data": True,
            "entries": [
                {
                    "path": "generations/1/manifest.json",
                    "media_type": "application/json",
                    "size": 0,
                    "sha256": SHA256,
                }
            ],
        },
        ManifestSummary: {
            "work_items": 2,
            "processable": 2,
            "manual_review": 1,
            "maximum_score": "100",
        },
    }
    return expected[type(instance)]


@pytest.mark.parametrize("instance", wire_instances(), ids=lambda item: type(item).__name__)
def test_persisted_wires_match_independent_fixed_goldens(instance: Any) -> None:
    expected = expected_wire(instance)
    assert instance.to_dict() == expected
    assert type(instance).from_dict(expected) == instance
    with pytest.raises(ValueError, match="wire fields"):
        type(instance).from_dict(expected | {"unknown": True})
    if "schema_version" in expected:
        with pytest.raises(ValueError, match="schema version"):
            type(instance).from_dict(expected | {"schema_version": None})


def test_representative_canonical_bytes_are_fixed_independent_contracts() -> None:
    correction = CorrectionDraft(
        "work-1",
        TargetKind.ANSWER_CELL,
        1,
        AnswerValue((1,), AnswerStatus.NORMAL),
        AnswerValue((1,), AnswerStatus.NORMAL),
        "correction",
    )
    records = (
        (
            CurrentPointer(1, "session-1", 1, "generation-1", "generations/1", SHA256, TIMESTAMP),
            b'{"committed_at":"2026-07-27T12:34:56.789012Z","generation_id":"generation-1","generation_relpath":"generations/1","manifest_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","revision":1,"schema_version":1,"session_id":"session-1"}\n',
        ),
        (
            ArchiveEntry("generations/1/manifest.json", "application/json", 0, SHA256),
            b'{"media_type":"application/json","path":"generations/1/manifest.json","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","size":0}\n',
        ),
        (
            correction,
            b'{"after":{"choices":[1],"status":"normal"},"before":{"choices":[1],"status":"normal"},"reason":"correction","target_key":1,"target_kind":"answer_cell","work_item_id":"work-1"}\n',
        ),
    )

    for record, expected in records:
        assert canonical_json(record.to_dict()) == expected


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.json",
        "../escape.json",
        "a//b",
        "a/../b",
        "C:/a",
        "a\\b",
        "a./b",
        "CON.json",
        "CONIN$.json",
        "CONOUT$.json",
        "COM¹.backup",
        "LPT².backup",
        "aux ",
        "a/<bad>.json",
        "e\u0301.json",
    ],
)
def test_manifest_file_paths_reject_portable_and_windows_aliases(path: str) -> None:
    with pytest.raises(ValueError, match="path|unsafe"):
        ManifestFile(path, 0, SHA256, "application/json")


def test_manifest_requires_hashes_immediate_parent_and_casefold_unique_paths() -> None:
    with pytest.raises(ValueError, match="sha256"):
        SessionManifest(
            1,
            "session-1",
            1,
            "generation-1",
            None,
            None,
            None,
            "operation-1",
            OperationKind.CREATE,
            "0.1.0",
            TIMESTAMP,
            SessionState.CREATED,
            (),
            None,
            None,
            SHA256,
            None,
            None,
            (),
            ManifestSummary(0, 0, 0, None),
        )
    with pytest.raises(ValueError, match="immediately"):
        SessionManifest(
            1,
            "session-1",
            3,
            "generation-3",
            1,
            "generation-1",
            SHA256,
            "operation-3",
            OperationKind.CREATE,
            "0.1.0",
            TIMESTAMP,
            SessionState.CREATED,
            (),
            None,
            SHA256,
            SHA256,
            None,
            None,
            (),
            ManifestSummary(0, 0, 0, None),
        )
    with pytest.raises(ValueError, match="case-insensitive"):
        manifest(
            (
                ManifestFile("A.json", 0, SHA256, "application/json"),
                ManifestFile("a.json", 0, SHA256, "application/json"),
            )
        )


@pytest.mark.parametrize(
    "summary",
    [
        (True, 0, 0, None),
        (1, 2, 0, None),
        (1, 0, 2, None),
        (1, 1, 0, "-1"),
    ],
)
def test_manifest_summary_rejects_invalid_counts_and_scores(
    summary: tuple[object, object, object, object],
) -> None:
    with pytest.raises(ValueError):
        ManifestSummary(*summary)  # type: ignore[arg-type]


def test_session_manifest_requires_a_manifest_summary_value() -> None:
    with pytest.raises(ValueError, match="ManifestSummary"):
        SessionManifest(
            1,
            "session-1",
            1,
            "generation-1",
            None,
            None,
            None,
            "operation-1",
            OperationKind.CREATE,
            "0.1.0",
            TIMESTAMP,
            SessionState.CREATED,
            (),
            None,
            SHA256,
            SHA256,
            None,
            None,
            (),
            object(),  # type: ignore[arg-type]
        )


def test_restore_and_archive_omitted_parent_are_immediate_and_complete() -> None:
    omitted = OmittedParent(1, "generation-1", SHA256)
    with pytest.raises(ValueError, match="omitted parent"):
        RestoreProvenance(
            1,
            "session-1",
            SHA256,
            1,
            "generation-1",
            SHA256,
            omitted,
            TIMESTAMP,
            LineageState.VALID_TRUNCATED_ANCESTOR,
        )
    with pytest.raises(ValueError, match="omitted parent"):
        ArchiveManifest(
            1,
            1,
            "0.1.0",
            TIMESTAMP,
            ArchiveLineageMode.CURRENT_ONLY,
            "session-1",
            2,
            "generation-2",
            SHA256,
            OmittedParent(2, "generation-2", SHA256),
            False,
            (),
        )
    with pytest.raises(ValueError, match="omitted parent"):
        RestoreProvenance(
            1,
            "session-1",
            SHA256,
            2,
            "generation-2",
            SHA256,
            None,
            TIMESTAMP,
            LineageState.VALID_TRUNCATED_ANCESTOR,
        )
    with pytest.raises(ValueError, match="omitted parent"):
        ArchiveManifest(
            1,
            1,
            "0.1.0",
            TIMESTAMP,
            ArchiveLineageMode.CURRENT_ONLY,
            "session-1",
            2,
            "generation-2",
            SHA256,
            None,
            False,
            (),
        )
    assert (
        RestoreProvenance(
            1,
            "session-1",
            SHA256,
            1,
            "generation-1",
            SHA256,
            None,
            TIMESTAMP,
            LineageState.VALID_TRUNCATED_ANCESTOR,
        ).omitted_parent
        is None
    )
    assert RestoreProvenance(
        1,
        "session-1",
        SHA256,
        2,
        "generation-2",
        SHA256,
        OmittedParent(1, "generation-1", PARENT_SHA256),
        TIMESTAMP,
        LineageState.VALID_TRUNCATED_ANCESTOR,
    ).omitted_parent == OmittedParent(1, "generation-1", PARENT_SHA256)
    assert (
        ArchiveManifest(
            1,
            1,
            "0.1.0",
            TIMESTAMP,
            ArchiveLineageMode.CURRENT_ONLY,
            "session-1",
            1,
            "generation-1",
            SHA256,
            None,
            False,
            (),
        ).omitted_parent
        is None
    )
    assert ArchiveManifest(
        1,
        1,
        "0.1.0",
        TIMESTAMP,
        ArchiveLineageMode.CURRENT_ONLY,
        "session-1",
        2,
        "generation-2",
        SHA256,
        OmittedParent(1, "generation-1", PARENT_SHA256),
        False,
        (),
    ).omitted_parent == OmittedParent(1, "generation-1", PARENT_SHA256)
