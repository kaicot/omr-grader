"""Portable-store adapters for response import and committed grading snapshots."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from omr_grader.application.dto import (
    EffectiveResponseProjection,
    ImportResponseCommand,
    ScoreResult,
    ScoreSet,
    ScoreStatistics,
    SessionCreateResult,
    SnapshotRequest,
)
from omr_grader.application.grading_use_case import CommittedGradingSnapshot
from omr_grader.domain.corrections import project_effective_responses
from omr_grader.domain.enums import (
    AnswerKeySnapshotKind,
    AnswerStatus,
    CreationKind,
    ExamTerm,
    KeyQuestionStatus,
    OperationKind,
    RosterRowStatus,
    RosterSnapshotKind,
    SessionState,
    SnapshotPurpose,
)
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import (
    AnswerKeyEntry,
    AnswerKeySnapshot,
    AnswerValue,
    EffectiveResponse,
    IdentityRecord,
    ImportedResponseRef,
    ManifestFile,
    ManifestSummary,
    RosterEntry,
    RosterSnapshot,
    SessionManifest,
    SessionRecord,
)
from omr_grader.infrastructure.session_store import SessionCommitCoordinator, SessionStore

_APP_VERSION = "omr-grader"
_ROSTER_NORMALIZATION = "imported-response-v1"


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _error(code: str, reason: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", context={"reason": reason}),))


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _unset_answer_key() -> AnswerKeySnapshot:
    return AnswerKeySnapshot(
        1,
        AnswerKeySnapshotKind.UNSET,
        None,
        None,
        None,
        "unset-v1",
        tuple(
            AnswerKeyEntry(
                question, AnswerValue((), AnswerStatus.UNASKED), "0", KeyQuestionStatus.UNASKED
            )
            for question in range(1, 101)
        ),
        (),
    )


def _semantic_sha(value: RosterSnapshot | AnswerKeySnapshot) -> str:
    def wire(item: object) -> object:
        if hasattr(item, "value") and type(item.value) is str:
            return item.value
        if isinstance(item, Decimal):
            text = format(item, "f").rstrip("0").rstrip(".")
            return text or "0"
        if isinstance(item, dict):
            return {str(key): wire(value) for key, value in item.items()}
        if isinstance(item, list | tuple):
            return [wire(value) for value in item]
        return item

    return hashlib.sha256(
        json.dumps(
            wire(value.to_dict()), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("JSON object is invalid")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("JSON array is invalid")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("JSON string is invalid")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("JSON integer is invalid")
    return value


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("JSON decimal is invalid")
    return Decimal(value)


def _imported_roster(
    rows: tuple[ImportedResponseRef, ...], source_sha256: str, sheet_name: str
) -> RosterSnapshot:
    groups: dict[str, list[ImportedResponseRef]] = {}
    for row in rows:
        if (
            len(row.raw_student_id) == 8
            and row.raw_student_id.isascii()
            and row.raw_student_id.isdigit()
        ):
            groups.setdefault(row.raw_student_id, []).append(row)
    entries: list[RosterEntry] = []
    for row in rows:
        student_id = row.raw_student_id if row.raw_student_id in groups else None
        issues: list[str] = []
        if student_id is None:
            issues.append(RosterRowStatus.INVALID_ID.value)
        else:
            group = groups[student_id]
            if len(group) > 1:
                issues.append(RosterRowStatus.DUPLICATE_ID.value)
            if len({item.name for item in group}) > 1:
                issues.append(RosterRowStatus.NAME_CONFLICT.value)
        ordered = tuple(sorted(issues))
        status = next(
            (
                candidate
                for candidate in (
                    RosterRowStatus.INVALID_ID,
                    RosterRowStatus.DUPLICATE_ID,
                    RosterRowStatus.NAME_CONFLICT,
                )
                if candidate.value in ordered
            ),
            RosterRowStatus.NORMAL,
        )
        entries.append(
            RosterEntry(
                hashlib.sha256(
                    f"{source_sha256}:{sheet_name}:{row.row_number}".encode()
                ).hexdigest(),
                row.row_number,
                row.input_ordinal,
                row.raw_student_id,
                student_id,
                row.name,
                status,
                ordered,
            )
        )
    return RosterSnapshot(
        1,
        RosterSnapshotKind.IMPORTED_RESPONSE,
        rows[0].source_filename,
        source_sha256,
        sheet_name,
        _ROSTER_NORMALIZATION,
        tuple(entries),
        (),
    )


class ResponseImportCommitCoordinator:
    """Build a complete immutable imported-response generation before publication."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    def commit_imported_responses(
        self,
        command: ImportResponseCommand,
        *,
        exam_name: str,
        exam_year: int | None,
        exam_term: ExamTerm,
        source_sha256: str,
        sheet_name: str,
        rows: tuple[ImportedResponseRef, ...],
    ) -> Result[SessionCreateResult]:
        if not rows or any(
            row.source_sha256 != source_sha256 or row.sheet_name != sheet_name for row in rows
        ):
            return _error("RESPONSE_IMPORT_INVALID", "response rows do not match pinned source")
        try:
            roster = _imported_roster(rows, source_sha256, sheet_name)
            projection = EffectiveResponseProjection((), rows, ())
            effective = project_effective_responses(
                projection, session_id=command.session_id, expected_base_revision=1
            )
            if isinstance(effective, Err):
                return effective
            now = _utc()
            generation_id = uuid4().hex
            identity = IdentityRecord(1, command.session_id, now, CreationKind.IMPORTED_RESPONSES)
            record = SessionRecord(
                1,
                command.session_id,
                1,
                SessionState.RECOGNIZED,
                exam_name,
                exam_year,
                exam_term,
                now,
                None,
                now,
            )
            answer_key = _unset_answer_key()
            combined = {
                "session": record.to_dict(),
                "roster": roster.to_dict(),
                "responses": [item.to_dict() for item in effective.value],
                "scores": None,
                "answer_key": answer_key.to_dict(),
                "failures": [],
            }
            snapshot = {
                "session_id": command.session_id,
                "revision": 1,
                "generation_id": generation_id,
            }
            detail_index: list[dict[str, object]] = []
            artifacts = {
                "semantic_inputs.json": _json_bytes({"combined": combined}),
                "projection_request.json": _json_bytes(
                    {
                        "schema_version": 1,
                        "base_snapshot": None,
                        "base_response_ids": [item.work_item_id for item in effective.value],
                        "projection": {
                            "automatic_pages": [],
                            "imported_responses": [item.to_dict() for item in rows],
                            "corrections": [],
                        },
                    }
                ),
            }
            for response in effective.value:
                detail_path = f"details/{response.work_item_id}.json"
                artifacts[detail_path] = _json_bytes(
                    {
                        "schema_version": 1,
                        "snapshot": snapshot,
                        "work_item_id": response.work_item_id,
                        "payload": {"response": response.to_dict()},
                    }
                )
                detail_index.append(
                    {
                        "work_item_id": response.work_item_id,
                        "detail_path": detail_path,
                        "image_path": None,
                        "student_id": response.student_id,
                        "student_name": next(
                            (
                                entry.name
                                for entry in roster.rows
                                if entry.student_id == response.student_id
                            ),
                            None,
                        ),
                        "score": None,
                        "rank": None,
                    }
                )
            artifacts["detail_index.json"] = _json_bytes(
                {
                    "schema_version": 1,
                    "snapshot": snapshot,
                    "work_items": detail_index,
                }
            )
            files = tuple(
                ManifestFile(path, len(payload), _sha(payload), "application/json")
                for path, payload in sorted(artifacts.items(), key=lambda item: item[0].encode())
            )
            base_ids = tuple(
                sorted((item.work_item_id for item in effective.value), key=str.encode)
            )
            manifest = SessionManifest(
                1,
                command.session_id,
                1,
                generation_id,
                None,
                None,
                None,
                command.operation_id,
                OperationKind.IMPORT_RESPONSES,
                _APP_VERSION,
                now,
                SessionState.RECOGNIZED,
                base_ids,
                None,
                _semantic_sha(roster),
                _semantic_sha(answer_key),
                None,
                None,
                files,
                ManifestSummary(len(base_ids), len(base_ids), 0, None),
            )
            return self._store._create_initial_generation(
                identity=identity,
                manifest=manifest,
                session=record,
                display_name=command.session_id,
                artifacts=artifacts,
            )
        except (TypeError, ValueError, OSError) as exc:
            return _error("RESPONSE_IMPORT_COMMIT_FAILED", str(exc))


