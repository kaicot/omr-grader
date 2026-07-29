"""Write-free portable-root capability discovery and explicit bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.infrastructure.paths import ManagedPaths, is_path_writable


def _error(code: str, message: str) -> ErrorInfo:
    return ErrorInfo(code, f"error.{code.lower()}", context={"reason": message})


@dataclass(frozen=True, slots=True)
class CapabilityToken:
    """Opaque authorization produced by capability discovery or explicit test injection."""

    _root: Path

    @classmethod
    def _issue(cls, root: Path) -> CapabilityToken:
        return cls(root)

    @classmethod
    def for_testing(cls, root: Path) -> CapabilityToken:
        """Create an injectable token for isolated tests only."""
        return cls(root.resolve())


@dataclass(frozen=True, slots=True)
class RootCapability:
    paths: ManagedPaths
    write_enabled: bool
    token: CapabilityToken | None
    read_only_reason: str | None


def probe_root_capability(paths: ManagedPaths) -> Result[RootCapability]:
    """Resolve and inspect the root without creating files or directories."""
    try:
        root = paths.root.resolve(strict=True)
    except (OSError, FileNotFoundError):
        return Err((_error("PORTABLE_ROOT_UNAVAILABLE", "포터블 실행 폴더를 확인할 수 없습니다."),))
    if not root.is_dir():
        return Err((_error("PORTABLE_ROOT_INVALID", "포터블 실행 경로가 폴더가 아닙니다."),))

    resolved_paths = ManagedPaths.from_root(root)
    profiles = resolved_paths.profiles_target()
    data = resolved_paths.data_target()
    config = resolved_paths.config_target()
    if isinstance(profiles, Err):
        return profiles
    if isinstance(data, Err):
        return data
    if isinstance(config, Err):
        return config
    if profiles.value.exists() and not profiles.value.is_dir():
        return Err((_error("MANAGED_PATH_INVALID", "관리 폴더 경로가 올바르지 않습니다."),))
    if data.value.exists() and not data.value.is_dir():
        return Err((_error("MANAGED_PATH_INVALID", "관리 폴더 경로가 올바르지 않습니다."),))
    if config.value.exists() and not config.value.is_file():
        return Err((_error("MANAGED_PATH_INVALID", "설정 파일 경로가 올바르지 않습니다."),))

    if is_path_writable(root):
        return Ok(RootCapability(resolved_paths, True, CapabilityToken._issue(root), None))
    reason = "실행 폴더에 쓸 수 없어 읽기 전용으로 실행합니다. 폴더 권한을 확인하세요."
    warning = ErrorInfo(
        "ROOT_WRITE_DENIED",
        "warning.root_write_denied",
        context={"reason": reason},
    )
    return Ok(RootCapability(resolved_paths, False, None, reason), (warning,))


def bootstrap_managed_paths(paths: ManagedPaths, token: CapabilityToken) -> Result[ManagedPaths]:
    """Create missing managed directories only after a successful capability probe."""
    if token._root != paths.root.resolve():
        return Err(
            (
                _error(
                    "INVALID_CAPABILITY_TOKEN",
                    "쓰기 권한 토큰이 현재 포터블 경로와 일치하지 않습니다.",
                ),
            )
        )
    profiles = paths.profiles_target()
    data = paths.data_target()
    if isinstance(profiles, Err):
        return profiles
    if isinstance(data, Err):
        return data
    try:
        profiles.value.mkdir(parents=False, exist_ok=True)
        data.value.mkdir(parents=False, exist_ok=True)
    except OSError as exc:
        return Err(
            (
                ErrorInfo(
                    "ROOT_WRITE_DENIED",
                    "error.root_write_denied",
                    context={"reason": "관리 폴더를 만들 수 없습니다."},
                    cause_type=type(exc).__name__,
                ),
            )
        )
    profiles = paths.profiles_target()
    data = paths.data_target()
    if isinstance(profiles, Err):
        return profiles
    if isinstance(data, Err):
        return data
    if not profiles.value.is_dir() or not data.value.is_dir():
        return Err((_error("MANAGED_PATH_INVALID", "관리 폴더 경로가 올바르지 않습니다."),))
    return Ok(paths)
