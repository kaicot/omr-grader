"""Remind whoever adds a new kind of saved record to raise the data format.

An older program that meets a record it does not know can silently lose it (4.4 regrading an
exam whose student IDs 4.5.0 fixed would write the misread IDs back).  The data format number
in Data/FORMAT.json and in .omrbak archives makes an older program stop instead: it opens a
newer folder read-only and refuses a newer archive.  These tests fail when a new operation or
a new generation record appears without the number being raised and recorded here.
"""

from __future__ import annotations

import re
from pathlib import Path

import omr_grader.infrastructure.generation_materializer as materializer
from omr_grader.domain.enums import OperationKind
from omr_grader.domain.id_fixes import ID_FIXES_FILE
from omr_grader.domain.models import DATA_FORMAT_VERSION, ArchiveManifest
from omr_grader.infrastructure.data_format import DATA_FORMAT

# What each data format added.  Raise DATA_FORMAT_VERSION and add a row for the next change.
_OPERATIONS_BY_FORMAT = {
    1: {
        "create",
        "recognize",
        "import_responses",
        "correct",
        "regrade",
        "finalize",
        "metadata_edit",
    },
    2: {"fix_student_id"},
}
_RECORDS_BY_FORMAT = {
    1: {
        "correction_events.json",
        "correction_state.json",
        "detail_index.json",
        "projection_request.json",
        "review_geometry.json",
        "semantic_inputs.json",
    },
    2: {ID_FIXES_FILE},
}
_MESSAGE = (
    "A new kind of saved record needs DATA_FORMAT_VERSION raised (domain/models.py) and a row "
    "in this test, so older programs open newer data read-only and refuse newer backups."
)


def _known(table: dict[int, set[str]]) -> set[str]:
    return set().union(*(table[number] for number in range(1, DATA_FORMAT_VERSION + 1)))


def test_folders_and_backups_share_one_format_number() -> None:
    assert DATA_FORMAT == DATA_FORMAT_VERSION
    assert set(_OPERATIONS_BY_FORMAT) == set(_RECORDS_BY_FORMAT) == set(
        range(1, DATA_FORMAT_VERSION + 1)
    ), _MESSAGE


def test_every_operation_belongs_to_a_data_format() -> None:
    assert {kind.value for kind in OperationKind} == _known(_OPERATIONS_BY_FORMAT), _MESSAGE


def test_every_generation_record_belongs_to_a_data_format() -> None:
    source = Path(materializer.__file__).read_text(encoding="utf-8")
    written = set(re.findall(r'"([a-z_]+\.json)"', source)) | {ID_FIXES_FILE}

    assert written == _known(_RECORDS_BY_FORMAT), _MESSAGE


def test_archives_of_every_known_format_are_accepted_and_newer_ones_are_not() -> None:
    accepted = range(1, DATA_FORMAT_VERSION + 1)
    for number in (*accepted, DATA_FORMAT_VERSION + 1):
        wire = {
            "schema_version": 1,
            "format_version": number,
            "app_version": "omr-grader",
            "exported_at": "2026-10-31T00:00:00.000000Z",
            "lineage_mode": "current_only",
            "session_id": "session-1",
            "revision": 1,
            "generation_id": "generation-1",
            "manifest_sha256": "a" * 64,
            "omitted_parent": None,
            "contains_personal_data": True,
            "entries": [],
        }
        try:
            ArchiveManifest.from_dict(wire)
            refused = False
        except ValueError:
            refused = True
        assert refused is (number not in accepted), number