class CommittedGradingSnapshotReader:
    """Read only manifest-allowlisted canonical inputs from one pinned generation."""

    def __init__(self, coordinator: SessionCommitCoordinator) -> None:
        self._coordinator = coordinator

    def read_grading_snapshot(
        self, session_id: str, expected_revision: int
    ) -> Result[CommittedGradingSnapshot]:
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
            combined_stream = lease.open_allowlisted("semantic_inputs.json")
            projection_stream = lease.open_allowlisted("projection_request.json")
            if isinstance(combined_stream, Err) or isinstance(projection_stream, Err):
                raise ValueError("required canonical grading artifacts are absent")
            with combined_stream.value:
                envelope = _mapping(json.load(combined_stream.value))
            with projection_stream.value:
                projection_document = _mapping(json.load(projection_stream.value))
            canonical = _mapping(envelope.get("combined"))
            if set(envelope) != {"combined"} or set(canonical) != {
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
                raise ValueError("canonical session does not match pinned manifest")
            if (
                lease.manifest.session_id != session_id
                or lease.manifest.revision != expected_revision
                or lease.manifest.state is not record.state
            ):
                raise ValueError("manifest identity does not match pinned session")
            roster = RosterSnapshot.from_dict(_mapping(canonical["roster"]))
            if _semantic_sha(roster) != lease.manifest.roster_sha256:
                raise ValueError("committed roster does not match manifest")
            responses = tuple(
                EffectiveResponse.from_dict(_mapping(item))
                for item in _array(canonical["responses"])
            )
            ids = tuple(sorted((item.work_item_id for item in responses), key=str.encode))
            if not responses or ids != lease.manifest.base_response_ids:
                raise ValueError("canonical responses do not match manifest base responses")
            projection = self._projection(projection_document, session_id, expected_revision, ids)
            projected = project_effective_responses(
                projection, session_id=session_id, expected_base_revision=expected_revision
            )
            if isinstance(projected, Err) or projected.value != responses:
                raise ValueError("projection does not reproduce canonical responses")
            key_value = canonical["answer_key"]
            if key_value is None:
                raise ValueError("committed answer key is absent")
            answer_key = AnswerKeySnapshot.from_dict(_mapping(key_value))
            if _semantic_sha(answer_key) != lease.manifest.key_sha256:
                raise ValueError("committed answer key does not match manifest")
            scores = self._scores(canonical["scores"], ids)
            if record.state is SessionState.GRADED and scores is None:
                raise ValueError("graded session has no canonical scores")
            if scores is not None and (
                lease.manifest.summary.work_items != len(scores.rows)
                or lease.manifest.summary.processable != scores.statistics.participant_count
                or lease.manifest.summary.manual_review
                != len(scores.rows) - scores.statistics.participant_count
                or lease.manifest.summary.maximum_score != format(scores.maximum_score, "f")
            ):
                raise ValueError("canonical scores do not match manifest summary")
            return Ok(
                CommittedGradingSnapshot(record.state, answer_key, responses, projection, scores)
            )
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            InvalidOperation,
            AttributeError,
        ) as exc:
            return _error("SESSION_GRADING_SNAPSHOT_INVALID", str(exc))
        finally:
            lease.close()

    @staticmethod
    def _projection(
        document: dict[str, object], session_id: str, revision: int, ids: tuple[str, ...]
    ) -> EffectiveResponseProjection:
        if (
            set(document) != {"schema_version", "base_snapshot", "base_response_ids", "projection"}
            or document.get("schema_version") != 1
        ):
            raise ValueError("projection envelope is invalid")
        if (
            tuple(
                sorted(
                    _array(document["base_response_ids"]),
                    key=lambda item: str(item).encode(),
                )
            )
            != ids
        ):
            raise ValueError("projection response IDs do not match canonical responses")
        payload = _mapping(document["projection"])
        if set(payload) != {"automatic_pages", "imported_responses", "corrections"}:
            raise ValueError("projection payload is invalid")
        from omr_grader.domain.models import AutomaticPage, CorrectionDraft

        return EffectiveResponseProjection(
            tuple(
                AutomaticPage.from_dict(_mapping(item))
                for item in _array(payload["automatic_pages"])
            ),
            tuple(
                ImportedResponseRef.from_dict(_mapping(item))
                for item in _array(payload["imported_responses"])
            ),
            tuple(
                CorrectionDraft.from_dict(_mapping(item)) for item in _array(payload["corrections"])
            ),
        )

    @staticmethod
    def _scores(value: object, ids: tuple[str, ...]) -> ScoreSet | None:
        if value is None:
            return None
        wire = _mapping(value)
        if set(wire) != {"maximum_score", "rows", "statistics"}:
            raise ValueError("canonical scores are invalid")
        stats = _mapping(wire["statistics"])
        rows = tuple(
            ScoreResult(
                _string(item["work_item_id"]),
                None if item["score"] is None else _decimal(item["score"]),
                None if item["rank"] is None else _integer(item["rank"]),
            )
            for raw in _array(wire["rows"])
            for item in (_mapping(raw),)
        )
        if tuple(sorted((row.work_item_id for row in rows), key=str.encode)) != ids:
            raise ValueError("canonical score IDs do not match responses")
        return ScoreSet(
            _decimal(wire["maximum_score"]),
            rows,
            ScoreStatistics(
                _integer(stats["participant_count"]),
                None if stats["average_score"] is None else _decimal(stats["average_score"]),
                None if stats["highest_score"] is None else _decimal(stats["highest_score"]),
                None if stats["lowest_score"] is None else _decimal(stats["lowest_score"]),
            ),
        )


__all__ = ["CommittedGradingSnapshotReader", "ResponseImportCommitCoordinator"]
