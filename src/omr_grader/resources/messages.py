"""Korean text for stable user-facing message keys."""

from __future__ import annotations

from collections.abc import Iterable

MESSAGE_CATALOG: dict[str, str] = {
    "app.title": "OMR Grader",
    "app.organization": "OMR Grader",
    "bootstrap.read_only_title": "읽기 전용 실행",
    "bootstrap.read_only_body": (
        "실행 폴더에 쓸 수 없어 읽기 전용으로 실행합니다. 폴더 권한을 확인한 뒤 다시 실행하세요."
    ),
    "bootstrap.unavailable_title": "실행 폴더를 사용할 수 없음",
    "bootstrap.unavailable_body": (
        "포터블 실행 폴더를 확인할 수 없습니다. 프로그램 위치와 폴더 권한을 확인하세요."
    ),
    "bootstrap.shell_body": "OMR Grader를 시작했습니다.",
    "error.atomic_write_failed": "파일을 안전하게 저장하지 못했습니다.",
    "error.backup_handle_closed": "백업 파일 검증 연결이 이미 닫혔습니다.",
    "error.validation_token_close_failed": "파일 검증 연결을 닫지 못했습니다.",
    "error.backup_source_changed": "백업 파일이 검증 후 변경되었거나 사용할 수 없습니다.",
    "error.config_invalid": "설정 값이 올바르지 않습니다.",
    "error.config_missing": "설정 파일이 없어 기본 설정을 사용합니다.",
    "error.invalid_capability_token": "쓰기 권한 토큰이 현재 포터블 경로와 일치하지 않습니다.",
    "error.invalid_config": "설정 형식이 올바르지 않습니다.",
    "error.invalid_default_profile": "기본 프로필은 .omrtemplate 파일명이어야 합니다.",
    "error.invalid_json_value": "저장할 JSON 값이 올바르지 않습니다.",
    "error.invalid_managed_path": "안전하지 않은 관리 경로 구성 요소입니다.",
    "error.invalid_write_payload": "저장할 데이터는 바이트여야 합니다.",
    "error.logging_setup_failed": "로그 파일을 열지 못해 표준 오류 로그만 사용합니다.",
    "error.managed_path_escape": "관리 경로가 포터블 루트를 벗어났습니다.",
    "error.managed_path_invalid": "관리 폴더 또는 설정 파일 경로가 올바르지 않습니다.",
    "error.portable_root_invalid": "포터블 실행 경로가 폴더가 아닙니다.",
    "error.portable_root_unavailable": "포터블 실행 폴더를 확인할 수 없습니다.",
    "error.root_write_denied": "실행 폴더에 쓸 권한이 없습니다.",
    "error.xlsx_source_changed": "엑셀 원본 파일이 검증 후 변경되었거나 사용할 수 없습니다.",
    "error.xlsx_validation_token_closed": "엑셀 파일 검증 연결이 이미 닫혔습니다.",
    "warning.atomic_write_failed": "파일을 안전하게 저장하지 못했습니다.",
    "warning.backup_handle_closed": "백업 파일 검증 연결이 이미 닫혔습니다.",
    "warning.backup_source_changed": "백업 파일이 검증 후 변경되었거나 사용할 수 없습니다.",
    "warning.config_invalid": "설정 값이 올바르지 않습니다.",
    "warning.config_missing": "설정 파일이 없어 기본 설정을 사용합니다.",
    "warning.invalid_capability_token": "쓰기 권한 토큰이 현재 포터블 경로와 일치하지 않습니다.",
    "warning.invalid_config": "설정 형식이 올바르지 않습니다.",
    "warning.invalid_default_profile": "기본 프로필은 .omrtemplate 파일명이어야 합니다.",
    "warning.invalid_json_value": "저장할 JSON 값이 올바르지 않습니다.",
    "warning.invalid_managed_path": "안전하지 않은 관리 경로 구성 요소입니다.",
    "warning.invalid_write_payload": "저장할 데이터는 바이트여야 합니다.",
    "warning.logging_setup_failed": "로그 파일을 열지 못해 표준 오류 로그만 사용합니다.",
    "warning.managed_path_escape": "관리 경로가 포터블 루트를 벗어났습니다.",
    "warning.managed_path_invalid": "관리 폴더 또는 설정 파일 경로가 올바르지 않습니다.",
    "warning.portable_root_invalid": "포터블 실행 경로가 폴더가 아닙니다.",
    "warning.portable_root_unavailable": "포터블 실행 폴더를 확인할 수 없습니다.",
    "warning.root_write_denied": "실행 폴더에 쓸 권한이 없습니다.",
    "warning.xlsx_source_changed": "엑셀 원본 파일이 검증 후 변경되었거나 사용할 수 없습니다.",
    "warning.xlsx_validation_token_closed": "엑셀 파일 검증 연결이 이미 닫혔습니다.",
    "status.ready": "준비됨",
    "status.processing": "처리 중",
    "status.completed": "완료",
    "status.failed": "실패",
    "status.read_only": "읽기 전용",
    "status.created": "생성됨",
    "status.recognized": "인식 완료",
    "status.graded": "채점 완료",
    "status.finalized": "확정됨",
    "status.normal": "정상",
    "status.blank": "미표기",
    "status.multiple": "복수 표기",
    "status.uncertain": "판독 불확실",
    "status.all": "전체 표기",
    "status.unasked": "출제하지 않음",
    "status.invalid": "올바르지 않음",
    "status.missing": "없음",
    "status.duplicate": "중복",
}


def get_message(key: str) -> str:
    """Return Korean text for a stable key, raising for an unknown key."""
    if not isinstance(key, str):
        raise TypeError("message key must be a string")
    return MESSAGE_CATALOG[key]


def missing_message_keys(keys: Iterable[str]) -> tuple[str, ...]:
    """Return requested stable keys that have no Korean catalog entry."""
    return tuple(sorted({key for key in keys if key not in MESSAGE_CATALOG}))


def validate_message_catalog(keys: Iterable[str]) -> None:
    """Raise a clear contract error when a caller's stable keys lack Korean text."""
    missing = missing_message_keys(keys)
    if missing:
        raise ValueError(f"Korean messages are missing for: {', '.join(missing)}")


__all__ = [
    "MESSAGE_CATALOG",
    "get_message",
    "missing_message_keys",
    "validate_message_catalog",
]
