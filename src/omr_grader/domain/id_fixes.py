"""Student-ID fixes a teacher makes after reading: a separate layer over what was read.

A student who marks the wrong ID bubble gets a sheet that reads as someone else.  The teacher
types the right ID; the fix is stored as its own record (``id_fixes.json`` in a generation)
and laid over the read responses wherever results are shown or combined.  What was read is
never changed, so removing a fix brings the read ID back.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from .enums import StudentIdStatus
from .models import EffectiveResponse

ID_FIXES_FILE = "id_fixes.json"
_STUDENT_ID = re.compile(r"[0-9]{8}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")


@dataclass(frozen=True, slots=True)
class StudentIdFix:
    """One answer sheet's ID as the teacher set it; ``read_id`` is what the sheet read as."""

    work_item_id: str
    student_id: str
    read_id: str | None
    fixed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.work_item_id, str) or not self.work_item_id:
            raise ValueError("work_item_id must be nonempty")
        if not isinstance(self.student_id, str) or _STUDENT_ID.fullmatch(self.student_id) is None:
            raise ValueError("a fixed student ID has exactly 8 digits")
        if self.read_id is not None and (
            not isinstance(self.read_id, str) or re.fullmatch(r"[0-9]{1,8}", self.read_id) is None
        ):
            raise ValueError("read_id must be digits or None")
        if not isinstance(self.fixed_at, str) or _TIMESTAMP.fullmatch(self.fixed_at) is None:
            raise ValueError("fixed_at must be a UTC timestamp")

    def to_dict(self) -> dict[str, object]:
        return {
            "work_item_id": self.work_item_id,
            "student_id": self.student_id,
            "read_id": self.read_id,
            "fixed_at": self.fixed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> StudentIdFix:
        if set(value) != {"work_item_id", "student_id", "read_id", "fixed_at"}:
            raise ValueError("student ID fix fields are invalid")
        read_id = value["read_id"]
        return cls(
            str(value["work_item_id"]),
            str(value["student_id"]),
            None if read_id is None else str(read_id),
            str(value["fixed_at"]),
        )


def fixes_to_wire(fixes: Sequence[StudentIdFix]) -> dict[str, object]:
    ordered = sorted(fixes, key=lambda fix: fix.work_item_id)
    return {"schema_version": 1, "fixes": [fix.to_dict() for fix in ordered]}


def fixes_from_wire(value: object) -> tuple[StudentIdFix, ...]:
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "fixes"}:
        raise ValueError("student ID fixes document is invalid")
    if value["schema_version"] != 1 or not isinstance(value["fixes"], list):
        raise ValueError("student ID fixes document is invalid")
    fixes = tuple(StudentIdFix.from_dict(item) for item in value["fixes"])
    validate_fixes(fixes)
    return fixes


def validate_fixes(fixes: Sequence[StudentIdFix], work_item_ids: Iterable[str] | None = None) -> None:
    """One fix per sheet, and only for sheets the exam has."""
    seen = [fix.work_item_id for fix in fixes]
    if len(set(seen)) != len(seen):
        raise ValueError("a sheet has more than one ID fix")
    if work_item_ids is not None and not set(seen) <= set(work_item_ids):
        raise ValueError("an ID fix names a sheet the exam does not have")


def apply_id_fixes(
    responses: Sequence[EffectiveResponse], fixes: Sequence[StudentIdFix]
) -> tuple[EffectiveResponse, ...]:
    """The responses as results show them: fixed IDs set, duplicate marks recounted.

    Without fixes the responses come back unchanged.  With fixes every sheet holding a valid
    ID is NORMAL, or DUPLICATE when another sheet holds the same ID, so a fix that resolves
    a clash clears the other sheet as well.
    """
    if not fixes:
        return tuple(responses)
    by_sheet = {fix.work_item_id: fix.student_id for fix in fixes}
    fixed = [
        replace(response, student_id=by_sheet[response.work_item_id], student_id_status=StudentIdStatus.NORMAL)
        if response.work_item_id in by_sheet
        else response
        for response in responses
    ]
    valid = {StudentIdStatus.NORMAL, StudentIdStatus.DUPLICATE}
    counts = Counter(
        response.student_id for response in fixed if response.student_id_status in valid
    )
    return tuple(
        replace(
            response,
            student_id_status=(
                StudentIdStatus.DUPLICATE
                if counts[response.student_id] > 1
                else StudentIdStatus.NORMAL
            ),
        )
        if response.student_id_status in valid
        else response
        for response in fixed
    )


def read_ids(fixes: Sequence[StudentIdFix]) -> dict[str, str]:
    """The ID each fixed sheet read as, for the 비고 note ('' when it could not be read)."""
    return {fix.work_item_id: fix.read_id or "" for fix in fixes}


__all__ = [
    "ID_FIXES_FILE",
    "StudentIdFix",
    "apply_id_fixes",
    "fixes_from_wire",
    "fixes_to_wire",
    "read_ids",
    "validate_fixes",
]
