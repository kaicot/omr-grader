"""Immutable, strict JSON wire contracts for persisted domain records."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import UnionType
from typing import Any, ClassVar, Self, TypeVar, cast, get_args, get_origin, get_type_hints

from .enums import (
    AnswerKeySnapshotKind,
    AnswerStatus,
    ArchiveLineageMode,
    CellStatus,
    CreationKind,
    ExamTerm,
    FieldStatus,
    KeyQuestionStatus,
    LineageState,
    OperationKind,
    ProcessingStatus,
    RosterRowStatus,
    RosterSnapshotKind,
    SessionState,
    SourceKind,
    StudentIdStatus,
    TargetKind,
)
from .errors import ErrorInfo

SCHEMA_VERSION = 1
_DIGITS = re.compile(r"^[0-9]{8}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
type JsonValue = None | bool | int | str | tuple[JsonValue, ...] | dict[str, JsonValue]


def _int(value: object, *, minimum: int | None = None) -> None:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError("expected integer")


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")


def _sha(value: object, label: str = "sha256") -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"invalid {label}")


def _decimal(value: object) -> str:
    if not isinstance(value, str) or not value or value[0] == "+":
        raise ValueError("decimal must be a canonical finite string")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("decimal is invalid") from error
    if not number.is_finite():
        raise ValueError("decimal must be finite")
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    text = "0" if text in {"", "-0"} else text
    if "." in text and len(text.split(".", 1)[1]) > 12:
        raise ValueError("decimal exceeds twelve fractional digits")
    if text != value:
        raise ValueError("decimal is not canonical")
    return text


def _timestamp(value: object) -> None:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError("timestamp must be UTC RFC3339 with six fractional digits")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as error:
        raise ValueError("invalid timestamp") from error


def _schema(value: object) -> None:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ValueError("unsupported schema version")


def _json(value: object) -> JsonValue:
    if value is None:
        return None
    if type(value) is bool:
        return bool(value)
    if type(value) is int:
        return int(value)
    if type(value) is str:
        return str(value)
    if isinstance(value, list | tuple):
        return tuple(_json(item) for item in value)
    if isinstance(value, Mapping):
        output: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            output[key] = _json(item)
        return output
    raise ValueError("extensions must be JSON-safe")


def _extensions(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise ValueError("extensions must be an object")
    normalized: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_-]*\.[A-Za-z][A-Za-z0-9_.-]*", key
        ):
            raise ValueError("extension keys must be namespaced")
        normalized[key] = _json(item)
    return normalized


def _path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _is_windows_device_name(component: str) -> bool:
    stem = component.split(".", 1)[0].upper().translate(str.maketrans("¹²³", "123"))
    return stem in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }


def _path(value: object) -> None:
    if not isinstance(value, str) or not value or unicodedata.normalize("NFC", value) != value:
        raise ValueError("path must be NFC, portable, and relative")
    if "\\" in value or value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        raise ValueError("path must be portable and relative")
    for part in value.split("/"):
        if (
            not part
            or part in {".", ".."}
            or part[-1] in {".", " "}
            or _is_windows_device_name(part)
            or any(
                ord(character) < 32 or ord(character) == 127 or character in '<>:"|?*'
                for character in part
            )
        ):
            raise ValueError("path contains an unsafe component")


def validate_portable_component(component: str) -> str:
    """Validate and return one NFC Windows-safe portable filename component."""
    if not isinstance(component, str) or "/" in component:
        raise ValueError("component must be one portable filename")
    _path(component)
    return component


def _session_id(value: str) -> None:
    _text(value, "session_id")
    validate_portable_component(value)


def _ordered_paths(paths: tuple[str, ...], label: str) -> None:
    if paths != tuple(sorted(paths, key=lambda path: path.encode("utf-8"))):
        raise ValueError(f"{label} must be UTF-8 sorted")
    canonical = tuple(_path_key(path) for path in paths)
    if len(set(canonical)) != len(canonical):
        raise ValueError(f"{label} contain a case-insensitive collision")


def _enum(value: object, enum_type: type[E]) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"expected {enum_type.__name__}")


E = TypeVar("E", bound=Enum)


class _Wire:
    __dataclass_fields__: ClassVar[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return {item.name: _encode(getattr(self, item.name)) for item in fields(cast(Any, self))}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        if not isinstance(value, Mapping) or set(value) != {
            item.name for item in fields(cast(Any, cls))
        }:
            raise ValueError(f"{cls.__name__} has invalid wire fields")
        if "schema_version" in value:
            _schema(value["schema_version"])
        hints = get_type_hints(cls)
        return cls(
            **{
                item.name: _decode(value[item.name], hints[item.name])
                for item in fields(cast(Any, cls))
            }
        )


def _encode(value: object) -> object:
    if isinstance(value, _Wire):
        return value.to_dict()
    if isinstance(value, ErrorInfo):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _encode(item) for key, item in value.items()}
    return value


def _decode(value: object, hint: object) -> object:
    if hint == JsonValue:
        return _json(value)
    origin = get_origin(hint)
    if hint is ErrorInfo:
        if not isinstance(value, dict):
            raise ValueError("error must be object")
        return ErrorInfo.from_dict(value)
    if isinstance(hint, type) and issubclass(hint, _Wire):
        if not isinstance(value, Mapping):
            raise ValueError("record must be object")
        return hint.from_dict(value)
    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint(value)
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError("tuple wire field must be array")
        args = get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(item, args[0]) for item in value)
        if len(value) != len(args):
            raise ValueError("tuple has invalid length")
        return tuple(_decode(item, member) for item, member in zip(value, args, strict=True))
    if origin in (dict, Mapping):
        if not isinstance(value, Mapping):
            raise ValueError("mapping wire field must be object")
        key_hint, value_hint = get_args(hint)
        return {_decode(key, key_hint): _decode(item, value_hint) for key, item in value.items()}
    if origin in (UnionType,) or str(origin) == "typing.Union":
        for member in get_args(hint):
            if member is type(None) and value is None:
                return None
            try:
                return _decode(value, member)
            except (TypeError, ValueError):
                continue
        raise ValueError("wire value does not match union")
    if hint is int:
        if type(value) is not int:
            raise ValueError("integer required")
    elif hint is bool:
        if type(value) is not bool:
            raise ValueError("boolean required")
    elif hint is str:
        if not isinstance(value, str):
            raise ValueError("string required")
    elif hint is type(None):
        if value is not None:
            raise ValueError("null required")
    return value


@dataclass(frozen=True, slots=True)
class PixelRect(_Wire):
    x: int
    y: int
    w: int
    h: int

    def __post_init__(self) -> None:
        if (
            any(type(v) is not int for v in (self.x, self.y, self.w, self.h))
            or self.w <= 0
            or self.h <= 0
        ):
            raise ValueError("invalid pixel rectangle")


@dataclass(frozen=True, slots=True)
class RatioRect(_Wire):
    x: str
    y: str
    w: str
    h: str

    def __post_init__(self) -> None:
        if (
            any(_decimal(v) != v for v in (self.x, self.y, self.w, self.h))
            or Decimal(self.w) <= 0
            or Decimal(self.h) <= 0
        ):
            raise ValueError("invalid ratio rectangle")


@dataclass(frozen=True, slots=True)
class CellEvidence(_Wire):
    index: int
    question: int | None
    digit: int | None
    choice: int | None
    pixel_rect: PixelRect | None
    ratio_rect: RatioRect | None
    fill_score: str | None
    selected: bool
    status: CellStatus

    def __post_init__(self) -> None:
        _int(self.index, minimum=0)
        if type(self.selected) is not bool or not isinstance(self.status, CellStatus):
            raise ValueError("invalid cell evidence")
        is_digit = self.digit is not None and self.question is None and self.choice is None
        is_answer = self.digit is None and self.question is not None and self.choice is not None
        if not (is_digit or is_answer):
            raise ValueError("invalid cell identity")
        if self.digit is not None and (type(self.digit) is not int or not 0 <= self.digit <= 9):
            raise ValueError("invalid digit")
        if self.question is not None and (
            type(self.question) is not int or not 1 <= self.question <= 100
        ):
            raise ValueError("invalid question")
        if self.choice is not None and (type(self.choice) is not int or not 1 <= self.choice <= 5):
            raise ValueError("invalid choice")
        if self.fill_score is not None:
            if not Decimal("0") <= Decimal(_decimal(self.fill_score)) <= Decimal("1"):
                raise ValueError("fill score must be between zero and one")


@dataclass(frozen=True, slots=True)
class IdCell(_Wire):
    selected_digit: str | None
    status: FieldStatus
    candidates: tuple[CellEvidence, ...]

    def __post_init__(self) -> None:
        _enum(self.status, FieldStatus)
        if not all(isinstance(candidate, CellEvidence) for candidate in self.candidates):
            raise ValueError("ID cell candidates must be cell evidence")
        if (
            len(self.candidates) != 10
            or {item.digit for item in self.candidates} != set(range(10))
            or any(item.question is not None for item in self.candidates)
        ):
            raise ValueError("ID cell needs ten digit candidates")
        normal = isinstance(self.selected_digit, str) and bool(
            re.fullmatch(r"[0-9]", self.selected_digit)
        )
        if (self.status is FieldStatus.NORMAL) != normal or (
            self.status is not FieldStatus.NORMAL and self.selected_digit is not None
        ):
            raise ValueError("ID cell status mismatch")
        selected = {item.digit for item in self.candidates if item.selected}
        expected = (
            {int(self.selected_digit)}
            if self.status is FieldStatus.NORMAL and self.selected_digit is not None
            else set()
        )
        if self.status in {FieldStatus.NORMAL, FieldStatus.BLANK} and selected != expected:
            raise ValueError("ID evidence does not match status")
        if self.status is FieldStatus.MULTIPLE and len(selected) < 2:
            raise ValueError("multiple ID status needs multiple selected candidates")


@dataclass(frozen=True, slots=True)
class StudentIdRecognition(_Wire):
    value: str | None
    status: StudentIdStatus
    cells: tuple[IdCell, ...]

    def __post_init__(self) -> None:
        _enum(self.status, StudentIdStatus)
        if self.status is StudentIdStatus.UNREADABLE:
            if self.value is not None or self.cells:
                raise ValueError("unreadable ID has no cells")
            return
        if len(self.cells) != 8:
            raise ValueError("student ID needs eight cells")
        complete = all(cell.status is FieldStatus.NORMAL for cell in self.cells)
        if (
            (self.status is StudentIdStatus.NORMAL) != complete
            or (
                complete and self.value != "".join(cell.selected_digit or "" for cell in self.cells)
            )
            or (not complete and self.value is not None)
        ):
            raise ValueError("student ID mismatch")


@dataclass(frozen=True, slots=True)
class AnswerValue(_Wire):
    choices: tuple[int, ...]
    status: AnswerStatus

    def __post_init__(self) -> None:
        _enum(self.status, AnswerStatus)
        if tuple(sorted(set(self.choices))) != self.choices or any(
            type(x) is not int or not 1 <= x <= 5 for x in self.choices
        ):
            raise ValueError("invalid answer choices")
        sizes = {
            AnswerStatus.NORMAL: {1},
            AnswerStatus.BLANK: {0},
            AnswerStatus.MULTIPLE: {2, 3, 4, 5},
            AnswerStatus.UNCERTAIN: {0, 1, 2, 3, 4, 5},
            AnswerStatus.ALL: {0},
            AnswerStatus.UNASKED: {0},
        }
        if len(self.choices) not in sizes[self.status]:
            raise ValueError("answer status mismatch")


@dataclass(frozen=True, slots=True)
class AnswerRecognition(_Wire):
    question: int
    value: AnswerValue
    cells: tuple[CellEvidence, ...]

    def __post_init__(self) -> None:
        if (
            type(self.question) is not int
            or not 1 <= self.question <= 100
            or len(self.cells) != 5
            or {item.choice for item in self.cells} != {1, 2, 3, 4, 5}
            or any(item.question != self.question for item in self.cells)
        ):
            raise ValueError("answer requires five cells")
        selected = tuple(item.choice for item in self.cells if item.selected)
        expected = {
            AnswerStatus.NORMAL: self.value.choices,
            AnswerStatus.BLANK: (),
            AnswerStatus.MULTIPLE: self.value.choices,
            AnswerStatus.UNCERTAIN: self.value.choices,
            AnswerStatus.ALL: (1, 2, 3, 4, 5),
            AnswerStatus.UNASKED: (),
        }[self.value.status]
        if selected != expected:
            raise ValueError("answer evidence does not match value status")


@dataclass(frozen=True, slots=True)
class PageRef(_Wire):
    schema_version: int
    session_id: str
    work_item_id: str
    source_kind: SourceKind
    source_sha256: str
    source_display_name: str
    source_label: str
    page_number: int | None
    frame_number: int | None
    input_ordinal: int
    duplicate_ordinal: int
    artifact_stem: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _enum(self.source_kind, SourceKind)
        _sha(self.source_sha256)
        _session_id(self.session_id)
        _text(self.work_item_id, "work_item_id")
        _text(self.source_display_name, "source_display_name")
        _text(self.source_label, "source_label")
        _text(self.artifact_stem, "artifact_stem")
        _int(self.input_ordinal, minimum=0)
        _int(self.duplicate_ordinal, minimum=0)
        for number in (self.page_number, self.frame_number):
            if number is not None:
                _int(number, minimum=1)


@dataclass(frozen=True, slots=True)
class AutomaticPage(_Wire):
    schema_version: int
    page_ref: PageRef
    processing_status: ProcessingStatus
    rotation_degrees: int | None
    orientation_confidence: str | None
    normalization_confidence: str | None
    normalized_size: tuple[int, int] | None
    homography_forward: tuple[str, ...] | None
    homography_inverse: tuple[str, ...] | None
    student_id: StudentIdRecognition
    answers: tuple[AnswerRecognition, ...]
    evidence: tuple[CellEvidence, ...]
    errors: tuple[ErrorInfo, ...] = ()
    extensions: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _enum(self.processing_status, ProcessingStatus)
        object.__setattr__(self, "extensions", _extensions(self.extensions))
        failed = self.processing_status in {ProcessingStatus.FAILED, ProcessingStatus.UNPROCESSABLE}
        geometry = (
            self.rotation_degrees,
            self.orientation_confidence,
            self.normalization_confidence,
            self.normalized_size,
            self.homography_forward,
            self.homography_inverse,
        )
        if failed:
            if (
                any(value is not None for value in geometry)
                or self.answers
                or self.evidence
                or self.student_id.status is not StudentIdStatus.UNREADABLE
                or self.student_id.cells
                or not self.errors
                or not all(
                    isinstance(item, ErrorInfo) and item.message_key.startswith("error.")
                    for item in self.errors
                )
            ):
                raise ValueError("failed page must be empty and diagnosed")
            return
        if (
            self.processing_status
            not in {ProcessingStatus.PROCESSED, ProcessingStatus.NEEDS_MANUAL_REVIEW}
            or self.rotation_degrees not in {0, 90, 180, 270}
            or any(value is None for value in geometry)
        ):
            raise ValueError("processed page geometry is incomplete")
        if (
            any(type(v) is not int or v <= 0 for v in self.normalized_size or ())
            or len(self.homography_forward or ()) != 9
            or len(self.homography_inverse or ()) != 9
        ):
            raise ValueError("invalid processed geometry")
        if any(
            _decimal(v) != v
            for v in (
                self.orientation_confidence,
                self.normalization_confidence,
                *(self.homography_forward or ()),
                *(self.homography_inverse or ()),
            )
        ):
            raise ValueError("noncanonical geometry confidence")
        if any(
            not Decimal("0") <= Decimal(cast(str, value)) <= Decimal("1")
            for value in (self.orientation_confidence, self.normalization_confidence)
        ):
            raise ValueError("confidence must be between zero and one")
        if (
            self.student_id.status is StudentIdStatus.UNREADABLE
            or len(self.student_id.cells) != 8
            or len(self.answers) != 100
            or tuple(item.question for item in self.answers) != tuple(range(1, 101))
        ):
            raise ValueError("processed page recognition incomplete")
        expected = tuple(
            cell for item in self.student_id.cells for cell in item.candidates
        ) + tuple(cell for item in self.answers for cell in item.cells)
        if self.evidence != expected:
            raise ValueError("processed evidence must be complete and ordered")


@dataclass(frozen=True, slots=True)
class EvidenceSummary(_Wire):
    id_cell_count: int
    answer_cell_count: int
    selected_cell_count: int

    def __post_init__(self) -> None:
        for value in (self.id_cell_count, self.answer_cell_count, self.selected_cell_count):
            _int(value, minimum=0)
        if (
            self.id_cell_count != 80
            or self.answer_cell_count != 500
            or self.selected_cell_count > 580
        ):
            raise ValueError("invalid evidence summary")


@dataclass(frozen=True, slots=True)
class PageFailure(_Wire):
    schema_version: int
    page_ref: PageRef
    errors: tuple[ErrorInfo, ...]
    evidence_summary: EvidenceSummary

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        if not self.errors or not all(
            isinstance(item, ErrorInfo) and item.message_key.startswith("error.")
            for item in self.errors
        ):
            raise ValueError("page failure needs error diagnostics")


@dataclass(frozen=True, slots=True)
class ImportedResponseRef(_Wire):
    schema_version: int
    work_item_id: str
    source_sha256: str
    sheet_name: str
    row_number: int
    input_ordinal: int
    serial: int
    source_filename: str
    raw_student_id: str
    name: str
    answers: tuple[AnswerValue, ...]
    note: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _sha(self.source_sha256)
        _text(self.work_item_id, "work_item_id")
        if len(self.answers) != 100:
            raise ValueError("imported response requires 100 answers")
        _int(self.row_number, minimum=1)
        _int(self.input_ordinal, minimum=0)
        _int(self.serial, minimum=1)


@dataclass(frozen=True, slots=True)
class RosterEntry(_Wire):
    roster_row_id: str
    source_row_number: int
    input_ordinal: int
    raw_student_id: str
    student_id: str | None
    name: str
    status: RosterRowStatus
    issues: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.roster_row_id, "roster_row_id")
        _int(self.source_row_number, minimum=1)
        _int(self.input_ordinal, minimum=0)
        _enum(self.status, RosterRowStatus)
        if self.student_id is not None and not _DIGITS.fullmatch(self.student_id):
            raise ValueError("invalid roster student ID")
        allowed_issues = {
            RosterRowStatus.INVALID_ID.value,
            RosterRowStatus.DUPLICATE_ID.value,
            RosterRowStatus.NAME_CONFLICT.value,
        }
        if (
            tuple(sorted(set(self.issues))) != self.issues
            or any(type(item) is not str or item not in allowed_issues for item in self.issues)
            or (self.student_id is None) != (RosterRowStatus.INVALID_ID.value in self.issues)
            or (self.student_id is None and self.issues != (RosterRowStatus.INVALID_ID.value,))
        ):
            raise ValueError("roster issues are inconsistent")
        expected_status = next(
            (
                status
                for status in (
                    RosterRowStatus.INVALID_ID,
                    RosterRowStatus.DUPLICATE_ID,
                    RosterRowStatus.NAME_CONFLICT,
                )
                if status.value in self.issues
            ),
            RosterRowStatus.NORMAL,
        )
        if self.status is not expected_status:
            raise ValueError("roster status does not match issues")


@dataclass(frozen=True, slots=True)
class RosterSnapshot(_Wire):
    schema_version: int
    snapshot_kind: RosterSnapshotKind
    source_name: str | None
    source_sha256: str | None
    sheet_name: str | None
    normalization_version: str
    rows: tuple[RosterEntry, ...]
    validation_errors: tuple[ErrorInfo, ...]
    extensions: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        object.__setattr__(self, "extensions", _extensions(self.extensions))
        _enum(self.snapshot_kind, RosterSnapshotKind)
        _text(self.normalization_version, "normalization_version")
        if self.source_sha256 is not None:
            _sha(self.source_sha256)
        if not all(isinstance(row, RosterEntry) for row in self.rows) or not all(
            isinstance(error, ErrorInfo) and error.message_key.startswith("error.")
            for error in self.validation_errors
        ):
            raise ValueError("invalid roster snapshot contents")
        none = self.snapshot_kind is RosterSnapshotKind.NONE
        if none != (
            self.source_name is None
            and self.source_sha256 is None
            and self.sheet_name is None
            and not self.rows
            and not self.validation_errors
        ):
            raise ValueError("roster snapshot provenance mismatch")
        if not none and (
            not isinstance(self.source_name, str)
            or not self.source_name
            or not isinstance(self.sheet_name, str)
            or not self.sheet_name
            or self.source_sha256 is None
        ):
            raise ValueError("roster snapshot source provenance is incomplete")
        groups: dict[str, list[RosterEntry]] = {}
        for row in self.rows:
            if row.student_id is not None:
                groups.setdefault(row.student_id, []).append(row)
        for group in groups.values():
            duplicate = len(group) > 1
            name_conflict = len({row.name for row in group}) > 1
            for row in group:
                issues = set(row.issues)
                if (RosterRowStatus.DUPLICATE_ID.value in issues) != duplicate or (
                    RosterRowStatus.NAME_CONFLICT.value in issues
                ) != name_conflict:
                    raise ValueError("roster group issues are inconsistent")


@dataclass(frozen=True, slots=True)
class AnswerKeyEntry(_Wire):
    question: int
    answer: AnswerValue
    points: str
    status: KeyQuestionStatus

    def __post_init__(self) -> None:
        _int(self.question, minimum=1)
        _enum(self.status, KeyQuestionStatus)
        if (
            not isinstance(self.answer, AnswerValue)
            or self.question > 100
            or Decimal(_decimal(self.points)) < 0
        ):
            raise ValueError("invalid answer key entry")
        expected_statuses = {
            KeyQuestionStatus.ANSWER: {AnswerStatus.NORMAL, AnswerStatus.MULTIPLE},
            KeyQuestionStatus.ALL: {AnswerStatus.ALL},
            KeyQuestionStatus.UNASKED: {AnswerStatus.UNASKED},
        }[self.status]
        if self.answer.status not in expected_statuses or (
            self.status is KeyQuestionStatus.UNASKED and self.points != "0"
        ):
            raise ValueError("key status mismatch")


@dataclass(frozen=True, slots=True)
class AnswerKeySnapshot(_Wire):
    schema_version: int
    snapshot_kind: AnswerKeySnapshotKind
    source_name: str | None
    source_sha256: str | None
    sheet_name: str | None
    normalization_version: str
    entries: tuple[AnswerKeyEntry, ...]
    validation_errors: tuple[ErrorInfo, ...]
    extensions: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        object.__setattr__(self, "extensions", _extensions(self.extensions))
        _enum(self.snapshot_kind, AnswerKeySnapshotKind)
        _text(self.normalization_version, "normalization_version")
        if self.source_sha256 is not None:
            _sha(self.source_sha256)
        if not all(isinstance(entry, AnswerKeyEntry) for entry in self.entries) or not all(
            isinstance(error, ErrorInfo) and error.message_key.startswith("error.")
            for error in self.validation_errors
        ):
            raise ValueError("invalid key snapshot contents")
        if len(self.entries) != 100 or tuple(item.question for item in self.entries) != tuple(
            range(1, 101)
        ):
            raise ValueError("key requires Q1 through Q100")
        if self.snapshot_kind is AnswerKeySnapshotKind.UNSET and (
            self.source_name is not None
            or self.source_sha256 is not None
            or self.sheet_name is not None
            or self.validation_errors
            or any(item.status is not KeyQuestionStatus.UNASKED for item in self.entries)
        ):
            raise ValueError("unset key invariant failed")
        if self.snapshot_kind is AnswerKeySnapshotKind.WORKBOOK and (
            not isinstance(self.source_name, str)
            or not self.source_name
            or not isinstance(self.sheet_name, str)
            or not self.sheet_name
            or self.source_sha256 is None
        ):
            raise ValueError("key snapshot source provenance is incomplete")


@dataclass(frozen=True, slots=True)
class IdCorrectionValue(_Wire):
    digit: str | None
    status: FieldStatus

    def __post_init__(self) -> None:
        _enum(self.status, FieldStatus)
        normal = isinstance(self.digit, str) and bool(re.fullmatch(r"[0-9]", self.digit))
        if (self.status is FieldStatus.NORMAL) != normal or (
            self.status is not FieldStatus.NORMAL and self.digit is not None
        ):
            raise ValueError("ID correction status mismatch")


@dataclass(frozen=True, slots=True)
class CorrectionDraft(_Wire):
    work_item_id: str
    target_kind: TargetKind
    target_key: int
    before: IdCorrectionValue | AnswerValue
    after: IdCorrectionValue | AnswerValue
    reason: str

    def __post_init__(self) -> None:
        _text(self.work_item_id, "work_item_id")
        _text(self.reason, "reason")
        _enum(self.target_kind, TargetKind)
        valid = (
            self.target_kind is TargetKind.ID_CELL
            and 0 <= self.target_key <= 7
            and isinstance(self.before, IdCorrectionValue)
            and isinstance(self.after, IdCorrectionValue)
        ) or (
            self.target_kind is TargetKind.ANSWER_CELL
            and 1 <= self.target_key <= 100
            and isinstance(self.before, AnswerValue)
            and isinstance(self.after, AnswerValue)
        )
        if type(self.target_key) is not int or not valid:
            raise ValueError("correction target mismatch")


@dataclass(frozen=True, slots=True)
class CorrectionEvent(_Wire):
    schema_version: int
    event_id: str
    session_id: str
    work_item_id: str
    target_kind: TargetKind
    target_key: int
    expected_base_revision: int
    before: IdCorrectionValue | AnswerValue
    after: IdCorrectionValue | AnswerValue
    reason: str
    local_actor: str
    created_at: str
    committed_revision: int
    idempotency_key: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _timestamp(self.created_at)
        CorrectionDraft(
            self.work_item_id,
            self.target_kind,
            self.target_key,
            self.before,
            self.after,
            self.reason,
        )
        for text in (self.event_id, self.session_id, self.local_actor, self.idempotency_key):
            _text(text, "identifier")
        _int(self.expected_base_revision, minimum=1)
        _int(self.committed_revision, minimum=1)


@dataclass(frozen=True, slots=True)
class EffectiveResponse(_Wire):
    work_item_id: str
    source_kind: SourceKind
    source_label: str
    student_id: str | None
    student_id_status: StudentIdStatus
    answers: tuple[AnswerValue, ...]
    corrected_targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.work_item_id, "work_item_id")
        _text(self.source_label, "source_label")
        _enum(self.source_kind, SourceKind)
        _enum(self.student_id_status, StudentIdStatus)
        has_valid_id = isinstance(self.student_id, str) and bool(_DIGITS.fullmatch(self.student_id))
        if (
            len(self.answers) != 100
            or not all(isinstance(answer, AnswerValue) for answer in self.answers)
            or (self.student_id is not None and not has_valid_id)
            or (self.student_id_status in {StudentIdStatus.NORMAL, StudentIdStatus.DUPLICATE})
            != has_valid_id
        ):
            raise ValueError("effective response mismatch")
        parsed: list[tuple[int, int]] = []
        for target in self.corrected_targets:
            match = re.fullmatch(r"(id_cell|answer_cell):(\d+)", target)
            if match is None:
                raise ValueError("invalid corrected target")
            kind, number = match.groups()
            index = int(number)
            if (kind == "id_cell" and not 0 <= index <= 7) or (
                kind == "answer_cell" and not 1 <= index <= 100
            ):
                raise ValueError("corrected target out of range")
            parsed.append((0 if kind == "id_cell" else 1, index))
        if len(set(parsed)) != len(parsed) or parsed != sorted(parsed):
            raise ValueError("corrected targets must be unique and ordered")


@dataclass(frozen=True, slots=True)
class ManifestFile(_Wire):
    path: str
    size: int
    sha256: str
    media_type: str

    def __post_init__(self) -> None:
        _path(self.path)
        _int(self.size, minimum=0)
        _sha(self.sha256)
        _text(self.media_type, "media_type")


@dataclass(frozen=True, slots=True)
class ManifestSummary(_Wire):
    work_items: int
    processable: int
    manual_review: int
    maximum_score: str | None

    def __post_init__(self) -> None:
        for value in (self.work_items, self.processable, self.manual_review):
            _int(value, minimum=0)
        if self.processable > self.work_items or self.manual_review > self.work_items:
            raise ValueError("manifest summary counts are inconsistent")
        if self.maximum_score is not None and Decimal(_decimal(self.maximum_score)) < 0:
            raise ValueError("maximum score must be nonnegative")


@dataclass(frozen=True, slots=True)
class SessionManifest(_Wire):
    schema_version: int
    session_id: str
    revision: int
    generation_id: str
    parent_revision: int | None
    parent_generation_id: str | None
    parent_manifest_sha256: str | None
    operation_id: str
    operation_kind: OperationKind
    app_version: str
    created_at: str
    state: SessionState
    base_response_ids: tuple[str, ...]
    profile_sha256: str | None
    roster_sha256: str
    key_sha256: str
    threshold_version: str | None
    threshold_sha256: str | None
    files: tuple[ManifestFile, ...]
    summary: ManifestSummary

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _timestamp(self.created_at)
        for value, label in (
            (self.session_id, "session_id"),
            (self.generation_id, "generation_id"),
            (self.operation_id, "operation_id"),
            (self.app_version, "app_version"),
        ):
            _text(value, label)
        _int(self.revision, minimum=1)
        _enum(self.operation_kind, OperationKind)
        _enum(self.state, SessionState)
        parent_fields = (
            self.parent_revision,
            self.parent_generation_id,
            self.parent_manifest_sha256,
        )
        if self.revision == 1:
            if any(value is not None for value in parent_fields):
                raise ValueError("revision one has no parent manifest")
        elif any(value is None for value in parent_fields):
            raise ValueError("revision requires a complete parent manifest")
        else:
            _int(self.parent_revision, minimum=1)
            if self.parent_revision != self.revision - 1:
                raise ValueError("parent revision must immediately precede revision")
            _text(self.parent_generation_id, "parent_generation_id")
            _sha(self.parent_manifest_sha256)
        if self.profile_sha256 is not None:
            _sha(self.profile_sha256)
        _sha(self.roster_sha256)
        _sha(self.key_sha256)
        if (self.threshold_version is None) != (self.threshold_sha256 is None):
            raise ValueError("threshold provenance must be complete or absent")
        if self.threshold_version is not None:
            _text(self.threshold_version, "threshold_version")
            _sha(self.threshold_sha256)
        if not all(isinstance(item, str) and item for item in self.base_response_ids):
            raise ValueError("base response IDs must be nonempty strings")
        if self.base_response_ids != tuple(
            sorted(set(self.base_response_ids), key=lambda identifier: identifier.encode("utf-8"))
        ):
            raise ValueError("base response IDs must be unique UTF-8 sorted")
        if not all(isinstance(item, ManifestFile) for item in self.files):
            raise ValueError("manifest files must be ManifestFile values")
        if not isinstance(self.summary, ManifestSummary):
            raise ValueError("manifest summary must be a ManifestSummary")
        _ordered_paths(tuple(item.path for item in self.files), "manifest files")


@dataclass(frozen=True, slots=True)
class SessionRecord(_Wire):
    schema_version: int
    session_id: str
    revision: int
    state: SessionState
    exam_name: str
    exam_year: int | None
    exam_term: ExamTerm
    created_at: str
    graded_at: str | None
    updated_at: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _session_id(self.session_id)
        _text(self.exam_name, "exam_name")
        _enum(self.state, SessionState)
        _enum(self.exam_term, ExamTerm)
        _int(self.revision, minimum=1)
        _timestamp(self.created_at)
        _timestamp(self.updated_at)
        if self.graded_at is not None:
            _timestamp(self.graded_at)
        if self.exam_year is not None and (
            type(self.exam_year) is not int or not 2000 <= self.exam_year <= 2100
        ):
            raise ValueError("invalid exam year")


@dataclass(frozen=True, slots=True)
class IdentityRecord(_Wire):
    schema_version: int
    session_id: str
    created_at: str
    creation_kind: CreationKind

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _session_id(self.session_id)
        _timestamp(self.created_at)
        _enum(self.creation_kind, CreationKind)


@dataclass(frozen=True, slots=True)
class SessionReservation(_Wire):
    schema_version: int
    session_id: str
    operation_id: str
    creation_kind: CreationKind
    created_at: str
    display_name: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _session_id(self.session_id)
        _text(self.operation_id, "operation_id")
        _enum(self.creation_kind, CreationKind)
        _timestamp(self.created_at)
        _text(self.display_name, "display_name")


@dataclass(frozen=True, slots=True)
class DeleteTombstone(_Wire):
    schema_version: int
    session_id: str
    operation_id: str
    committed_at: str
    generation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _session_id(self.session_id)
        _text(self.operation_id, "operation_id")
        _timestamp(self.committed_at)
        if (
            not self.generation_ids
            or not all(
                isinstance(generation_id, str) and generation_id
                for generation_id in self.generation_ids
            )
            or len(set(self.generation_ids)) != len(self.generation_ids)
        ):
            raise ValueError("generation IDs must be nonempty and unique")


@dataclass(frozen=True, slots=True)
class OmittedParent(_Wire):
    revision: int
    generation_id: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        _int(self.revision, minimum=1)
        _text(self.generation_id, "generation_id")
        _sha(self.manifest_sha256)


@dataclass(frozen=True, slots=True)
class RestoreProvenance(_Wire):
    schema_version: int
    session_id: str
    archive_sha256: str
    boundary_revision: int
    boundary_generation_id: str
    boundary_manifest_sha256: str
    omitted_parent: OmittedParent | None
    restored_at: str
    lineage_state: LineageState

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _session_id(self.session_id)
        _sha(self.archive_sha256)
        _int(self.boundary_revision, minimum=1)
        _text(self.boundary_generation_id, "boundary_generation_id")
        _sha(self.boundary_manifest_sha256)
        if (self.boundary_revision == 1 and self.omitted_parent is not None) or (
            self.boundary_revision > 1
            and (
                not isinstance(self.omitted_parent, OmittedParent)
                or self.omitted_parent.revision != self.boundary_revision - 1
            )
        ):
            raise ValueError("omitted parent must be the immediate boundary parent")
        _timestamp(self.restored_at)
        _enum(self.lineage_state, LineageState)


@dataclass(frozen=True, slots=True)
class ArchiveEntry(_Wire):
    path: str
    media_type: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        _path(self.path)
        _text(self.media_type, "media_type")
        _int(self.size, minimum=0)
        _sha(self.sha256)


@dataclass(frozen=True, slots=True)
class ArchiveManifest(_Wire):
    schema_version: int
    format_version: int
    app_version: str
    exported_at: str
    lineage_mode: ArchiveLineageMode
    session_id: str
    revision: int
    generation_id: str
    manifest_sha256: str
    omitted_parent: OmittedParent | None
    contains_personal_data: bool
    entries: tuple[ArchiveEntry, ...]

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        if type(self.format_version) is not int or self.format_version != 1:
            raise ValueError("unsupported archive format version")
        _text(self.app_version, "app_version")
        _timestamp(self.exported_at)
        _enum(self.lineage_mode, ArchiveLineageMode)
        _session_id(self.session_id)
        _int(self.revision, minimum=1)
        _text(self.generation_id, "generation_id")
        _sha(self.manifest_sha256)
        if (self.revision == 1 and self.omitted_parent is not None) or (
            self.revision > 1
            and (
                not isinstance(self.omitted_parent, OmittedParent)
                or self.omitted_parent.revision != self.revision - 1
            )
        ):
            raise ValueError("omitted parent must be the immediate manifest parent")
        if type(self.contains_personal_data) is not bool:
            raise ValueError("contains_personal_data must be boolean")
        if not all(isinstance(item, ArchiveEntry) for item in self.entries):
            raise ValueError("archive entries must be ArchiveEntry values")
        _ordered_paths(tuple(item.path for item in self.entries), "archive entries")


@dataclass(frozen=True, slots=True)
class CurrentPointer(_Wire):
    schema_version: int
    session_id: str
    revision: int
    generation_id: str
    generation_relpath: str
    manifest_sha256: str
    committed_at: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _session_id(self.session_id)
        _int(self.revision, minimum=1)
        _text(self.generation_id, "generation_id")
        _path(self.generation_relpath)
        _sha(self.manifest_sha256)
        _timestamp(self.committed_at)


@dataclass(frozen=True, slots=True)
class DashboardIndexEntry(_Wire):
    session_id: str
    revision: int
    generation_id: str
    manifest_sha256: str
    display_folder: str
    exam_name: str
    exam_year: int | None
    exam_term: ExamTerm
    state: SessionState
    graded_at: str | None
    participant_count: int
    average_score: str | None
    highest_score: str | None
    lowest_score: str | None
    needs_review_count: int

    def __post_init__(self) -> None:
        _enum(self.exam_term, ExamTerm)
        _enum(self.state, SessionState)
        _session_id(self.session_id)
        _int(self.revision, minimum=1)
        _text(self.generation_id, "generation_id")
        _sha(self.manifest_sha256)
        _text(self.display_folder, "display_folder")
        _text(self.exam_name, "exam_name")
        if self.graded_at is not None:
            _timestamp(self.graded_at)
        if self.exam_year is not None and (
            type(self.exam_year) is not int or not 2000 <= self.exam_year <= 2100
        ):
            raise ValueError("invalid exam year")
        for number in (self.participant_count, self.needs_review_count):
            _int(number, minimum=0)
        for score in (self.average_score, self.highest_score, self.lowest_score):
            if score is not None and Decimal(_decimal(score)) < 0:
                raise ValueError("scores must be canonical nonnegative decimals")


@dataclass(frozen=True, slots=True)
class DashboardIndexRecord(_Wire):
    schema_version: int
    built_at: str
    source_digest: str
    entries: tuple[DashboardIndexEntry, ...]

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _timestamp(self.built_at)
        _sha(self.source_digest, "source_digest")
        if tuple(item.session_id for item in self.entries) != tuple(
            sorted(item.session_id for item in self.entries)
        ):
            raise ValueError("index entries must be sorted")
