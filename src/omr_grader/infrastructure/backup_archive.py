"""Exact current-only ``omrbak-v1`` archive boundary."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import stat
import unicodedata
import uuid
import zipfile
import zlib
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from omr_grader.application.ports import CommittedSnapshotLease
from omr_grader.application.validation_token import (
    BorrowedInspectionHandle,
    SourceFileIdentity,
    ValidatedBackup,
)
from omr_grader.domain.enums import ArchiveLineageMode
from omr_grader.domain.errors import Err, ErrorInfo, Ok, Result
from omr_grader.domain.models import (
    ArchiveEntry,
    ArchiveManifest,
    CurrentPointer,
    IdentityRecord,
    OmittedParent,
    SessionManifest,
    SessionRecord,
)

_PREFIX = "omrbak-v1"
_ARCHIVE_MANIFEST = f"{_PREFIX}/archive_manifest.json"
_IDENTITY = "IDENTITY.json"
_CURRENT = "CURRENT.json"
_CHUNK_SIZE = 1024 * 1024
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _error(code: str, path: str = "") -> Err:
    return Err((ErrorInfo(code, f"error.{code.lower()}", field_path=path or None),))


def _portable_path(path: str) -> bool:
    if not path or unicodedata.normalize("NFC", path) != path or "\\" in path:
        return False
    if path.startswith(("/", "\\")) or (len(path) >= 2 and path[1] == ":"):
        return False
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    for part in path.split("/"):
        stem = part.split(".", 1)[0].upper().translate(str.maketrans("¹²³", "123"))
        if (
            not part
            or part in {".", ".."}
            or part[-1:] in {".", " "}
            or stem in reserved
            or any(ord(char) < 32 or ord(char) == 127 or char in '<>:"|?*' for char in part)
        ):
            return False
    return True


def _json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _omitted_parent(manifest: SessionManifest) -> OmittedParent | None:
    if manifest.revision == 1:
        return None
    revision = manifest.parent_revision
    generation_id = manifest.parent_generation_id
    digest = manifest.parent_manifest_sha256
    if revision is None or generation_id is None or digest is None:
        raise ValueError("archive source lineage is incomplete")
    return OmittedParent(revision, generation_id, digest)


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    path: str
    size: int
    sha256: str
    crc32: int


@dataclass(frozen=True, slots=True)
class StagingOwnership:
    """Stable identities captured when the restore tree is created."""

    tree_device: int
    tree_inode: int
    parent_device: int
    parent_inode: int


@dataclass(frozen=True, slots=True)
class ExtractedBackup:
    staging_root: str
    manifest: SessionManifest
    identity: IdentityRecord
    current: CurrentPointer
    archive_manifest: ArchiveManifest
    archive_sha256: str
    source_identity: SourceFileIdentity | None
    staging_ownership: StagingOwnership | None = None


class BackupArchive:
    """Archive boundary.  The archive is a closed, current-generation-only tree."""

    MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
    MAX_ENTRIES = 20_000
    MAX_METADATA_BYTES = 4 * 1024 * 1024
    MAX_ENTRY_BYTES = 512 * 1024 * 1024
    MAX_TOTAL_BYTES = 20 * 1024 * 1024 * 1024
    MAX_COMPRESSION_RATIO = 200

    def export(
        self, lease: CommittedSnapshotLease, destination: str, *, replace: bool
    ) -> Result[str]:
        target = Path(destination)
        if target.suffix.lower() != ".omrbak":
            return _error("BACKUP_DESTINATION_INVALID", destination)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            generation = Path(lease.root_path)
            session = generation.parent.parent
            identity_bytes = (session / _IDENTITY).read_bytes()
            current_bytes = (session / _CURRENT).read_bytes()
            manifest_bytes = (generation / "manifest.json").read_bytes()
            record_bytes = (generation / "session.json").read_bytes()
            identity = IdentityRecord.from_dict(_mapping(json.loads(identity_bytes)))
            current = CurrentPointer.from_dict(_mapping(json.loads(current_bytes)))
            manifest = SessionManifest.from_dict(_mapping(json.loads(manifest_bytes)))
            SessionRecord.from_dict(_mapping(json.loads(record_bytes)))
            name = f"g{manifest.revision:08d}_{manifest.generation_id}"
            if (
                lease.manifest != manifest
                or current.session_id != identity.session_id
                or current.revision != manifest.revision
                or current.generation_id != manifest.generation_id
                or current.generation_relpath != f"generations/{name}"
                or current.manifest_sha256 != _sha_bytes(manifest_bytes)
            ):
                return _error("BACKUP_SNAPSHOT_INVALID", destination)
            prefix = f"{_PREFIX}/generations/{name}"
            controls = (
                (f"{_PREFIX}/{_IDENTITY}", identity_bytes),
                (f"{_PREFIX}/{_CURRENT}", current_bytes),
                (f"{prefix}/manifest.json", manifest_bytes),
                (f"{prefix}/session.json", record_bytes),
            )
            entries: list[ArchiveEntry] = [
                ArchiveEntry(path, "application/json", len(data), _sha_bytes(data))
                for path, data in controls
            ]
            payload_paths = tuple(f"{prefix}/{item.path}" for item in manifest.files)
            for path, item in zip(payload_paths, manifest.files, strict=True):
                entries.append(ArchiveEntry(path, item.media_type, item.size, item.sha256))
            archive_manifest = ArchiveManifest(
                1,
                1,
                "omr-grader",
                manifest.created_at,
                ArchiveLineageMode.CURRENT_ONLY,
                manifest.session_id,
                manifest.revision,
                manifest.generation_id,
                _sha_bytes(manifest_bytes),
                _omitted_parent(manifest),
                True,
                tuple(sorted(entries, key=lambda entry: entry.path.encode("utf-8"))),
            )
            with zipfile.ZipFile(
                temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True
            ) as archive:
                self._write_member(
                    archive, _ARCHIVE_MANIFEST, BytesIO(_json(archive_manifest.to_dict()))
                )
                for path, data in controls:
                    self._write_member(archive, path, BytesIO(data))
                for path, item in zip(payload_paths, manifest.files, strict=True):
                    opened = lease.open_allowlisted(item.path)
                    if isinstance(opened, Err):
                        return opened
                    with opened.value as source:
                        member = self._write_member(archive, path, source)
                    if member.size != item.size or member.sha256 != item.sha256:
                        return _error("BACKUP_SNAPSHOT_INVALID", item.path)
            with temporary.open("rb") as handle:
                inspected = self._inspect(handle)
                if isinstance(inspected, Err) or inspected.value.manifest != manifest:
                    return _error("BACKUP_EXPORT_FAILED", destination)
                handle.seek(0)
                archive_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
                if os.fstat(handle.fileno()).st_size > self.MAX_ARCHIVE_BYTES:
                    return _error("BACKUP_EXPORT_FAILED", destination)
            if replace:
                os.replace(temporary, target)
            else:
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    return _error("BACKUP_DESTINATION_EXISTS", destination)
            return Ok(archive_sha256)
        except (
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
        ):
            return _error("BACKUP_EXPORT_FAILED", destination)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def preflight(self, token: ValidatedBackup) -> Result[SessionManifest]:
        try:
            inspected = token.inspect_revalidated(self._inspect)
            if isinstance(inspected, Err):
                return inspected
            return Ok(inspected.value.manifest)
        except (
            KeyError,
            OSError,
            ValueError,
            TypeError,
            UnicodeError,
            zlib.error,
            json.JSONDecodeError,
            zipfile.BadZipFile,
        ):
            return _error("BACKUP_ARCHIVE_INVALID", token.canonical_path)

    def extract(
        self,
        handle: BinaryIO,
        target_root: str,
        *,
        archive_sha256: str = "",
        source_identity: SourceFileIdentity | None = None,
    ) -> Result[ExtractedBackup]:
        staging: Path | None = None
        ownership: StagingOwnership | None = None
        try:
            handle.seek(0)
            consumed_identity = _source_identity(handle)
            consumed_sha256 = _sha_handle(handle)
            if archive_sha256 and archive_sha256 != consumed_sha256:
                return _error("BACKUP_SOURCE_CHANGED")
            if source_identity is not None and source_identity != consumed_identity:
                return _error("BACKUP_SOURCE_CHANGED")
            inspected = self._inspect(handle)
            if isinstance(inspected, Err):
                return inspected
            layout = inspected.value
            target = self._safe_target(Path(target_root))
            if os.name == "nt":
                with _WindowsRestoreAdapter.create(target) as restore:
                    staging, ownership = restore.staging, restore.ownership
                    with zipfile.ZipFile(handle) as archive:
                        infos = self._safe_infos(archive.infolist())
                        for entry in layout.archive_manifest.entries:
                            restore.stream_entry(
                                archive,
                                infos[entry.path],
                                tuple(entry.path.split("/")[1:]),
                                entry,
                            )
                        restore.verify()
            else:
                with self._owned_staging(target) as (
                    staging,
                    ownership,
                    target_fd,
                    staging_fd,
                ):
                    with zipfile.ZipFile(handle) as archive:
                        infos = self._safe_infos(archive.infolist())
                        created_dirs: set[tuple[str, ...]] = set()
                        for entry in layout.archive_manifest.entries:
                            self._stream_entry_at(
                                archive,
                                infos[entry.path],
                                staging_fd,
                                tuple(entry.path.split("/")[1:]),
                                entry,
                                created_dirs,
                            )
                    self._verify_owned_fds(
                        target, staging, ownership, target_fd, staging_fd
                    )
            final_identity = _source_identity(handle)
            final_sha256 = _sha_handle(handle)
            if final_identity != consumed_identity or final_sha256 != consumed_sha256:
                return self._discard_with_primary(
                    _error("BACKUP_SOURCE_CHANGED"), staging, ownership
                )
            return Ok(
                ExtractedBackup(
                    str(staging),
                    layout.manifest,
                    layout.identity,
                    layout.current,
                    layout.archive_manifest,
                    consumed_sha256,
                    consumed_identity,
                    ownership,
                )
            )
        except _RestorePlatformUnavailable:
            return _error("BACKUP_RESTORE_PLATFORM_UNAVAILABLE", target_root)
        except (
            KeyError,
            OSError,
            ValueError,
            TypeError,
            UnicodeError,
            zlib.error,
            json.JSONDecodeError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
        ):
            return self._discard_with_primary(
                _error("BACKUP_RESTORE_FAILED", target_root), staging, ownership
            )

    def discard(
        self, staging_root: str, ownership: StagingOwnership | None = None
    ) -> Result[None]:
        """Quarantine an owned tree before deleting it; never follow a replacement."""
        if ownership is None:
            return _error("BACKUP_RESTORE_PLATFORM_UNAVAILABLE", staging_root)
        staging = Path(staging_root)
        try:
            self._assert_owned(staging, ownership)
            quarantine = staging.with_name(f".backup-quarantine-{uuid.uuid4().hex}")
            _move_directory_no_replace(staging, quarantine)
            quarantined = StagingOwnership(
                ownership.tree_device,
                ownership.tree_inode,
                ownership.parent_device,
                ownership.parent_inode,
            )
            self._assert_owned(quarantine, quarantined)
            shutil.rmtree(quarantine)
        except FileNotFoundError:
            # A successful publisher atomically moves the owned staging tree; no cleanup remains.
            return Ok(None)
        except _RestorePlatformUnavailable:
            return _error("BACKUP_RESTORE_PLATFORM_UNAVAILABLE", staging_root)
        except OSError:
            return _error("BACKUP_RESTORE_CLEANUP_REQUIRED", staging_root)
        except ValueError:
            return _error("BACKUP_RESTORE_OWNERSHIP_LOST", staging_root)
        return Ok(None)

    @staticmethod
    def _require_safe_directory_fds() -> None:
        if (
            not hasattr(os, "O_DIRECTORY")
            or not hasattr(os, "O_NOFOLLOW")
            or os.open not in os.supports_dir_fd
            or os.mkdir not in os.supports_dir_fd
        ):
            raise _RestorePlatformUnavailable()


    @contextmanager
    def _owned_staging(
        self, target: Path
    ) -> Iterator[tuple[Path, StagingOwnership, int, int]]:
        self._require_safe_directory_fds()
        target_fd = os.open(target, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
        staging_fd = -1
        try:
            name = f".backup-staging-{uuid.uuid4().hex}"
            os.mkdir(name, dir_fd=target_fd)
            staging_fd = os.open(
                name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=target_fd
            )
            target_metadata = os.fstat(target_fd)
            staging_metadata = os.fstat(staging_fd)
            ownership = StagingOwnership(
                staging_metadata.st_dev,
                staging_metadata.st_ino,
                target_metadata.st_dev,
                target_metadata.st_ino,
            )
            yield target / name, ownership, target_fd, staging_fd
        finally:
            if staging_fd != -1:
                os.close(staging_fd)
            os.close(target_fd)

    def _verify_owned_fds(
        self,
        target: Path,
        staging: Path,
        ownership: StagingOwnership,
        target_fd: int,
        staging_fd: int,
    ) -> None:
        target_metadata = os.fstat(target_fd)
        staging_metadata = os.fstat(staging_fd)
        if (
            (staging_metadata.st_dev, staging_metadata.st_ino)
            != (ownership.tree_device, ownership.tree_inode)
            or (target_metadata.st_dev, target_metadata.st_ino)
            != (ownership.parent_device, ownership.parent_inode)
        ):
            raise ValueError("restore handle ownership changed")
        self._assert_owned(staging, ownership)
        if _path_identity(target) != (target_metadata.st_dev, target_metadata.st_ino):
            raise ValueError("restore target name binding changed")

    def _assert_owned(self, path: Path, ownership: StagingOwnership) -> None:
        if _path_identity(path) != (ownership.tree_device, ownership.tree_inode):
            raise ValueError("restore tree ownership changed")
        if _path_identity(path.parent) != (ownership.parent_device, ownership.parent_inode):
            raise ValueError("restore parent ownership changed")
        if _reparse_in_path(path, stop=path.parent):
            raise ValueError("restore tree is a reparse point")

    def _discard_with_primary(
        self,
        primary: Err,
        staging: Path | None,
        ownership: StagingOwnership | None,
    ) -> Err:
        if staging is None or ownership is None:
            return primary
        discarded = self.discard(str(staging), ownership)
        if isinstance(discarded, Err):
            return Err((*primary.errors, *discarded.errors))
        return primary

    def _stream_entry_at(
        self,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        root_fd: int,
        parts: tuple[str, ...],
        entry: ArchiveEntry,
        created_dirs: set[tuple[str, ...]],
    ) -> None:
        if not parts or any(not part for part in parts):
            raise ValueError("archive entry has no relative path")
        descriptor = os.dup(root_fd)
        try:
            for index, part in enumerate(parts[:-1], 1):
                prefix = parts[:index]
                if prefix not in created_dirs:
                    try:
                        os.mkdir(part, dir_fd=descriptor)
                    except FileExistsError as exc:
                        raise ValueError("restore child was pre-created") from exc
                    created_dirs.add(prefix)
                next_descriptor = os.open(
                    part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = next_descriptor
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
            output_fd = os.open(parts[-1], flags, 0o600, dir_fd=descriptor)
            try:
                with archive.open(info) as source, os.fdopen(output_fd, "wb") as output:
                    output_fd = -1
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := source.read(_CHUNK_SIZE):
                        size += len(chunk)
                        if size > self.MAX_ENTRY_BYTES:
                            raise ValueError("entry quota exceeded")
                        digest.update(chunk)
                        output.write(chunk)
                if size != entry.size or digest.hexdigest() != entry.sha256:
                    raise ValueError("archive payload integrity mismatch")
            finally:
                if output_fd != -1:
                    os.close(output_fd)
        finally:
            os.close(descriptor)
    def _inspect(self, handle: BinaryIO | BorrowedInspectionHandle) -> Result[_Layout]:
        try:
            return self._inspect_layout(handle)
        except (
            KeyError,
            OSError,
            ValueError,
            TypeError,
            UnicodeError,
            zlib.error,
            json.JSONDecodeError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
        ):
            return _error("BACKUP_ARCHIVE_INVALID")

    def _inspect_layout(self, handle: BinaryIO | BorrowedInspectionHandle) -> Result[_Layout]:
        handle.seek(0, os.SEEK_END)
        if handle.tell() > self.MAX_ARCHIVE_BYTES:
            raise ValueError("archive quota exceeded")
        handle.seek(0)
        with zipfile.ZipFile(handle) as archive:
            infos = self._safe_infos(archive.infolist())
            wrapper_info = infos.get(_ARCHIVE_MANIFEST)
            if wrapper_info is None:
                return _error("BACKUP_ARCHIVE_INVALID")
            archive_manifest = ArchiveManifest.from_dict(
                _mapping(self._read_json(archive, wrapper_info, metadata=True))
            )
            if archive_manifest.lineage_mode is not ArchiveLineageMode.CURRENT_ONLY:
                raise ValueError("not a current-only archive")
            name = f"g{archive_manifest.revision:08d}_{archive_manifest.generation_id}"
            prefix = f"{_PREFIX}/generations/{name}"
            entry_paths = {entry.path for entry in archive_manifest.entries}
            expected = {_ARCHIVE_MANIFEST, *entry_paths}
            if (
                len(entry_paths) != len(archive_manifest.entries)
                or set(infos) != expected
                or any(not path.startswith(f"{_PREFIX}/") for path in infos)
                or entry_paths != expected - {_ARCHIVE_MANIFEST}
            ):
                raise ValueError("archive layout mismatch")
            by_path = {entry.path: entry for entry in archive_manifest.entries}
            for path, entry in by_path.items():
                info = infos[path]
                if info.file_size != entry.size:
                    raise ValueError("archive entry size mismatch")
                self._verify_entry(archive, info, entry)
            identity = IdentityRecord.from_dict(
                _mapping(self._read_json(archive, infos[f"{_PREFIX}/{_IDENTITY}"], metadata=True))
            )
            current = CurrentPointer.from_dict(
                _mapping(self._read_json(archive, infos[f"{_PREFIX}/{_CURRENT}"], metadata=True))
            )
            manifest_bytes = self._read_raw(
                archive, infos[f"{prefix}/manifest.json"], metadata=True
            )
            manifest = SessionManifest.from_dict(_mapping(json.loads(manifest_bytes)))
            record = SessionRecord.from_dict(
                _mapping(self._read_json(archive, infos[f"{prefix}/session.json"], metadata=True))
            )
            payloads = {f"{prefix}/{entry.path}" for entry in manifest.files}
            controls = {
                f"{_PREFIX}/{_IDENTITY}",
                f"{_PREFIX}/{_CURRENT}",
                f"{prefix}/manifest.json",
                f"{prefix}/session.json",
            }
            if entry_paths != controls | payloads or any(
                path not in entry_paths for path in payloads
            ):
                raise ValueError("archive includes non-allowlisted payload")
            if (
                identity.session_id != manifest.session_id
                or current.session_id != manifest.session_id
                or current.revision != manifest.revision
                or current.generation_id != manifest.generation_id
                or current.generation_relpath != f"generations/{name}"
                or current.manifest_sha256 != _sha_bytes(manifest_bytes)
                or record.session_id != manifest.session_id
                or record.revision != manifest.revision
                or record.state is not manifest.state
            ):
                raise ValueError("archive identity/current mismatch")
            if (
                archive_manifest.session_id != manifest.session_id
                or archive_manifest.revision != manifest.revision
                or archive_manifest.generation_id != manifest.generation_id
                or archive_manifest.manifest_sha256 != _sha_bytes(manifest_bytes)
                or archive_manifest.omitted_parent != _omitted_parent(manifest)
            ):
                raise ValueError("archive manifest identity or lineage mismatch")
            for item in manifest.files:
                entry = by_path[f"{prefix}/{item.path}"]
                if entry.size != item.size or entry.sha256 != item.sha256:
                    raise ValueError("payload differs from session manifest")
            return Ok(_Layout(archive_manifest, identity, current, manifest))

    def _safe_infos(self, entries: Iterable[zipfile.ZipInfo]) -> dict[str, zipfile.ZipInfo]:
        entries = tuple(entries)
        if len(entries) > self.MAX_ENTRIES:
            raise ValueError("too many entries")
        result: dict[str, zipfile.ZipInfo] = {}
        keys: set[str] = set()
        total = 0
        compressed_total = 0
        for info in entries:
            name = info.filename
            if (
                not _portable_path(name)
                or name in result
                or unicodedata.normalize("NFC", name).casefold() in keys
                or info.is_dir()
                or info.extra
                or info.external_attr & (0x10 | 0x400)
                or info.flag_bits & 1
                or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            ):
                raise ValueError("unsafe archive member")
            mode = info.external_attr >> 16
            if info.create_system == 3 and (
                stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
            ):
                raise ValueError("special archive member")
            if info.file_size > self.MAX_ENTRY_BYTES or (
                info.file_size
                and (
                    info.compress_size == 0
                    or info.file_size > info.compress_size * self.MAX_COMPRESSION_RATIO
                )
            ):
                raise ValueError("archive quota exceeded")
            total += info.file_size
            compressed_total += info.compress_size
            if total > self.MAX_TOTAL_BYTES or (
                total
                and (compressed_total == 0 or total > compressed_total * self.MAX_COMPRESSION_RATIO)
            ):
                raise ValueError("total quota exceeded")
            result[name] = info
            keys.add(unicodedata.normalize("NFC", name).casefold())
        return result

    def _read_raw(
        self, archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, metadata: bool
    ) -> bytes:
        limit = self.MAX_METADATA_BYTES if metadata else self.MAX_ENTRY_BYTES
        if info.file_size > limit:
            raise ValueError("metadata quota exceeded")
        with archive.open(info) as stream:
            data = stream.read(limit + 1)
        if len(data) != info.file_size or len(data) > limit:
            raise ValueError("invalid entry size")
        return data

    def _read_json(
        self, archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, metadata: bool
    ) -> object:
        return json.loads(self._read_raw(archive, info, metadata=metadata).decode("utf-8"))

    def _verify_entry(
        self, archive: zipfile.ZipFile, info: zipfile.ZipInfo, entry: ArchiveEntry
    ) -> None:
        digest = hashlib.sha256()
        crc = 0
        size = 0
        with archive.open(info) as stream:
            while chunk := stream.read(_CHUNK_SIZE):
                size += len(chunk)
                digest.update(chunk)
                crc = zlib.crc32(chunk, crc)
        if (
            size != entry.size
            or digest.hexdigest() != entry.sha256
            or (crc & 0xFFFFFFFF) != info.CRC
        ):
            raise ValueError("archive payload integrity mismatch")

    def _write_member(self, archive: zipfile.ZipFile, path: str, source: BinaryIO) -> ArchiveMember:
        digest = hashlib.sha256()
        crc = 0
        size = 0
        info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0
        with archive.open(info, "w") as output:
            while chunk := source.read(_CHUNK_SIZE):
                size += len(chunk)
                if size > self.MAX_ENTRY_BYTES:
                    raise ValueError("entry quota exceeded")
                digest.update(chunk)
                crc = zlib.crc32(chunk, crc)
                output.write(chunk)
        return ArchiveMember(path, size, digest.hexdigest(), crc & 0xFFFFFFFF)

    def _stream_entry(
        self,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        destination: Path,
        entry: ArchiveEntry,
    ) -> None:
        with archive.open(info) as source, destination.open("xb") as output:
            digest = hashlib.sha256()
            size = 0
            while chunk := source.read(_CHUNK_SIZE):
                size += len(chunk)
                if size > self.MAX_ENTRY_BYTES:
                    raise ValueError("entry quota exceeded")
                digest.update(chunk)
                output.write(chunk)
        if size != entry.size or digest.hexdigest() != entry.sha256:
            raise ValueError("archive payload integrity mismatch")

    def _safe_target(self, target: Path) -> Path:
        target.mkdir(parents=True, exist_ok=True)
        resolved = target.resolve(strict=True)
        if _reparse_in_path(target) or _reparse_in_path(resolved):
            raise ValueError("extraction target contains reparse point")
        return resolved

    def _safe_parent(self, root: Path, candidate: Path) -> Path:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("extraction path escapes staging") from exc
        if _reparse_in_path(candidate, stop=root.parent):
            raise ValueError("extraction path contains reparse point")
        return candidate


class _WindowsRestoreAdapter:
    """Handle-pinned Windows extraction tree; path names are never reopened unchecked."""

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _SHARE_READ_WRITE = 0x00000003
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

    def __init__(self, target: Path) -> None:
        try:
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._create_file = kernel32.CreateFileW
            self._create_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            self._create_file.restype = wintypes.HANDLE
            self._close_handle = kernel32.CloseHandle
            self._close_handle.argtypes = (wintypes.HANDLE,)
            self._close_handle.restype = wintypes.BOOL
            self._create_directory = kernel32.CreateDirectoryW
            self._create_directory.argtypes = (wintypes.LPCWSTR, ctypes.c_void_p)
            self._create_directory.restype = wintypes.BOOL

            class _ByHandleFileInformation(ctypes.Structure):
                _fields_ = (
                    ("attributes", wintypes.DWORD),
                    ("creation_time", wintypes.FILETIME),
                    ("last_access_time", wintypes.FILETIME),
                    ("last_write_time", wintypes.FILETIME),
                    ("volume_serial", wintypes.DWORD),
                    ("file_size_high", wintypes.DWORD),
                    ("file_size_low", wintypes.DWORD),
                    ("number_of_links", wintypes.DWORD),
                    ("file_index_high", wintypes.DWORD),
                    ("file_index_low", wintypes.DWORD),
                )

            self._file_information_type = _ByHandleFileInformation
            self._file_information = kernel32.GetFileInformationByHandle
            self._file_information.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(_ByHandleFileInformation),
            )
            self._file_information.restype = wintypes.BOOL
        except (AttributeError, OSError):
            raise _RestorePlatformUnavailable() from None
        self._directories: dict[tuple[str, ...], tuple[Path, int, tuple[int, int]]] = {}
        self._ancestors: list[tuple[Path, int, tuple[int, int]]] = []
        self._files: list[tuple[int, tuple[int, int]]] = []
        try:
            target_handle, target_identity = self._open_target_directory(target)
        except BaseException:
            self.close()
            raise
        self._directories[()] = (target, target_handle, target_identity)
        self.staging: Path
        self.ownership: StagingOwnership

    @classmethod
    def create(cls, target: Path) -> _WindowsRestoreAdapter:
        restore = cls(target)
        try:
            name = f".backup-staging-{uuid.uuid4().hex}"
            staging = target / name
            if not restore._create_directory(str(staging), None):
                raise ctypes.WinError(ctypes.get_last_error())
            staging_handle, staging_identity = restore._open_directory(staging)
            restore._directories[("_staging",)] = (staging, staging_handle, staging_identity)
            restore.staging = staging
            tree_device, tree_inode = _path_identity(staging)
            parent_device, parent_inode = _path_identity(target)
            restore.ownership = StagingOwnership(
                tree_device, tree_inode, parent_device, parent_inode
            )
            return restore
        except BaseException:
            restore.close()
            raise

    def __enter__(self) -> _WindowsRestoreAdapter:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        for descriptor, _identity in self._files:
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._files.clear()
        for _path, handle, _identity in reversed(tuple(self._directories.values())):
            self._close_handle(handle)
        self._directories.clear()
        for _path, handle, _identity in reversed(self._ancestors):
            self._close_handle(handle)
        self._ancestors.clear()

    def _info(self, handle: int) -> tuple[int, tuple[int, int]]:
        result = self._file_information_type()
        if not self._file_information(handle, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        file_index = (int(result.file_index_high) << 32) | int(result.file_index_low)
        return int(result.attributes), (int(result.volume_serial), file_index)

    def _open_target_directory(self, target: Path) -> tuple[int, tuple[int, int]]:
        if not target.anchor:
            raise ValueError("restore target must be absolute")
        current = Path(target.anchor)
        handle, identity = self._open_directory(current)
        for part in target.parts[1:]:
            self._ancestors.append((current, handle, identity))
            current /= part
            handle, identity = self._open_directory(current)
        return handle, identity
    def _open_directory(self, path: Path) -> tuple[int, tuple[int, int]]:
        handle = self._create_file(
            str(path),
            self._GENERIC_READ,
            self._SHARE_READ_WRITE,
            None,
            self._OPEN_EXISTING,
            self._FILE_FLAG_BACKUP_SEMANTICS | self._FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            attributes, identity = self._info(handle)
            if attributes & self._FILE_ATTRIBUTE_REPARSE_POINT or not (
                attributes & self._FILE_ATTRIBUTE_DIRECTORY
            ):
                raise ValueError("restore directory is a reparse point")
            return handle, identity
        except BaseException:
            self._close_handle(handle)
            raise

    def _directory(self, parts: tuple[str, ...]) -> tuple[Path, int, tuple[int, int]]:
        key = ("_staging", *parts)
        existing = self._directories.get(key)
        if existing is not None:
            return existing
        parent = self._directory(parts[:-1])
        path = parent[0] / parts[-1]
        if not self._create_directory(str(path), None):
            raise ValueError("restore child was pre-created")
        handle, identity = self._open_directory(path)
        created = (path, handle, identity)
        self._directories[key] = created
        return created

    def stream_entry(
        self,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        parts: tuple[str, ...],
        entry: ArchiveEntry,
    ) -> None:
        if not parts or any(not part for part in parts):
            raise ValueError("archive entry has no relative path")
        parent = self._directory(parts[:-1])
        path = parent[0] / parts[-1]
        handle = self._create_file(
            str(path),
            self._GENERIC_WRITE,
            self._SHARE_READ_WRITE,
            None,
            self._CREATE_NEW,
            self._FILE_ATTRIBUTE_NORMAL | self._FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        descriptor = -1
        try:
            attributes, identity = self._info(handle)
            if attributes & (self._FILE_ATTRIBUTE_REPARSE_POINT | self._FILE_ATTRIBUTE_DIRECTORY):
                raise ValueError("restore file is unsafe")
            import msvcrt

            descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
            handle = 0
            digest = hashlib.sha256()
            size = 0
            with archive.open(info) as source:
                while chunk := source.read(_CHUNK_SIZE):
                    size += len(chunk)
                    if size > BackupArchive.MAX_ENTRY_BYTES:
                        raise ValueError("entry quota exceeded")
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("restore write failed")
                        view = view[written:]
            native_handle = msvcrt.get_osfhandle(descriptor)
            final_attributes, final_identity = self._info(native_handle)
            unsafe_attributes = self._FILE_ATTRIBUTE_REPARSE_POINT | self._FILE_ATTRIBUTE_DIRECTORY
            if (
                final_identity != identity
                or final_attributes & unsafe_attributes
                or size != entry.size
                or digest.hexdigest() != entry.sha256
            ):
                raise ValueError("archive payload integrity mismatch")
            self._files.append((descriptor, identity))
            descriptor = -1
        except BaseException:
            if descriptor != -1:
                os.close(descriptor)
            elif handle:
                self._close_handle(handle)
            raise

    def verify(self) -> None:
        import msvcrt

        unsafe_attributes = self._FILE_ATTRIBUTE_REPARSE_POINT | self._FILE_ATTRIBUTE_DIRECTORY
        for _path, handle, identity in (*self._ancestors, *self._directories.values()):
            attributes, current = self._info(handle)
            if current != identity or attributes & self._FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("restore parent identity changed")
        for descriptor, identity in self._files:
            attributes, current = self._info(msvcrt.get_osfhandle(descriptor))
            if current != identity or attributes & unsafe_attributes:
                raise ValueError("restore file identity changed")
class _RestorePlatformUnavailable(Exception):
    pass
@dataclass(frozen=True, slots=True)
class _Layout:
    archive_manifest: ArchiveManifest
    identity: IdentityRecord
    current: CurrentPointer
    manifest: SessionManifest


def _path_identity(path: Path) -> tuple[int, int]:
    metadata = os.lstat(path)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & 0x0400)
    ):
        raise ValueError("restore path is not an owned directory")
    return metadata.st_dev, metadata.st_ino


def _move_directory_no_replace(source: Path, target: Path) -> None:
    if os.name == "nt":
        from ctypes import wintypes

        move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileW
        move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        move_file.restype = wintypes.BOOL
        if not move_file(str(source), str(target)):
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error), str(target))
        return
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise _RestorePlatformUnavailable()
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))

def _reparse_in_path(path: Path, stop: Path | None = None) -> bool:
    current = path
    while True:
        try:
            if current.exists() and (
                current.is_symlink()
                or bool(
                    getattr(os.lstat(current), "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                )
            ):
                return True
        except OSError:
            raise
        if stop is not None and current == stop:
            return False
        if current.parent == current:
            return False
        current = current.parent


def _source_identity(handle: BinaryIO) -> SourceFileIdentity:
    value = os.fstat(handle.fileno())
    return SourceFileIdentity(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _sha_handle(handle: BinaryIO) -> str:
    handle.seek(0)
    digest = hashlib.sha256()
    while chunk := handle.read(_CHUNK_SIZE):
        digest.update(chunk)
    handle.seek(0)
    return digest.hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError("invalid JSON object")
    return {key: item for key, item in value.items() if isinstance(key, str)}


__all__ = ["BackupArchive", "ExtractedBackup", "StagingOwnership"]
