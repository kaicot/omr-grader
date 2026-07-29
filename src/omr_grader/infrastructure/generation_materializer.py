"""Private generation-artifact materialization behind a store-owned staging token."""

from __future__ import annotations

import json
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from omr_grader.application.dto import (
    CorrectionSemanticView,
    EffectiveResponseProjection,
    GenerationMutation,
    GradingSemanticView,
    RecognitionSemanticView,
    ScoreInput,
    ScoreSet,
)
from omr_grader.domain.enums import OperationKind
from omr_grader.domain.errors import Err
from omr_grader.domain.grading import score_effective
from omr_grader.domain.models import (
    AnswerKeySnapshot,
    AutomaticPage,
    CorrectionDraft,
    CorrectionEvent,
    EffectiveResponse,
    ImportedResponseRef,
    RosterSnapshot,
    SessionRecord,
)
from omr_grader.infrastructure.atomic_io import atomic_write_json
from omr_grader.workbooks.score_book import write_final_book, write_score_book

if TYPE_CHECKING:
    from omr_grader.domain.models import SessionManifest


@dataclass(frozen=True, slots=True)
class RecognitionArtifactInput:
    """Pinned recognition artifacts keyed by automatic-page work-item ID."""

    normalized_images: Mapping[str, bytes] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(
            isinstance(work_item_id, str) and work_item_id and type(image) is bytes and image
            for work_item_id, image in self.normalized_images.items()
        ):
            raise TypeError("recognition artifacts must be nonempty pinned bytes")


@dataclass(frozen=True, slots=True)
class StagingToken:
    """Capability for a single store-created staging directory."""

    _root: Path

    def path(self, relative: str) -> Path:
        parts = relative.split("/")
        if not relative or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("materializer path escapes staging")
        candidate = self._root.joinpath(*parts)
        if candidate.parent != self._root and self._root not in candidate.parents:
            raise ValueError("materializer path escapes staging")
        return candidate

    def write_json(self, relative: str, value: object) -> None:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        result = atomic_write_json(target, value)
        if isinstance(result, Err):
            raise OSError(result.errors[0].context.get("reason", "JSON write failed"))

    def write_bytes(self, relative: str, value: bytes) -> None:
        if type(value) is not bytes:
            raise TypeError("artifact bytes must be pinned bytes")
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)


@dataclass(frozen=True, slots=True)
class GenerationMaterializationInput:
    token: StagingToken
    mutation: GenerationMutation
    record: SessionRecord
    parent_manifest: SessionManifest
    parent_manifest_sha256: str
    parent_generation: Path
    generation_id: str
    recognition_artifacts: RecognitionArtifactInput | None = None

    def __post_init__(self) -> None:
        if not self.generation_id:
            raise ValueError("generation ID is required for snapshot-bound details")


