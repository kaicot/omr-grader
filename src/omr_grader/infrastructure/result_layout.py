"""Human-readable result folder and artifact naming."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

OCR_IMAGE_DIR = "01_인식결과_이미지"
SCORE_IMAGE_DIR = "02_채점결과_이미지"
COORDINATE_DIR = "좌표데이터"
REVIEW_DIR = "수동확인필요"
SOURCE_IMAGE_DIR = "00_원본스캔"

_KST = ZoneInfo("Asia/Seoul")
_UNSAFE = re.compile(r"""[\s<>:"/\\|?*\x00-\x1f]+""")
_UNDERSCORES = re.compile(r"_+")


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


def ocr_filename(exam_name: str, created_at: str) -> str:
    return f"01_ocr_{result_base_name(exam_name, created_at)}_응답결과.xlsx"


def answer_key_filename(exam_name: str, created_at: str) -> str:
    return f"정답표_{result_base_name(exam_name, created_at)}.xlsx"


__all__ = [
    "COORDINATE_DIR",
    "OCR_IMAGE_DIR",
    "REVIEW_DIR",
    "SCORE_IMAGE_DIR",
    "SOURCE_IMAGE_DIR",
    "answer_key_filename",
    "ocr_filename",
    "result_base_name",
    "result_timestamp",
    "safe_exam_name",
]
