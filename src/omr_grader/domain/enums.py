"""Pure domain enumeration contracts."""

from enum import StrEnum


class SessionState(StrEnum):
    CREATED = "created"
    RECOGNIZED = "recognized"
    GRADED = "graded"
    FINALIZED = "finalized"


class OperationKind(StrEnum):
    CREATE = "create"
    RECOGNIZE = "recognize"
    IMPORT_RESPONSES = "import_responses"
    CORRECT = "correct"
    REGRADE = "regrade"
    FINALIZE = "finalize"
    METADATA_EDIT = "metadata_edit"


class SourceKind(StrEnum):
    IMAGE = "image"
    PDF = "pdf"
    TIFF = "tiff"
    IMPORTED_XLSX = "imported_xlsx"


class ExamTerm(StrEnum):
    FIRST = "first"
    SECOND = "second"
    SUMMER = "summer"
    WINTER = "winter"
    OTHER = "other"
    UNSPECIFIED = "unspecified"


class IndexState(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNCHANGED = "unchanged"


class CleanupState(StrEnum):
    COMPLETE = "complete"
    PENDING = "pending"


class ProcessingStatus(StrEnum):
    PROCESSED = "processed"
    FAILED = "failed"
    UNPROCESSABLE = "unprocessable"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"


class FieldStatus(StrEnum):
    NORMAL = "normal"
    BLANK = "blank"
    MULTIPLE = "multiple"
    UNCERTAIN = "uncertain"


class CellStatus(StrEnum):
    NORMAL = "normal"
    BLANK = "blank"
    MULTIPLE = "multiple"
    UNCERTAIN = "uncertain"


class AnswerStatus(StrEnum):
    NORMAL = "normal"
    BLANK = "blank"
    MULTIPLE = "multiple"
    UNCERTAIN = "uncertain"
    ALL = "all"
    UNASKED = "unasked"


class StudentIdStatus(StrEnum):
    NORMAL = "normal"
    UNREADABLE = "unreadable"
    INVALID = "invalid"
    DUPLICATE = "duplicate"


class TargetKind(StrEnum):
    ID_CELL = "id_cell"
    ANSWER_CELL = "answer_cell"


class SnapshotPurpose(StrEnum):
    DETAIL = "detail"
    BACKUP = "backup"
    COMBINED = "combined"
    RECOVERY = "recovery"


class RosterRowStatus(StrEnum):
    NORMAL = "normal"
    DUPLICATE_ID = "duplicate_id"
    INVALID_ID = "invalid_id"
    NAME_CONFLICT = "name_conflict"


class RosterSnapshotKind(StrEnum):
    NONE = "none"
    WORKBOOK = "workbook"
    IMPORTED_RESPONSE = "imported_response"


class AnswerKeySnapshotKind(StrEnum):
    UNSET = "unset"
    WORKBOOK = "workbook"


class KeyQuestionStatus(StrEnum):
    ANSWER = "answer"
    ALL = "all"
    UNASKED = "unasked"


class CreationKind(StrEnum):
    SCAN = "scan"
    IMPORTED_RESPONSES = "imported_responses"
    RESTORE = "restore"


class LineageState(StrEnum):
    VALID_TRUNCATED_ANCESTOR = "valid_truncated_ancestor"


class ArchiveLineageMode(StrEnum):
    CURRENT_ONLY = "current_only"