class GenerationMaterializer:
    """Regenerates only projections whose inputs are pinned by the mutation."""

    def materialize(self, request: GenerationMaterializationInput) -> None:
        mutation = request.mutation
        parent = _read_combined(request.parent_generation)
        if parent is not None:
            _validate_parent_combined(parent, request.parent_manifest)
        parent_projection = _read_parent_projection(
            request.parent_generation, request.parent_manifest
        )
        _validate_parent_projection(request, parent, parent_projection)
        projection = _projection(request)
        _validate_projection_lineage(request, parent_projection, projection)
        if projection is not None:
            request.token.write_json(
                "projection_request.json",
                _projection_envelope(request, projection),
            )
        _materialize_correction_events(request, parent_projection, projection)
        combined = _combined(request, parent, projection)
        if combined is not None:
            request.token.write_json("semantic_inputs.json", {"combined": combined})
            self._write_details(request, combined, projection or parent_projection)
        if mutation.operation_kind in (
            OperationKind.REGRADE,
            OperationKind.FINALIZE,
            OperationKind.CORRECT,
        ):
            self._write_workbook(request, combined)

    def _write_workbook(
        self, request: GenerationMaterializationInput, combined: dict[str, object] | None
    ) -> None:
        if combined is None:
            raise ValueError("required pinned generation inputs are absent")
        answer_key, scores = _grading_envelope(request.mutation.operation_kind, combined)
        responses = tuple(
            EffectiveResponse.from_dict(_mapping(value)) for value in _array(combined, "responses")
        )
        roster = RosterSnapshot.from_dict(_object(combined, "roster"))
        names = {row.student_id: row.name for row in roster.rows if row.student_id is not None}
        # This is deliberately the parent digest: embedding the target manifest
        # digest in an artifact would make the manifest's own hash cyclic.
        if request.mutation.operation_kind is OperationKind.FINALIZE:
            output = write_final_book(
                request.token.path("artifacts"),
                exam_name=request.record.exam_name,
                committed_at=request.record.updated_at,
                session_id=request.record.session_id,
                revision=request.record.revision,
                manifest_sha256=request.parent_manifest_sha256,
                responses=responses,
                key=answer_key,
                scores=scores,
                names_by_student_id=names,
            )
        else:
            output = write_score_book(
                request.token.path("artifacts"),
                exam_name=request.record.exam_name,
                committed_at=request.record.updated_at,
                session_id=request.record.session_id,
                revision=request.record.revision,
                manifest_sha256=request.parent_manifest_sha256,
                responses=responses,
                key=answer_key,
                scores=scores,
                names_by_student_id=names,
            )
        _validate_xlsx(output)
        request.token.write_json(
            f"{output.relative_to(request.token._root).as_posix()}.provenance.json",
            {"schema_version": 1, "source_manifest_sha256": request.parent_manifest_sha256},
        )

    def _write_details(
        self,
        request: GenerationMaterializationInput,
        combined: dict[str, object],
        projection: EffectiveResponseProjection | None,
    ) -> None:
        """Write immutable, allowlisted detail documents from pinned canonical inputs."""
        responses = tuple(
            EffectiveResponse.from_dict(_mapping(value)) for value in _array(combined, "responses")
        )
        roster = RosterSnapshot.from_dict(_object(combined, "roster"))
        names = {row.student_id: row.name for row in roster.rows if row.student_id is not None}
        scores = _score_rows(combined.get("scores"))
        pages = {
            page.page_ref.work_item_id: page
            for page in (projection.automatic_pages if projection is not None else ())
        }
        snapshot = {
            "session_id": request.record.session_id,
            "revision": request.record.revision,
            "generation_id": request.generation_id,
        }
        index: list[dict[str, object]] = []
        artifacts = (
            request.recognition_artifacts.normalized_images
            if request.recognition_artifacts is not None
            else {}
        )
        for response in responses:
            page = pages.get(response.work_item_id)
            image_path: str | None = None
            if page is not None:
                request.token.write_json(f"evidence/{response.work_item_id}.json", page.to_dict())
                image = artifacts.get(response.work_item_id)
                if image is not None:
                    image_path = f"images/{response.work_item_id}.png"
                    request.token.write_bytes(image_path, image)
                elif request.token.path(f"images/{response.work_item_id}.png").is_file():
                    image_path = f"images/{response.work_item_id}.png"
            score, rank = scores.get(response.work_item_id, (None, None))
            detail_path = f"details/{response.work_item_id}.json"
            payload: dict[str, object] = {"response": response.to_dict()}
            if page is not None:
                payload["recognition"] = page.to_dict()
                payload["evidence_path"] = f"evidence/{response.work_item_id}.json"
            request.token.write_json(
                detail_path,
                {
                    "schema_version": 1,
                    "snapshot": snapshot,
                    "work_item_id": response.work_item_id,
                    "payload": payload,
                },
            )
            index.append(
                {
                    "work_item_id": response.work_item_id,
                    "detail_path": detail_path,
                    "image_path": image_path,
                    "student_id": response.student_id,
                    "student_name": (
                        names.get(response.student_id) if response.student_id is not None else None
                    ),
                    "score": score,
                    "rank": rank,
                }
            )
        request.token.write_json(
            "detail_index.json",
            {"schema_version": 1, "snapshot": snapshot, "work_items": index},
        )


