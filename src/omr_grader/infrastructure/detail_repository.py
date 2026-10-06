"""Pinned, allowlisted lazy detail reader.

Opaque handle IDs deliberately keep leases and filesystem paths inside this adapter.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

from omr_grader.application.correction_use_case import CommittedCorrectionSnapshot
from omr_grader.application.dto import (
    EffectiveResponseProjection,
    ScoreInput,
    ScoreSet,
    SnapshotRef,
    SnapshotRequest,
)
from omr_grader.application.ports import CommittedSnapshotLease, InternalSessionCoordinator
from omr_grader.domain.corrections import (
    CorrectionState,
    project_correction_state,
    project_effective_responses,
    validate_correction_event_history,
)
from omr_grader.domain.enums import SnapshotPurpose
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.grading import score_effective
from omr_grader.domain.models import (
    AnswerKeySnapshot,
    AnswerRecognition,
    AutomaticPage,
    CellEvidence,
    CorrectionDraft,
    CorrectionEvent,
    EffectiveResponse,
    ImportedResponseRef,
    SessionRecord,
)
from omr_grader.domain.score_average import with_rounded_average


@dataclass(frozen=True, slots=True)
class DetailHandle:
    handle_id: str
    snapshot: SnapshotRef


@dataclass(frozen=True, slots=True)
class DetailListRow:
    work_item_id: str
    student_id: str | None
    student_name: str | None
    score: str | None
    rank: int | None
    has_image: bool


@dataclass(frozen=True, slots=True)
class WorkItemDetail:
    work_item_id: str
    payload: dict[str, object]
    image: bytes | None


@dataclass(slots=True)
class _OpenedDetail:
    lease: CommittedSnapshotLease
    rows: dict[str, dict[str, object]]
    review_geometry: dict[str, dict[str, object]]


def _require_snapshot(value: object, snapshot: SnapshotRef) -> None:
    """Bind detail documents to the generation; manifest verifies their bytes.

    A target manifest digest cannot appear inside a manifest-listed detail file
    without creating a hash cycle.
    """
    payload = _mapping(value)
    if set(payload) != {"session_id", "revision", "generation_id"}:
        raise ValueError("snapshot identity envelope is invalid")
    if (
        payload["session_id"] != snapshot.session_id
        or payload["revision"] != snapshot.revision
        or payload["generation_id"] != snapshot.generation_id
    ):
        raise ValueError("snapshot identity does not match pinned lease")


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("detail JSON array is invalid")
    return value


def _decode_projection(
    document: dict[str, object], lease: CommittedSnapshotLease
) -> EffectiveResponseProjection:
    if set(document) != {"schema_version", "base_snapshot", "base_response_ids", "projection"}:
        raise ValueError("projection request envelope is invalid")
    if document["schema_version"] != 1:
        raise ValueError("unsupported projection request schema")
    manifest = lease.manifest
    base_snapshot = _mapping(document["base_snapshot"])
    if set(base_snapshot) != {"session_id", "revision", "generation_id", "manifest_sha256"}:
        raise ValueError("projection base snapshot is invalid")
    if (
        base_snapshot["session_id"] != lease.snapshot_ref.session_id
        or base_snapshot["revision"] != manifest.parent_revision
        or base_snapshot["generation_id"] != manifest.parent_generation_id
        or base_snapshot["manifest_sha256"] != manifest.parent_manifest_sha256
    ):
        raise ValueError("projection base snapshot does not match pinned revision")
    base_response_ids = _list(document["base_response_ids"])
    if (
        not all(isinstance(item, str) for item in base_response_ids)
        or tuple(base_response_ids) != manifest.base_response_ids
    ):
        raise ValueError("projection base response IDs do not match manifest")
    payload = _mapping(document["projection"])
    if set(payload) != {"automatic_pages", "imported_responses", "corrections"}:
        raise ValueError("projection request fields are invalid")
    return EffectiveResponseProjection(
        tuple(
            AutomaticPage.from_dict(_mapping(value)) for value in _list(payload["automatic_pages"])
        ),
        tuple(
            ImportedResponseRef.from_dict(_mapping(value))
            for value in _list(payload["imported_responses"])
        ),
        tuple(
            CorrectionDraft.from_dict(_mapping(value)) for value in _list(payload["corrections"])
        ),
    )


def _decode_correction_events(value: object) -> tuple[CorrectionEvent, ...]:
    document = _mapping(value)
    if set(document) != {"schema_version", "events"} or document["schema_version"] != 1:
        raise ValueError("correction event envelope is invalid")
    return tuple(CorrectionEvent.from_dict(_mapping(event)) for event in _list(document["events"]))


def _read_review_geometry(lease: CommittedSnapshotLease) -> dict[str, dict[str, object]]:
    declared = _manifest_declares(lease, "review_geometry.json")
    source = lease.open_allowlisted("review_geometry.json")
    if declared != (not isinstance(source, Err)):
        raise ValueError("declared review geometry is missing or unlisted")
    if not declared:
        return {}
    assert not isinstance(source, Err)
    with source.value:
        document = _mapping(json.load(source.value))
    if set(document) != {"schema_version", "pages"} or document["schema_version"] != 1:
        raise ValueError("review geometry envelope is invalid")
    pages = _list(document["pages"])
    result: dict[str, dict[str, object]] = {}
    for raw in pages:
        page = _mapping(raw)
        if set(page) != {"work_item_id", "evidence", "answers"}:
            raise ValueError("review geometry page is invalid")
        work_item_id = page["work_item_id"]
        evidence = page["evidence"]
        answers = page["answers"]
        if (
            not isinstance(work_item_id, str)
            or not work_item_id
            or not isinstance(evidence, list)
            or not isinstance(answers, list)
            or work_item_id in result
        ):
            raise ValueError("review geometry page is invalid")
        # Parse every typed value before exposing the raw, manifest-authenticated
        # geometry to the detail payload; no AutomaticPage is reconstructed.
        tuple(CellEvidence.from_dict(_mapping(item)) for item in evidence)
        tuple(AnswerRecognition.from_dict(_mapping(item)) for item in answers)
        result[work_item_id] = page
    return result


def _manifest_declares(lease: CommittedSnapshotLease, path: str) -> bool:
    return any(item.path == path for item in lease.manifest.files)


def _read_correction_state(
    lease: CommittedSnapshotLease,
    responses: tuple[EffectiveResponse, ...],
    session_id: str,
) -> CorrectionState:
    """Read the new immutable authority or a deliberately narrow legacy adapter."""
    state_declared = _manifest_declares(lease, "correction_state.json")
    state_file = lease.open_allowlisted("correction_state.json")
    if state_declared:
        if isinstance(state_file, Err):
            raise ValueError("declared correction state is absent")
        with state_file.value:
            state = CorrectionState.from_dict(json.load(state_file.value))
        return state
    if not isinstance(state_file, Err):
        raise ValueError("unlisted correction state is present")

    projection_declared = _manifest_declares(lease, "projection_request.json")
    projection_file = lease.open_allowlisted("projection_request.json")
    events_declared = _manifest_declares(lease, "correction_events.json")
    events_file = lease.open_allowlisted("correction_events.json")
    if projection_declared != (not isinstance(projection_file, Err)):
        raise ValueError("declared legacy projection is absent")
    if events_declared != (not isinstance(events_file, Err)):
        raise ValueError("declared legacy correction events are absent")
    if projection_declared:
        assert not isinstance(projection_file, Err)
        with projection_file.value:
            projection = _decode_projection(_mapping(json.load(projection_file.value)), lease)
        events: tuple[CorrectionEvent, ...] = ()
        if events_declared:
            assert not isinstance(events_file, Err)
            with events_file.value:
                events = _decode_correction_events(json.load(events_file.value))
        if projection.corrections and not events_declared:
            raise ValueError("legacy correction event authority is absent")
        validated = validate_correction_event_history(
            events, projection.corrections, session_id=session_id
        )
        if isinstance(validated, Err) or any(
            event.committed_revision > lease.snapshot_ref.revision for event in events
        ):
            raise ValueError("legacy correction history is invalid")
        baseline = project_effective_responses(
            EffectiveResponseProjection(
                projection.automatic_pages, projection.imported_responses, ()
            ),
            session_id=session_id,
            expected_base_revision=lease.manifest.parent_revision or lease.snapshot_ref.revision,
        )
        if isinstance(baseline, Err):
            raise ValueError("legacy correction baseline is invalid")
        return CorrectionState(
            1,
            lease.snapshot_ref.revision,
            lease.snapshot_ref.generation_id,
            lease.manifest.parent_revision or lease.snapshot_ref.revision,
            lease.manifest.parent_generation_id or lease.snapshot_ref.generation_id,
            baseline.value,
            events,
            True,
            "effective-baseline",
        )
    if events_declared:
        raise ValueError("legacy compact generation declares unattached correction events")
    if lease.manifest.state.value not in {"graded", "finalized"}:
        raise ValueError("legacy compact generation is not correction eligible")
    # Valid fixed20 compact manifests preserved the effective response set but not
    # a projection or historical events.  Treat that fact as explicit, read-only
    # compatibility metadata; no file is written simply by opening it.
    return CorrectionState(
        1,
        lease.snapshot_ref.revision,
        lease.snapshot_ref.generation_id,
        lease.snapshot_ref.revision,
        lease.snapshot_ref.generation_id,
        responses,
        (),
        False,
        "legacy-fixed20",
    )


def _validate_correction_state_binding(
    state: CorrectionState, lease: CommittedSnapshotLease, session_id: str
) -> None:
    """Bind state and history to this exact opened immutable generation."""
    snapshot = lease.snapshot_ref
    if (
        state.snapshot_revision != snapshot.revision
        or state.snapshot_generation_id != snapshot.generation_id
        or state.baseline_snapshot_revision > snapshot.revision
    ):
        raise ValueError("correction state snapshot binding is invalid")
    if (
        state.baseline_snapshot_revision == snapshot.revision
        and state.baseline_snapshot_generation_id != snapshot.generation_id
    ):
        raise ValueError("correction baseline generation is orphaned")
    for event in state.events:
        if (
            event.session_id != session_id
            or event.expected_base_revision < state.baseline_snapshot_revision
            or event.expected_base_revision >= event.committed_revision
            or event.committed_revision > snapshot.revision
        ):
            raise ValueError("correction history event is outside the opened snapshot")


def _score_set_wire(scores: ScoreSet) -> dict[str, object]:
    return {
        "maximum_score": str(scores.maximum_score),
        "rows": [
            {
                "work_item_id": row.work_item_id,
                "score": None if row.score is None else str(row.score),
                "rank": row.rank,
            }
            for row in scores.rows
        ],
        "statistics": {
            "participant_count": scores.statistics.participant_count,
            "average_score": (
                None
                if scores.statistics.average_score is None
                else str(scores.statistics.average_score)
            ),
            "highest_score": (
                None
                if scores.statistics.highest_score is None
                else str(scores.statistics.highest_score)
            ),
            "lowest_score": (
                None
                if scores.statistics.lowest_score is None
                else str(scores.statistics.lowest_score)
            ),
        },
    }


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("detail JSON object is invalid")
    return value


def _error(code: str, reason: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", context={"reason": reason}),))

def _close_error(lease: CommittedSnapshotLease) -> ErrorInfo | None:
    try:
        closed = lease.close()
    except BaseException as error:
        return ErrorInfo(
            "DETAIL_LEASE_CLOSE_FAILED",
            "error.detail_lease_close_failed",
            context={"reason": str(error)},
        )
    if isinstance(closed, Err):
        issue = closed.errors[0]
        return ErrorInfo(
            "DETAIL_LEASE_CLOSE_FAILED",
            "error.detail_lease_close_failed",
            issue.field_path,
            dict(issue.context),
            issue.retryable,
            issue.cause_type,
        )
    return None


def _with_cleanup(primary: Err, lease: CommittedSnapshotLease) -> Err:
    cleanup = _close_error(lease)
    return Err(primary.errors + (() if cleanup is None else (cleanup,)))

class DetailRepository:
    """Application-owned detail lease registry; values never contain paths or leases."""

    def __init__(self, coordinator: InternalSessionCoordinator) -> None:
        self._coordinator = coordinator
        self._opened: dict[str, _OpenedDetail] = {}

    def read_correction_snapshot(
        self, session_id: str, expected_revision: int
    ) -> Result[CommittedCorrectionSnapshot]:
        opened = self._coordinator.open_committed_snapshot(
            SnapshotRequest(session_id, expected_revision, SnapshotPurpose.DETAIL)
        )
        if isinstance(opened, Err):
            return opened
        lease = opened.value
        try:
            if (
                lease.snapshot_ref.session_id != session_id
                or lease.snapshot_ref.revision != expected_revision
            ):
                raise ValueError("pinned lease identity mismatch")
            combined_file = lease.open_allowlisted("semantic_inputs.json")
            if isinstance(combined_file, Err):
                raise ValueError("required canonical correction inputs are absent")
            with combined_file.value:
                combined = _mapping(json.load(combined_file.value))
            if set(combined) != {"combined"}:
                raise ValueError("semantic input envelope is invalid")
            canonical = _mapping(combined["combined"])
            if set(canonical) != {
                "session",
                "roster",
                "responses",
                "scores",
                "answer_key",
                "failures",
            }:
                raise ValueError("canonical combined envelope is invalid")
            record = SessionRecord.from_dict(_mapping(canonical["session"]))
            if (
                record.session_id != session_id
                or record.revision != expected_revision
                or record.state is not lease.manifest.state
            ):
                raise ValueError("combined session identity mismatch")
            responses = tuple(
                EffectiveResponse.from_dict(_mapping(value))
                for value in _list(canonical["responses"])
            )
            if len({item.work_item_id for item in responses}) != len(responses):
                raise ValueError("duplicate effective work item")
            answer_key = AnswerKeySnapshot.from_dict(_mapping(canonical["answer_key"]))
            if with_rounded_average(canonical["scores"]) != _score_set_wire(
                score_effective(ScoreInput(responses, answer_key))
            ):
                raise ValueError("canonical scores do not match recomputed scores")
            correction_state = _read_correction_state(lease, responses, session_id)
            _validate_correction_state_binding(correction_state, lease, session_id)
            projected = project_correction_state(correction_state, session_id=session_id)
            if isinstance(projected, Err) or projected.value != responses:
                raise ValueError("correction state does not match canonical responses")
            return Ok(
                CommittedCorrectionSnapshot(
                    lease.snapshot_ref,
                    lease,
                    record.state,
                    responses,
                    answer_key,
                    correction_state,
                )
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return _with_cleanup(
                _error(
                    "CORRECTION_MATERIALIZATION_INVALID",
                    "보정에 필요한 커밋 스냅샷 자료가 올바르지 않습니다.",
                ),
                lease,
            )

    def open_detail(
        self, session_id: str, revision: int | None = None
    ) -> Result[tuple[DetailHandle, tuple[DetailListRow, ...]]]:
        opened = self._coordinator.open_committed_snapshot(
            SnapshotRequest(session_id, revision, SnapshotPurpose.DETAIL)
        )
        if isinstance(opened, Err):
            return opened
        lease = opened.value
        index = lease.open_allowlisted("detail_index.json")
        if isinstance(index, Err):
            return _with_cleanup(
                _error(
                    "DETAIL_MATERIALIZATION_MISSING",
                    "상세 목록 자료가 없는 커밋 스냅샷입니다.",
                ),
                lease,
            )
        try:
            with index.value:
                raw = _mapping(json.load(index.value))
            if (
                set(raw) != {"schema_version", "snapshot", "work_items"}
                or raw["schema_version"] != 1
            ):
                raise ValueError("detail index envelope is invalid")
            _require_snapshot(raw["snapshot"], lease.snapshot_ref)
            values = raw["work_items"]
            if not isinstance(values, list):
                raise ValueError("work_items must be list")
            review_geometry = _read_review_geometry(lease)
            rows: dict[str, dict[str, object]] = {}
            public: list[DetailListRow] = []
            for item in values:
                if not isinstance(item, dict) or not isinstance(item.get("work_item_id"), str):
                    raise ValueError("invalid work item")
                work_item_id = item["work_item_id"]
                if work_item_id in rows:
                    raise ValueError("duplicate work item")
                detail_path = item.get("detail_path")
                image_path = item.get("image_path")
                if not isinstance(detail_path, str) or (
                    image_path is not None and not isinstance(image_path, str)
                ):
                    raise ValueError("invalid materialized paths")
                rows[work_item_id] = item
                public.append(
                    DetailListRow(
                        work_item_id,
                        item.get("student_id") if isinstance(item.get("student_id"), str) else None,
                        item.get("student_name")
                        if isinstance(item.get("student_name"), str)
                        else None,
                        item.get("score") if isinstance(item.get("score"), str) else None,
                        item.get("rank") if type(item.get("rank")) is int else None,
                        image_path is not None,
                    )
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return _with_cleanup(
                _error("DETAIL_MATERIALIZATION_INVALID", "상세 목록 자료가 올바르지 않습니다."),
                lease,
            )
        handle_id = secrets.token_urlsafe(24)
        self._opened[handle_id] = _OpenedDetail(lease, rows, review_geometry)
        return Ok((DetailHandle(handle_id, lease.snapshot_ref), tuple(public)))

    def load_work_item(
        self, handle_id: str, work_item_id: str, *, include_image: bool = True
    ) -> Result[WorkItemDetail]:
        opened = self._opened.get(handle_id)
        if opened is None:
            return _error("DETAIL_HANDLE_INVALID", "상세 화면 핸들이 유효하지 않습니다.")
        item = opened.rows.get(work_item_id)
        if item is None:
            return _error("DETAIL_WORK_ITEM_NOT_FOUND", "선택한 응시자를 찾을 수 없습니다.")
        detail_path = item["detail_path"]
        assert isinstance(detail_path, str)
        detail = opened.lease.open_allowlisted(detail_path)
        if isinstance(detail, Err):
            return _error("DETAIL_MATERIALIZATION_MISSING", "선택한 상세 자료가 없습니다.")
        try:
            with detail.value:
                document = _mapping(json.load(detail.value))
            if set(document) != {"schema_version", "snapshot", "work_item_id", "payload"}:
                raise ValueError("detail document envelope is invalid")
            if document["schema_version"] != 1 or document["work_item_id"] != work_item_id:
                raise ValueError("detail document work item is invalid")
            _require_snapshot(document["snapshot"], opened.lease.snapshot_ref)
            payload = _mapping(document["payload"])
            if "review_geometry" not in payload:
                geometry = opened.review_geometry.get(work_item_id)
                if geometry is not None:
                    payload = {**payload, "review_geometry": geometry}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return _error("DETAIL_MATERIALIZATION_INVALID", "선택한 상세 자료가 올바르지 않습니다.")
        image: bytes | None = None
        image_path = item.get("image_path")
        if include_image and image_path is not None:
            assert isinstance(image_path, str)
            source = opened.lease.open_allowlisted(image_path)
            if isinstance(source, Err):
                return _error("DETAIL_IMAGE_MISSING", "선택한 이미지 자료가 없습니다.")
            try:
                with source.value:
                    image = source.value.read()
            except OSError:
                return _error("DETAIL_IMAGE_INVALID", "선택한 이미지 자료를 읽을 수 없습니다.")
        return Ok(WorkItemDetail(work_item_id, payload, image))

    def close_detail(self, handle_id: str) -> Result[None]:
        opened = self._opened.get(handle_id)
        if opened is None:
            return _error("DETAIL_HANDLE_INVALID", "상세 화면 핸들이 유효하지 않습니다.")
        cleanup = _close_error(opened.lease)
        if cleanup is not None:
            return Err((cleanup,))
        del self._opened[handle_id]
        return Ok(None)

    def close_all(self) -> Result[None]:
        """Close every retained lease in stable handle order, retaining failed handles for retry."""
        failure: Err | None = None
        for handle_id in tuple(sorted(self._opened)):
            closed = self.close_detail(handle_id)
            if isinstance(closed, Err) and failure is None:
                failure = closed
        return failure if failure is not None else Ok(None)


__all__ = ["DetailHandle", "DetailListRow", "DetailRepository", "WorkItemDetail"]
