"""Portable, managed-path resolution without user-directory fallbacks."""

from __future__ import annotations

import os
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result

_CONFIG_NAME = "config.json"
_PROFILES_NAME = "Profiles"
_DATA_NAME = "OMR_Grader"
_LOGS_NAME = "logs"
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_FORBIDDEN_WINDOWS_COMPONENT_CHARACTERS = frozenset('<>:"/\\|?*')
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def _error(code: str, message: str, *, field_path: str | None = None) -> ErrorInfo:
    return ErrorInfo(
        code, f"error.{code.lower()}", field_path=field_path, context={"reason": message}
    )


def _is_reparse_point(path: Path) -> bool:
    """Detect links and Windows junction/reparse points without following them."""
    metadata = os.lstat(path)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def resolve_portable_root(
    *,
    frozen: bool | None = None,
    executable: Path | None = None,
    source_entry: Path | None = None,
) -> Path:
    """Resolve the portable root from an injectable executable or ``main.py`` path."""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        candidate = executable if executable is not None else Path(sys.executable)
    else:
        candidate = source_entry if source_entry is not None else Path(sys.argv[0])
    return candidate.resolve().parent


def validate_component(value: str, *, field_path: str = "path") -> Result[str]:
    """Accept one portable Windows filename component only."""
    if not isinstance(value, str):
        return Err(
            (
                _error(
                    "INVALID_MANAGED_PATH",
                    "안전하지 않은 관리 경로 구성 요소입니다.",
                    field_path=field_path,
                ),
            )
        )
    normalized = unicodedata.normalize("NFC", value)
    windows_path = PureWindowsPath(normalized)
    reserved_stem = normalized.split(".", 1)[0].rstrip(" .").upper()
    if (
        not normalized
        or normalized in {".", ".."}
        or any(ord(character) <= 0x1F for character in normalized)
        or any(character in _FORBIDDEN_WINDOWS_COMPONENT_CHARACTERS for character in normalized)
        or windows_path.is_absolute()
        or windows_path.drive
        or normalized[-1:] in {".", " "}
        or reserved_stem in _RESERVED_WINDOWS_NAMES
    ):
        return Err(
            (
                _error(
                    "INVALID_MANAGED_PATH",
                    "안전하지 않은 관리 경로 구성 요소입니다.",
                    field_path=field_path,
                ),
            )
        )
    return Ok(normalized)


def validate_profile_filename(value: str) -> Result[str]:
    component = validate_component(value, field_path="default_profile")
    if isinstance(component, Err):
        return component
    if (
        not component.value.lower().endswith(".omrtemplate")
        or component.value.lower() == ".omrtemplate"
    ):
        return Err(
            (
                _error(
                    "INVALID_DEFAULT_PROFILE",
                    "기본 프로필은 .omrtemplate 파일명이어야 합니다.",
                    field_path="default_profile",
                ),
            )
        )
    return component


@dataclass(frozen=True, slots=True)
class ManagedPaths:
    """The only persistent paths owned by the portable application."""

    root: Path

    @classmethod
    def from_root(cls, root: Path) -> ManagedPaths:
        return cls(root.resolve())

    @property
    def config_path(self) -> Path:
        return self.root / _CONFIG_NAME

    @property
    def profiles_dir(self) -> Path:
        return self.root / _PROFILES_NAME

    @property
    def data_dir(self) -> Path:
        return self.root / _DATA_NAME

    @property
    def logs_dir(self) -> Path:
        return self.root / _LOGS_NAME

    def config_target(self) -> Result[Path]:
        return self._contained(self.config_path)

    def profiles_target(self) -> Result[Path]:
        return self._contained(self.profiles_dir)

    def data_target(self) -> Result[Path]:
        return self._contained(self.data_dir)

    def logs_target(self) -> Result[Path]:
        return self._contained(self.logs_dir)

    def profile_path(self, filename: str) -> Result[Path]:
        validated = validate_profile_filename(filename)
        if isinstance(validated, Err):
            return validated
        return self._contained(self.profiles_dir / validated.value)

    def data_path(self, *components: str) -> Result[Path]:
        candidate = self.data_dir
        for index, component in enumerate(components):
            validated = validate_component(component, field_path=f"data_path[{index}]")
            if isinstance(validated, Err):
                return validated
            candidate /= validated.value
        return self._contained(candidate)

    def log_path(self, filename: str) -> Result[Path]:
        validated = validate_component(filename, field_path="log_path")
        if isinstance(validated, Err):
            return validated
        return self._contained(self.logs_dir / validated.value)

    def _contained(self, candidate: Path) -> Result[Path]:
        """Resolve existing ancestors physically and reject every managed reparse point."""
        try:
            root = self.root.resolve(strict=True)
            relative = candidate.relative_to(self.root)
            current = root
            for component in relative.parts:
                current /= component
                try:
                    if _is_reparse_point(current):
                        return Err(
                            (
                                _error(
                                    "MANAGED_PATH_INVALID",
                                    "관리 경로에 링크 또는 재분석 지점이 있습니다.",
                                ),
                            )
                        )
                except FileNotFoundError:
                    break
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError):
            return Err((_error("MANAGED_PATH_ESCAPE", "관리 경로가 포터블 루트를 벗어났습니다."),))
        return Ok(resolved)

    def existing_managed_entries(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path in (
                self.config_path,
                self.profiles_dir,
                self.data_dir,
                self.logs_dir,
            )
            if path.exists()
        )


def is_path_writable(path: Path) -> bool:
    """Inspect access only; this deliberately never creates a probe file."""
    try:
        return path.is_dir() and os.access(path, os.W_OK | os.X_OK)
    except OSError:
        return False
