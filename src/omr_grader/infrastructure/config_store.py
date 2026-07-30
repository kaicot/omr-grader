"""Strict portable configuration persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.infrastructure.atomic_io import atomic_write_json
from omr_grader.infrastructure.capabilities import CapabilityToken
from omr_grader.infrastructure.paths import ManagedPaths, validate_profile_filename
from omr_grader.infrastructure.session_lease import FileGateBackend, GateHandle


@dataclass(frozen=True, slots=True)
class AppConfig:
    default_profile: str
    default_sensitivity: int
    use_multiprocessing: bool

    def to_dict(self) -> dict[str, str | int | bool]:
        return {
            "default_profile": self.default_profile,
            "default_sensitivity": self.default_sensitivity,
            "use_multiprocessing": self.use_multiprocessing,
        }


_CONFIG_GATES = FileGateBackend()


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """A validated portable configuration and its canonical content revision."""

    config: AppConfig
    revision: int


def _canonical_config_bytes(config: AppConfig) -> bytes:
    return (
        json.dumps(
            config.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def config_revision(config: AppConfig) -> int:
    """Return a stable, positive revision covering every persisted config value."""
    digest = hashlib.sha256(_canonical_config_bytes(config)).digest()
    return int.from_bytes(digest[:8], "big") or 1


def _snapshot(config: AppConfig) -> ConfigSnapshot:
    return ConfigSnapshot(config, config_revision(config))


def default_config() -> AppConfig:
    return AppConfig(default_profile="", default_sensitivity=5, use_multiprocessing=True)


def _issue(
    code: str,
    message: str,
    exc: BaseException | None = None,
    *,
    warning: bool = False,
) -> ErrorInfo:
    prefix = "warning" if warning else "error"
    return ErrorInfo(
        code,
        f"{prefix}.{code.lower()}",
        context={"reason": message},
        cause_type=type(exc).__name__ if exc is not None else None,
    )


def validate_config(config: AppConfig) -> Result[AppConfig]:
    if not isinstance(config, AppConfig):
        return Err((_issue("INVALID_CONFIG", "설정 형식이 올바르지 않습니다."),))
    normalized_profile = ""
    if config.default_profile:
        profile = validate_profile_filename(config.default_profile)
        if isinstance(profile, Err):
            return Err((_issue("INVALID_CONFIG", "기본 프로필 파일명이 올바르지 않습니다."),))
        normalized_profile = profile.value
    if (
        isinstance(config.default_sensitivity, bool)
        or not isinstance(config.default_sensitivity, int)
        or not 1 <= config.default_sensitivity <= 10
    ):
        return Err((_issue("INVALID_CONFIG", "기본 민감도는 1에서 10 사이의 정수여야 합니다."),))
    if not isinstance(config.use_multiprocessing, bool):
        return Err((_issue("INVALID_CONFIG", "다중 처리 설정은 참 또는 거짓이어야 합니다."),))
    return Ok(AppConfig(normalized_profile, config.default_sensitivity, config.use_multiprocessing))


def _parse_config(value: object) -> Result[AppConfig]:
    if not isinstance(value, dict):
        return Err((_issue("INVALID_CONFIG", "설정 JSON 객체가 올바르지 않습니다."),))
    try:
        config = AppConfig(
            default_profile=value["default_profile"],
            default_sensitivity=value["default_sensitivity"],
            use_multiprocessing=value["use_multiprocessing"],
        )
    except KeyError:
        return Err((_issue("INVALID_CONFIG", "필수 설정 항목이 없습니다."),))
    return validate_config(config)


def _has_token_for(paths: ManagedPaths, token: CapabilityToken | None) -> bool:
    return token is not None and token._root == paths.root.resolve()


def _config_gate(paths: ManagedPaths) -> Result[GateHandle]:
    """Serialize compare-and-replace across every process sharing this portable root."""
    path = paths.root / ".locks" / "config.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError as exc:
        return Err((_issue("CONFIG_LOCK_FAILED", "설정 잠금을 만들 수 없습니다.", exc),))
    gate = _CONFIG_GATES.acquire(path, exclusive=True, blocking=True)
    if gate is None:
        return Err((_issue("CONFIG_LOCK_FAILED", "설정 잠금을 획득할 수 없습니다."),))
    return Ok(gate)


def save_config(paths: ManagedPaths, config: AppConfig, token: CapabilityToken) -> Result[None]:
    """Validate and canonically replace config.json; unknown input keys cannot persist."""
    valid = validate_config(config)
    if isinstance(valid, Err):
        return valid
    if not _has_token_for(paths, token):
        return Err((_issue("ROOT_WRITE_DENIED", "설정을 저장할 쓰기 권한이 없습니다."),))
    target = paths.config_target()
    if isinstance(target, Err):
        return target
    return atomic_write_json(target.value, valid.value.to_dict())


def load_config(paths: ManagedPaths, token: CapabilityToken | None = None) -> Result[AppConfig]:
    """Read strict config, retaining safe defaults on missing or damaged input."""
    fallback = default_config()
    target = paths.config_target()
    if isinstance(target, Err):
        return target
    try:
        with target.value.open("r", encoding="utf-8") as stream:
            parsed = json.load(stream)
    except FileNotFoundError:
        warnings: list[ErrorInfo] = [
            _issue(
                "CONFIG_MISSING",
                "설정 파일이 없어 안전한 기본 설정을 사용합니다.",
                warning=True,
            )
        ]
        if token is not None and _has_token_for(paths, token):
            saved = save_config(paths, fallback, token)
            if isinstance(saved, Err):
                return saved
        return Ok(fallback, tuple(warnings))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return Ok(
            fallback,
            (
                _issue(
                    "CONFIG_INVALID",
                    "설정 파일이 손상되어 안전한 기본 설정을 사용합니다.",
                    warning=True,
                ),
            ),
        )
    except OSError as exc:
        return Ok(
            fallback,
            (
                _issue(
                    "CONFIG_IO_FAILED",
                    "설정 파일 I/O 오류로 안전한 기본 설정을 사용합니다.",
                    exc,
                    warning=True,
                ),
            ),
        )

    parsed_config = _parse_config(parsed)
    if isinstance(parsed_config, Err):
        return Ok(
            fallback,
            (
                _issue(
                    "CONFIG_INVALID",
                    "설정 값이 올바르지 않아 안전한 기본 설정을 사용합니다.",
                    warning=True,
                ),
            ),
        )
    return parsed_config


def load_config_snapshot(
    paths: ManagedPaths, token: CapabilityToken | None = None
) -> Result[ConfigSnapshot]:
    """Load safe settings with a revision of the exact canonical persisted keys."""
    loaded = load_config(paths, token)
    if isinstance(loaded, Err):
        return loaded
    return Ok(_snapshot(loaded.value), loaded.warnings)


def _current_config_for_compare(paths: ManagedPaths) -> Result[AppConfig]:
    """Read a current config without converting an I/O failure into a writable default."""
    target = paths.config_target()
    if isinstance(target, Err):
        return target
    try:
        with target.value.open("r", encoding="utf-8") as stream:
            parsed = json.load(stream)
    except FileNotFoundError:
        return Ok(default_config())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return Ok(default_config())
    except OSError as exc:
        return Err((_issue("CONFIG_READ_FAILED", "설정 파일을 읽을 수 없습니다.", exc),))
    parsed_config = _parse_config(parsed)
    return Ok(parsed_config.value if isinstance(parsed_config, Ok) else default_config())


def save_config_if_revision(
    paths: ManagedPaths,
    config: AppConfig,
    expected_revision: int,
    token: CapabilityToken,
) -> Result[ConfigSnapshot]:
    """Atomically replace config only when all canonical current content still matches."""
    if type(expected_revision) is not int or expected_revision <= 0:
        return Err((_issue("INVALID_CONFIG_REVISION", "설정 버전이 올바르지 않습니다."),))
    valid = validate_config(config)
    if isinstance(valid, Err):
        return valid
    if not _has_token_for(paths, token):
        return Err((_issue("ROOT_WRITE_DENIED", "설정을 저장할 쓰기 권한이 없습니다."),))
    gate = _config_gate(paths)
    if isinstance(gate, Err):
        return gate
    try:
        current = _current_config_for_compare(paths)
        if isinstance(current, Err):
            return current
        if config_revision(current.value) != expected_revision:
            return Err(
                (_issue("CONFIG_REVISION_CONFLICT", "설정이 다른 작업에서 변경되었습니다."),)
            )
        target = paths.config_target()
        if isinstance(target, Err):
            return target
        written = atomic_write_json(target.value, valid.value.to_dict())
        if isinstance(written, Err):
            return written
    finally:
        gate.value.close()
    return Ok(_snapshot(valid.value))
