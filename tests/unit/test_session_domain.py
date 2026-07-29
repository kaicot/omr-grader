from __future__ import annotations

from omr_grader.domain.enums import SourceKind
from omr_grader.domain.errors import Err, Ok
from omr_grader.domain.session import build_page_ref, stable_work_item_id, validate_lineage

SHA = "a" * 64


def test_page_ref_identity_ignores_display_case_and_unicode_form() -> None:
    first = build_page_ref(
        session_id="session",
        source_kind=SourceKind.PDF,
        source_sha256=SHA,
        source_display_name="Café.pdf",
        page_number=1,
        frame_number=None,
        input_ordinal=0,
        duplicate_ordinal=0,
    )
    second = build_page_ref(
        session_id="session",
        source_kind=SourceKind.PDF,
        source_sha256=SHA,
        source_display_name="CAFÉ.PDF",
        page_number=1,
        frame_number=None,
        input_ordinal=0,
        duplicate_ordinal=0,
    )
    assert isinstance(first, Ok)
    assert isinstance(second, Ok)
    assert first.value.work_item_id == second.value.work_item_id
    assert first.value.artifact_stem.startswith("Café_pdf_")


def test_identical_bytes_are_distinguished_by_input_and_duplicate_ordinals() -> None:
    first = stable_work_item_id("session", SourceKind.IMAGE, SHA, None, None, 0, 0)
    same = stable_work_item_id("session", SourceKind.IMAGE, SHA, None, None, 0, 0)
    duplicate = stable_work_item_id("session", SourceKind.IMAGE, SHA, None, None, 1, 1)
    assert isinstance(first, Ok)
    assert isinstance(same, Ok)
    assert isinstance(duplicate, Ok)
    assert first.value == same.value
    assert first.value != duplicate.value
    assert first.value.startswith("wi_") and len(first.value) == 27


def test_labels_and_stems_are_deterministic_and_windows_safe() -> None:
    result = build_page_ref(
        session_id="session",
        source_kind=SourceKind.IMAGE,
        source_sha256=SHA,
        source_display_name="CON: one?.png",
        page_number=None,
        frame_number=None,
        input_ordinal=0,
        duplicate_ordinal=1,
    )
    assert isinstance(result, Ok)
    assert result.value.source_label == "CON: one?.png (2)"
    assert ":" not in result.value.artifact_stem
    assert "?" not in result.value.artifact_stem
    assert len(result.value.artifact_stem.split("_", 1)[0]) <= 48


def test_lineage_requires_a_generation_for_current() -> None:
    # The public validator rejects an authority pointer without immutable generation truth.
    from omr_grader.domain.models import CurrentPointer

    current = CurrentPointer(
        1, "session", 1, "generation", "generations/generation", SHA, "2026-01-01T00:00:00.000000Z"
    )
    result = validate_lineage((), current)
    assert isinstance(result, Err)
    assert result.errors[0].code == "LINEAGE_EMPTY"