def _validate_xlsx(path: Path) -> None:
    """Reject a partially-written or non-workbook ZIP before it reaches a manifest."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise ValueError("XLSX structural entries are absent")
            if archive.testzip() is not None:
                raise ValueError("XLSX contains corrupt member data")
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError("XLSX structural validation failed") from error


def _projection(request: GenerationMaterializationInput) -> EffectiveResponseProjection | None:
    semantic = request.mutation.semantic_inputs
    projection = request.mutation.projection_request
    if projection is None and isinstance(semantic, RecognitionSemanticView):
        projection = EffectiveResponseProjection(
            tuple(
                page
                for page in semantic.pages
                if page.processing_status.value in {"processed", "needs_manual_review"}
            ),
            (),
            (),
        )
    if projection is None and request.mutation.operation_kind is OperationKind.CORRECT:
        raise ValueError("correction generation requires a projection request")
    return projection


def _projection_envelope(
    request: GenerationMaterializationInput, projection: EffectiveResponseProjection
) -> dict[str, object]:
    parent = request.parent_manifest
    return {
        "schema_version": 1,
        "base_snapshot": {
            "session_id": parent.session_id,
            "revision": parent.revision,
            "generation_id": parent.generation_id,
            "manifest_sha256": request.parent_manifest_sha256,
        },
        "base_response_ids": list(parent.base_response_ids),
        "projection": {
            "automatic_pages": [page.to_dict() for page in projection.automatic_pages],
            "imported_responses": [row.to_dict() for row in projection.imported_responses],
            "corrections": [draft.to_dict() for draft in projection.corrections],
        },
    }


def _read_parent_projection(
    generation: Path, manifest: SessionManifest
) -> EffectiveResponseProjection | None:
    path = generation / "projection_request.json"
    if not path.is_file():
        return None
    try:
        document = _mapping(json.loads(path.read_text(encoding="utf-8")))
        if (
            set(document) != {"schema_version", "base_snapshot", "base_response_ids", "projection"}
            or document["schema_version"] != 1
        ):
            raise ValueError("projection request envelope is invalid")
        base_snapshot = _mapping(document["base_snapshot"])
        if set(base_snapshot) != {
            "session_id",
            "revision",
            "generation_id",
            "manifest_sha256",
        }:
            raise ValueError("projection base snapshot is invalid")
        base_response_ids = _list(document["base_response_ids"])
        if not all(isinstance(item, str) for item in base_response_ids):
            raise ValueError("projection base response IDs are invalid")
        if (
            base_snapshot["session_id"] != manifest.session_id
            or base_snapshot["revision"] != manifest.parent_revision
            or base_snapshot["generation_id"] != manifest.parent_generation_id
            or base_snapshot["manifest_sha256"] != manifest.parent_manifest_sha256
            or tuple(base_response_ids) != manifest.base_response_ids
        ):
            raise ValueError("projection base snapshot does not match parent lineage")
        payload = _mapping(document["projection"])
        if set(payload) != {"automatic_pages", "imported_responses", "corrections"}:
            raise ValueError("projection request fields are invalid")
        return EffectiveResponseProjection(
            tuple(
                AutomaticPage.from_dict(_mapping(page))
                for page in _list(payload["automatic_pages"])
            ),
            tuple(
                ImportedResponseRef.from_dict(_mapping(row))
                for row in _list(payload["imported_responses"])
            ),
            tuple(
                CorrectionDraft.from_dict(_mapping(draft))
                for draft in _list(payload["corrections"])
            ),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("committed projection inputs are corrupt") from error


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("projection JSON array is invalid")
    return value


def _validate_parent_projection(
    request: GenerationMaterializationInput,
    parent: dict[str, object] | None,
    projection: EffectiveResponseProjection | None,
) -> None:
    if projection is None:
        return
    if parent is None:
        raise ValueError("projection parent lacks canonical inputs")
    from omr_grader.domain.corrections import project_effective_responses

    responses = project_effective_responses(
        projection,
        session_id=request.parent_manifest.session_id,
        expected_base_revision=request.parent_manifest.revision,
    )
    if isinstance(responses, Err):
        raise ValueError("committed parent projection is invalid")
    canonical = tuple(
        EffectiveResponse.from_dict(_mapping(value)) for value in _array(parent, "responses")
    )
    if (
        responses.value != canonical
        or tuple(sorted((item.work_item_id for item in responses.value), key=str.encode))
        != request.parent_manifest.base_response_ids
    ):
        raise ValueError("committed parent projection does not match canonical authority")


def _validate_projection_lineage(
    request: GenerationMaterializationInput,
    parent: EffectiveResponseProjection | None,
    projection: EffectiveResponseProjection | None,
) -> None:
    operation = request.mutation.operation_kind
    if operation is OperationKind.RECOGNIZE:
        return
    if operation in (OperationKind.CORRECT, OperationKind.REGRADE, OperationKind.FINALIZE):
        if parent is None or projection is None:
            raise ValueError("lifecycle generation requires a complete parent projection")
        if operation is OperationKind.CORRECT:
            semantic = request.mutation.semantic_inputs
            prefix = len(parent.corrections)
            delta = projection.corrections[prefix:]
            if (
                projection.automatic_pages != parent.automatic_pages
                or projection.imported_responses != parent.imported_responses
                or projection.corrections[:prefix] != parent.corrections
                or not delta
                or type(semantic) is not CorrectionSemanticView
                or semantic.corrections != delta
            ):
                raise ValueError("correction projection is not parent authority plus exact delta")
        elif projection != parent:
            raise ValueError("lifecycle projection must exactly preserve parent authority")


def _read_correction_events(generation: Path) -> tuple[CorrectionEvent, ...]:
    path = generation / "correction_events.json"
    if not path.is_file():
        return ()
    try:
        document = _mapping(json.loads(path.read_text(encoding="utf-8")))
        if set(document) != {"schema_version", "events"} or document["schema_version"] != 1:
            raise ValueError("correction event envelope is invalid")
        return tuple(
            CorrectionEvent.from_dict(_mapping(value)) for value in _list(document["events"])
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("committed correction events are corrupt") from error


def _materialize_correction_events(
    request: GenerationMaterializationInput,
    parent_projection: EffectiveResponseProjection | None,
    projection: EffectiveResponseProjection | None,
) -> None:
    parent_events = _read_correction_events(request.parent_generation)
    parent_drafts = parent_projection.corrections if parent_projection is not None else ()
    from omr_grader.domain.corrections import validate_correction_event_history

    validated = validate_correction_event_history(
        parent_events, parent_drafts, session_id=request.mutation.session_id
    )
    if isinstance(validated, Err):
        raise ValueError("parent correction event authority is invalid")
    if any(event.committed_revision > request.parent_manifest.revision for event in parent_events):
        raise ValueError("parent correction event exceeds authoritative revision")
    if request.mutation.operation_kind not in (
        OperationKind.CORRECT,
        OperationKind.REGRADE,
        OperationKind.FINALIZE,
    ):
        return
    if parent_projection is None:
        raise ValueError("lifecycle generation requires parent projection authority")
    events = parent_events
    drafts = parent_drafts
    expected_new_base_revision: int | None = None
    if request.mutation.operation_kind is OperationKind.CORRECT:
        if projection is None:
            raise ValueError("correction generation requires projection authority")
        drafts = projection.corrections
        delta = drafts[len(parent_drafts) :]
        events = parent_events + tuple(
            CorrectionEvent(
                1,
                str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{request.mutation.session_id}:{request.mutation.operation_id}:{position}",
                    )
                ),
                request.mutation.session_id,
                draft.work_item_id,
                draft.target_kind,
                draft.target_key,
                request.parent_manifest.revision,
                draft.before,
                draft.after,
                draft.reason,
                "local",
                request.record.updated_at,
                request.record.revision,
                f"{request.mutation.operation_id}:{position}",
            )
            for position, draft in enumerate(delta)
        )
        expected_new_base_revision = request.parent_manifest.revision
    validated = validate_correction_event_history(
        events,
        drafts,
        session_id=request.mutation.session_id,
        expected_new_base_revision=expected_new_base_revision,
    )
    if isinstance(validated, Err):
        raise ValueError("correction event authority does not match target projection")
    request.token.write_json(
        "correction_events.json",
        {"schema_version": 1, "events": [event.to_dict() for event in events]},
    )


def _combined(
    request: GenerationMaterializationInput,
    parent: dict[str, object] | None,
    projection: EffectiveResponseProjection | None,
) -> dict[str, object] | None:
    semantic = request.mutation.semantic_inputs
    if request.mutation.operation_kind in (
        OperationKind.CORRECT,
        OperationKind.REGRADE,
        OperationKind.FINALIZE,
    ) and (parent is None or parent.get("scores") is None):
        raise ValueError("score-bearing lifecycle generation requires canonical parent scores")
    if projection is None:
        if parent is None:
            return None
        return {**parent, "session": request.record.to_dict()}
    from omr_grader.domain.corrections import project_effective_responses

    responses = project_effective_responses(
        projection,
        session_id=request.mutation.session_id,
        expected_base_revision=request.parent_manifest.revision,
    )
    if isinstance(responses, Err):
        raise ValueError("effective-response projection is invalid")
    roster = _object(parent, "roster") if parent is not None else None
    if roster is None and isinstance(semantic, RecognitionSemanticView):
        roster = semantic.roster.to_dict()
    if roster is None:
        raise ValueError("required pinned roster input is absent")
    answer_key = (
        semantic.answer_key.to_dict()
        if isinstance(semantic, GradingSemanticView)
        else (
            _object(parent, "answer_key") if parent is not None and "answer_key" in parent else None
        )
    )
    scores: object
    if request.mutation.operation_kind in (
        OperationKind.CORRECT,
        OperationKind.REGRADE,
        OperationKind.FINALIZE,
    ):
        if answer_key is None:
            raise ValueError("required pinned grading answer key is absent")
        key = AnswerKeySnapshot.from_dict(answer_key)
        recomputed = score_effective(ScoreInput(responses.value, key))
        if isinstance(semantic, GradingSemanticView):
            if semantic.scores is None:
                raise ValueError("score-bearing grading generation requires typed scores")
            if semantic.scores != recomputed:
                raise ValueError("typed scores do not match recomputed scores")
        scores = _score_set_wire(recomputed)
    else:
        scores = parent.get("scores") if parent else None
    failures = parent.get("failures", []) if parent else []
    return {
        "session": request.record.to_dict(),
        "roster": roster,
        "responses": [item.to_dict() for item in responses.value],
        "scores": scores,
        "answer_key": answer_key,
        "failures": failures,
    }


def _validate_parent_combined(parent: dict[str, object], manifest: SessionManifest) -> None:
    if set(parent) != {"session", "roster", "responses", "scores", "answer_key", "failures"}:
        raise ValueError("canonical parent combined envelope is invalid")
    record = SessionRecord.from_dict(_object(parent, "session"))
    if (
        record.session_id != manifest.session_id
        or record.revision != manifest.revision
        or record.state is not manifest.state
    ):
        raise ValueError("parent combined session identity mismatch")
    scores = parent["scores"]
    if scores is not None:
        responses = tuple(
            EffectiveResponse.from_dict(_mapping(value)) for value in _array(parent, "responses")
        )
        answer_key = AnswerKeySnapshot.from_dict(_object(parent, "answer_key"))
        if scores != _score_set_wire(score_effective(ScoreInput(responses, answer_key))):
            raise ValueError("canonical parent scores do not match recomputed scores")


def _score_rows(value: object) -> dict[str, tuple[str | None, int | None]]:
    if not isinstance(value, Mapping):
        return {}
    rows = value.get("rows")
    if not isinstance(rows, list):
        return {}
    result: dict[str, tuple[str | None, int | None]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("work_item_id"), str):
            raise ValueError("canonical score row is invalid")
        score = row.get("score")
        rank = row.get("rank")
        if score is not None and not isinstance(score, str):
            raise ValueError("canonical score is invalid")
        if rank is not None and type(rank) is not int:
            raise ValueError("canonical rank is invalid")
        if row["work_item_id"] in result:
            raise ValueError("duplicate canonical score row")
        result[row["work_item_id"]] = (score, rank)
    return result


def _read_combined(generation: Path) -> dict[str, object] | None:
    path = generation / "semantic_inputs.json"
    if not path.is_file():
        return None
    try:
        payload = _mapping(json.loads(path.read_text(encoding="utf-8")))
        if set(payload) != {"combined"}:
            raise ValueError("semantic input envelope is invalid")
        return _object(payload, "combined")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("committed semantic inputs are corrupt") from error


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError("combined JSON value is invalid")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _grading_envelope(
    operation: OperationKind, combined: dict[str, object]
) -> tuple[AnswerKeySnapshot, ScoreSet]:
    if operation in (
        OperationKind.CORRECT,
        OperationKind.REGRADE,
        OperationKind.FINALIZE,
    ):
        key = AnswerKeySnapshot.from_dict(_object(combined, "answer_key"))
        responses = tuple(
            EffectiveResponse.from_dict(_mapping(value)) for value in _array(combined, "responses")
        )
        recomputed = score_effective(ScoreInput(responses, key))
        if _object(combined, "scores") != _score_set_wire(recomputed):
            raise ValueError("canonical scores do not match recomputed scores")
        return key, recomputed
    raise ValueError("required pinned grading inputs are absent")


def _score_set_wire(scores: ScoreSet) -> dict[str, object]:
    statistics = scores.statistics
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
            "participant_count": statistics.participant_count,
            "average_score": (
                None if statistics.average_score is None else str(statistics.average_score)
            ),
            "highest_score": (
                None if statistics.highest_score is None else str(statistics.highest_score)
            ),
            "lowest_score": None
            if statistics.lowest_score is None
            else str(statistics.lowest_score),
        },
    }


def _object(value: Mapping[str, object], key: str) -> dict[str, object]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise ValueError(f"combined {key} is absent")
    return item


def _array(value: Mapping[str, object], key: str) -> list[object]:
    item = value.get(key)
    if not isinstance(item, list):
        raise ValueError(f"combined {key} is absent")
    return item
