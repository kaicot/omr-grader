from __future__ import annotations

import pytest

from omr_grader.resources.messages import (
    MESSAGE_CATALOG,
    get_message,
    validate_message_catalog,
)

DECLARED_DIAGNOSTIC_KEYS = (
    "error.atomic_write_failed",
    "error.backup_handle_closed",
    "error.backup_source_changed",
    "error.config_invalid",
    "error.config_missing",
    "error.invalid_capability_token",
    "error.invalid_config",
    "error.invalid_default_profile",
    "error.invalid_json_value",
    "error.invalid_managed_path",
    "error.invalid_write_payload",
    "error.logging_setup_failed",
    "error.managed_path_escape",
    "error.managed_path_invalid",
    "error.portable_root_invalid",
    "error.portable_root_unavailable",
    "error.root_write_denied",
    "error.validation_token_close_failed",
    "error.xlsx_source_changed",
    "error.xlsx_validation_token_closed",
    "warning.atomic_write_failed",
    "warning.backup_handle_closed",
    "warning.backup_source_changed",
    "warning.config_invalid",
    "warning.config_missing",
    "warning.invalid_capability_token",
    "warning.invalid_config",
    "warning.invalid_default_profile",
    "warning.invalid_json_value",
    "warning.invalid_managed_path",
    "warning.invalid_write_payload",
    "warning.logging_setup_failed",
    "warning.managed_path_escape",
    "warning.managed_path_invalid",
    "warning.portable_root_invalid",
    "warning.portable_root_unavailable",
    "warning.root_write_denied",
    "warning.xlsx_source_changed",
    "warning.xlsx_validation_token_closed",
)
DECLARED_STATUS_KEYS = (
    "status.ready",
    "status.processing",
    "status.completed",
    "status.failed",
    "status.read_only",
    "status.created",
    "status.recognized",
    "status.graded",
    "status.finalized",
    "status.normal",
    "status.blank",
    "status.multiple",
    "status.uncertain",
    "status.all",
    "status.unasked",
    "status.invalid",
    "status.missing",
    "status.duplicate",
)


def test_catalog_provides_non_empty_korean_text_for_every_stable_key() -> None:
    validate_message_catalog((*DECLARED_DIAGNOSTIC_KEYS, *DECLARED_STATUS_KEYS))

    for key, message in MESSAGE_CATALOG.items():
        assert message.strip(), key
        if key not in {"app.title", "app.organization"}:
            assert any("가" <= character <= "힣" for character in message), key


def test_title_uses_official_product_name() -> None:
    assert get_message("app.title") == "OMR Grader"
    assert get_message("app.organization") == "OMR Grader"
    assert get_message("bootstrap.shell_body") == "OMR Grader를 시작했습니다."


def test_unknown_message_key_raises_instead_of_falling_back() -> None:
    with pytest.raises(KeyError):
        get_message("error.not_declared")
