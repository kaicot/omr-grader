"""Portable settings application service with profile-aware selection validation."""

from __future__ import annotations

from dataclasses import dataclass

from omr_grader.application.dto import Settings, SettingsSaveCommand, SettingsSaveResult
from omr_grader.application.profile_use_case import ProfileApplicationService
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.infrastructure.capabilities import CapabilityToken
from omr_grader.infrastructure.config_store import (
    AppConfig,
    load_config_snapshot,
    save_config_if_revision,
)
from omr_grader.infrastructure.paths import ManagedPaths


@dataclass(frozen=True, slots=True)
class SettingsState:
    """Settings offered to the UI together with the required CAS revision."""

    settings: Settings
    revision: int


def _warning(code: str, reason: str) -> ErrorInfo:
    return ErrorInfo(code, f"warning.{code.lower()}", context={"reason": reason})
def _warning_copy(issue: ErrorInfo) -> ErrorInfo:
    return ErrorInfo(
        issue.code,
        issue.message_key
        if issue.message_key.startswith("warning.")
        else f"warning.{issue.code.lower()}",
        issue.field_path,
        dict(issue.context),
        issue.retryable,
        issue.cause_type,
    )




def _error(code: str, reason: str) -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", context={"reason": reason}),))


@dataclass(frozen=True, slots=True)
class SettingsApplicationService:
    """Shared portable settings boundary; it never substitutes another profile."""

    paths: ManagedPaths
    capability_token: CapabilityToken
    profiles: ProfileApplicationService

    def load_settings(self) -> Result[SettingsState]:
        loaded = load_config_snapshot(self.paths, self.capability_token)
        if isinstance(loaded, Err):
            return loaded
        config = loaded.value.config
        warnings = list(loaded.warnings)
        selected = config.default_profile
        if selected:
            catalog = self.profiles.profile_catalog()
            if isinstance(catalog, Err):
                warnings.extend(_warning_copy(issue) for issue in catalog.errors)
            elif not any(item.filename == selected and item.is_valid for item in catalog.value):
                warnings.append(
                    _warning(
                        "DEFAULT_PROFILE_UNAVAILABLE",
                        "설정된 기본 프로필을 사용할 수 없습니다.",
                    )
                )
        return Ok(
            SettingsState(
                Settings(selected, config.default_sensitivity, config.use_multiprocessing),
                loaded.value.revision,
            ),
            tuple(warnings),
        )

    def save_settings(self, command: SettingsSaveCommand) -> Result[SettingsSaveResult]:
        selected = command.settings.default_profile
        if selected:
            catalog = self.profiles.profile_catalog()
            if isinstance(catalog, Err):
                return catalog
            if not any(item.filename == selected and item.is_valid for item in catalog.value):
                return _error(
                    "DEFAULT_PROFILE_UNAVAILABLE", "선택한 기본 프로필을 사용할 수 없습니다."
                )
        saved = save_config_if_revision(
            self.paths,
            AppConfig(
                command.settings.default_profile,
                command.settings.default_sensitivity,
                command.settings.use_multiprocessing,
            ),
            command.expected_revision,
            self.capability_token,
        )
        if isinstance(saved, Err):
            return saved
        return Ok(SettingsSaveResult(True, saved.value.revision, command.operation_id))


__all__ = ["SettingsApplicationService", "SettingsState"]
