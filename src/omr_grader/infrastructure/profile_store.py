"""Validated import and discovery of portable `.omrtemplate` files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omr_grader.application.dto import CollisionPolicy, ProfileImportRequest, ProfileImportResult
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.profile import MAX_PROFILE_BYTES, Profile, parse_profile_bytes
from omr_grader.infrastructure.atomic_io import atomic_write_bytes
from omr_grader.infrastructure.capabilities import CapabilityToken
from omr_grader.infrastructure.paths import ManagedPaths, validate_profile_filename


def _error(code: str, reason: str, *, cause: BaseException | None = None) -> Err:
    return Err(
        (
            ErrorInfo(
                code,
                f"error.{code.lower()}",
                context={"reason": reason},
                cause_type=type(cause).__name__ if cause else None,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class ProfileCatalogItem:
    """One managed profile candidate and its immutable validation diagnostics."""

    filename: str
    profile: Profile | None
    diagnostics: tuple[ErrorInfo, ...]

    @property
    def is_valid(self) -> bool:
        return self.profile is not None and not self.diagnostics


def _read_bounded(path: Path) -> Result[bytes]:
    try:
        if not path.is_file():
            return _error("PROFILE_SOURCE_INVALID", "프로필 원본 파일을 찾을 수 없습니다.")
        with path.open("rb") as stream:
            payload = stream.read(MAX_PROFILE_BYTES + 1)
    except OSError as exc:
        return _error("PROFILE_READ_FAILED", "프로필 파일을 읽을 수 없습니다.", cause=exc)
    if len(payload) > MAX_PROFILE_BYTES:
        return _error("INVALID_PROFILE", "프로필 파일이 허용 크기를 초과했습니다.")
    return Ok(payload)


@dataclass(frozen=True, slots=True)
class ProfileStore:
    """Filesystem adapter for imported profiles; all fallible operations return Result."""

    paths: ManagedPaths
    capability_token: CapabilityToken | None = None

    def _authorized(self) -> bool:
        return (
            self.capability_token is not None
            and self.capability_token._root == self.paths.root.resolve()
        )

    def load_path(self, path: Path) -> Result[Profile]:
        payload = _read_bounded(path)
        if isinstance(payload, Err):
            return payload
        return parse_profile_bytes(payload.value)

    def load(self, filename: str) -> Result[Profile]:
        target = self.paths.profile_path(filename)
        if isinstance(target, Err):
            return target
        return self.load_path(target.value)

    def catalog(self) -> Result[tuple[ProfileCatalogItem, ...]]:
        """Return every managed template candidate with its validation diagnostics."""
        target = self.paths.profiles_target()
        if isinstance(target, Err):
            return target
        try:
            if not target.value.exists():
                return Ok(())
            if not target.value.is_dir():
                return _error("MANAGED_PATH_INVALID", "프로필 폴더 경로가 올바르지 않습니다.")
            items: list[ProfileCatalogItem] = []
            for child in target.value.iterdir():
                if not child.is_file() or child.suffix.lower() != ".omrtemplate":
                    continue
                filename = validate_profile_filename(child.name)
                if isinstance(filename, Err) or filename.value != child.name:
                    diagnostics = (
                        filename.errors
                        if isinstance(filename, Err)
                        else (
                            ErrorInfo(
                                "INVALID_PROFILE_FILENAME",
                                "error.invalid_profile_filename",
                                context={"reason": "프로필 파일명이 정규화되지 않았습니다."},
                            ),
                        )
                    )
                    items.append(ProfileCatalogItem(child.name, None, diagnostics))
                    continue
                parsed = self.load_path(child)
                if isinstance(parsed, Ok):
                    items.append(ProfileCatalogItem(child.name, parsed.value, parsed.warnings))
                else:
                    items.append(ProfileCatalogItem(child.name, None, parsed.errors))
            return Ok(
                tuple(sorted(items, key=lambda item: (item.filename.casefold(), item.filename)))
            )
        except OSError as exc:
            return _error("PROFILE_DISCOVERY_FAILED", "프로필 폴더를 읽을 수 없습니다.", cause=exc)

    def discover(self) -> Result[tuple[str, ...]]:
        """Screen 1 discovery intentionally exposes valid profiles only."""
        catalog = self.catalog()
        if isinstance(catalog, Err):
            return catalog
        return Ok(tuple(item.filename for item in catalog.value if item.is_valid))

    def default_profile(self, filename: str) -> Result[Profile | None]:
        if filename == "":
            return Ok(None)
        validated = validate_profile_filename(filename)
        if isinstance(validated, Err):
            return validated
        loaded = self.load(validated.value)
        if isinstance(loaded, Err):
            warning = ErrorInfo(
                "DEFAULT_PROFILE_INVALID",
                "warning.default_profile_invalid",
                context={"reason": "기본 프로필을 사용할 수 없습니다."},
            )
            return Ok(None, (warning,))
        return Ok(loaded.value, loaded.warnings)

    def import_profile(self, request: ProfileImportRequest) -> Result[ProfileImportResult]:
        """Validate the source completely before atomically changing its destination."""
        if not self._authorized():
            return _error("ROOT_WRITE_DENIED", "프로필을 저장할 쓰기 권한이 없습니다.")
        source = Path(request.source_path)
        payload = _read_bounded(source)
        if isinstance(payload, Err):
            return payload
        parsed = parse_profile_bytes(payload.value)
        if isinstance(parsed, Err):
            return parsed
        source_name = request.new_name if request.new_name is not None else source.name
        filename = validate_profile_filename(source_name)
        if isinstance(filename, Err):
            return filename
        if request.collision is CollisionPolicy.RENAME and request.new_name is None:
            return _error(
                "PROFILE_RENAME_REQUIRED", "다른 이름으로 저장하려면 새 파일명이 필요합니다."
            )
        target = self.paths.profile_path(filename.value)
        if isinstance(target, Err):
            return target
        try:
            if not target.value.parent.is_dir():
                return _error("MANAGED_PATH_INVALID", "프로필 폴더가 준비되지 않았습니다.")
            exists = target.value.exists()
        except OSError as exc:
            return _error(
                "PROFILE_IMPORT_FAILED", "대상 프로필 경로를 확인할 수 없습니다.", cause=exc
            )
        if exists and request.collision is CollisionPolicy.ERROR:
            return _error("PROFILE_COLLISION", "같은 이름의 프로필이 이미 있습니다.")
        written = atomic_write_bytes(target.value, payload.value)
        if isinstance(written, Err):
            return written
        return Ok(ProfileImportResult(filename.value, parsed.value.sha256), parsed.warnings)


__all__ = ["ProfileCatalogItem", "ProfileStore"]
