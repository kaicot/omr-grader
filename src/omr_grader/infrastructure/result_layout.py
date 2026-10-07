"""Human-readable result folder and artifact naming."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

OCR_IMAGE_DIR = "01인식결과이미지"
SCORE_IMAGE_DIR = "02채점결과이미지"
COORDINATE_DIR = "좌표데이터"
REVIEW_DIR = "수동확인필요"
SOURCE_IMAGE_DIR = "01원본스캔"
ANSWER_KEY_SOURCE_DIR = "정답표원본"
RESPONSE_KIND = "응답결과"
SCORE_KIND = "채점결과"
FINAL_KIND = "최종성적표"
ANSWER_KEY_KIND = "정답표"
RESULT_WORKBOOK_KINDS = (RESPONSE_KIND, SCORE_KIND, FINAL_KIND, ANSWER_KEY_KIND)

_KST = ZoneInfo("Asia/Seoul")
_UNSAFE = re.compile(r"""[\s<>:"/\\|?*\x00-\x1f]+""")
_UNDERSCORES = re.compile(r"_+")
_RESULT_WORKBOOK = re.compile(
    rf"\d{{6}}_\d{{6}}_(?P<kind>{'|'.join(RESULT_WORKBOOK_KINDS)})_[^/\\]+\.xlsx"
)


def safe_exam_name(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("exam name must be a string")
    cleaned = _UNDERSCORES.sub("_", _UNSAFE.sub("_", value)).strip("._ ")
    return cleaned[:80] or "시험"


def result_timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    try:
        instant = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=ZoneInfo("UTC")
        )
    except ValueError as error:
        raise ValueError("timestamp must be UTC RFC3339 with microseconds") from error
    return instant.astimezone(_KST).strftime("%y%m%d_%H%M%S")


def result_base_name(exam_name: str, created_at: str) -> str:
    return f"{safe_exam_name(exam_name)}_{result_timestamp(created_at)}"


def result_workbook_filename(kind: str, exam_name: str, created_at: str) -> str:
    """``<yymmdd>_<HHMMSS>_<kind>_<exam name>.xlsx``, stamped with the session's creation time."""
    if kind not in RESULT_WORKBOOK_KINDS:
        raise ValueError("unknown result workbook kind")
    return f"{result_timestamp(created_at)}_{kind}_{safe_exam_name(exam_name)}.xlsx"


def result_workbook_kind(name: str) -> str | None:
    """The kind of a result workbook file name; None for any other name or relative path."""
    match = _RESULT_WORKBOOK.fullmatch(name)
    return match.group("kind") if match else None


def response_filename(exam_name: str, created_at: str) -> str:
    return result_workbook_filename(RESPONSE_KIND, exam_name, created_at)


def answer_key_filename(exam_name: str, created_at: str) -> str:
    return result_workbook_filename(ANSWER_KEY_KIND, exam_name, created_at)


def external_artifact_relpath(path: str) -> str | None:
    """Map a heavy generation path to its single session-root location."""
    mappings = (
        ("images/", f"{SOURCE_IMAGE_DIR}/"),
        ("sources/scans/", f"{SOURCE_IMAGE_DIR}/"),
        ("sources/answer_keys/", f"{ANSWER_KEY_SOURCE_DIR}/"),
        (f"{SCORE_IMAGE_DIR}/", f"{SCORE_IMAGE_DIR}/"),
    )
    for prefix, target in mappings:
        if path.startswith(prefix) and len(path) > len(prefix):
            return target + path[len(prefix) :]
    return None


__all__ = [
    "ANSWER_KEY_KIND",
    "ANSWER_KEY_SOURCE_DIR",
    "COORDINATE_DIR",
    "FINAL_KIND",
    "OCR_IMAGE_DIR",
    "RESPONSE_KIND",
    "RESULT_WORKBOOK_KINDS",
    "REVIEW_DIR",
    "SCORE_IMAGE_DIR",
    "SCORE_KIND",
    "SOURCE_IMAGE_DIR",
    "answer_key_filename",
    "external_artifact_relpath",
    "response_filename",
    "result_base_name",
    "result_timestamp",
    "result_workbook_filename",
    "result_workbook_kind",
    "safe_exam_name",
]
