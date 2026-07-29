"""Validate and normalize the immutable first-party wheel format."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import os
import re
import stat
import unicodedata
import zipfile
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path

WHEEL_RE = re.compile(r"^omr_grader-([A-Za-z0-9][A-Za-z0-9._]*?)-py3-none-any\.whl$")
DIST_FILES = {"METADATA", "WHEEL", "entry_points.txt", "top_level.txt", "RECORD"}


class WheelValidationError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _b64_digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def _safe_member(name: str) -> None:
    if not name or "\\" in name or name.startswith("/") or name.endswith("/"):
        raise WheelValidationError(f"unsafe wheel member path: {name!r}")
    if "\x00" in name or unicodedata.normalize("NFC", name) != name:
        raise WheelValidationError(f"non-canonical wheel member path: {name!r}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WheelValidationError(f"unsafe wheel member path: {name!r}")


def _expected_name(version: str) -> str:
    return f"omr_grader-{version}.dist-info"


def _validate_name_set(names: list[str], version: str) -> tuple[str, str]:
    seen_casefold: set[str] = set()
    for name in names:
        _safe_member(name)
        folded = name.casefold()
        if folded in seen_casefold:
            raise WheelValidationError(f"wheel member alias: {name!r}")
        seen_casefold.add(folded)
    if len(names) != len(set(names)):
        raise WheelValidationError("duplicate wheel member")
    dist_info = _expected_name(version)
    record = f"{dist_info}/RECORD"
    allowed_dist = {f"{dist_info}/{part}" for part in DIST_FILES}
    for name in names:
        if name.startswith("omr_grader/"):
            if not name.endswith(".py"):
                raise WheelValidationError(f"unexpected package member: {name}")
        elif name not in allowed_dist:
            raise WheelValidationError(f"unexpected wheel member: {name}")
    required = {
        "omr_grader/__init__.py",
        f"{dist_info}/METADATA",
        f"{dist_info}/WHEEL",
        f"{dist_info}/entry_points.txt",
        record,
    }
    missing = required.difference(names)
    if missing:
        raise WheelValidationError(f"missing wheel members: {sorted(missing)!r}")
    return dist_info, record


def _timestamp(epoch: int) -> tuple[int, int, int, int, int, int]:
    if epoch < 315532800 or epoch > 4354819198 or epoch % 2:
        raise WheelValidationError(
            "SOURCE_DATE_EPOCH must be an even ZIP timestamp from 1980 through 2107"
        )
    return datetime.fromtimestamp(epoch, UTC).timetuple()[:6]


def _record_bytes(entries: list[tuple[str, bytes]], record_name: str) -> bytes:
    rows = [[name, f"sha256={_b64_digest(data)}", str(len(data))] for name, data in entries]
    rows.append([record_name, "", ""])

    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    return stream.getvalue().encode("utf-8")


def _canonical_wheel_bytes(entries: list[tuple[str, bytes]], record: str, epoch: int) -> bytes:
    ordered = sorted(entries)
    payload = io.BytesIO()
    with zipfile.ZipFile(
        payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, data in [*ordered, (record, _record_bytes(ordered, record))]:
            info = zipfile.ZipInfo(name, _timestamp(epoch))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return payload.getvalue()


def normalize_wheel(path: Path, epoch: int) -> None:
    """Rewrite a pip-produced pure wheel into the contract's canonical ZIP bytes."""
    path = Path(path)
    match = WHEEL_RE.fullmatch(path.name)
    if not match:
        raise WheelValidationError(f"not an omr_grader pure wheel: {path.name}")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        _, record = _validate_name_set(names, match.group(1))
        entries = [(name, archive.read(name)) for name in names if name != record]
    replacement = path.with_suffix(".tmp")
    replacement.write_bytes(_canonical_wheel_bytes(entries, record, epoch))
    os.replace(replacement, path)


def validate_wheel(path: Path, epoch: int, expected_version: str | None = None) -> str:
    path = Path(path)
    match = WHEEL_RE.fullmatch(path.name)
    if not match:
        raise WheelValidationError(f"not an omr_grader pure wheel: {path.name}")
    version = match.group(1)
    if expected_version is not None and version != expected_version:
        raise WheelValidationError(
            f"wheel version {version!r} does not equal expected {expected_version!r}"
        )
    expected_stamp = _timestamp(epoch)
    with zipfile.ZipFile(path) as archive:
        if archive.comment:
            raise WheelValidationError("wheel ZIP comment is forbidden")
        infos = archive.infolist()
        names = [info.filename for info in infos]
        dist_info, record = _validate_name_set(names, version)
        if names != sorted(name for name in names if name != record) + [record]:
            raise WheelValidationError("wheel members are not in canonical order")
        records: dict[str, tuple[str, str]] = {}
        for info in infos:
            if info.date_time != expected_stamp:
                raise WheelValidationError(f"non-canonical timestamp: {info.filename}")
            if (
                info.is_dir()
                or info.create_system != 3
                or (info.external_attr >> 16) != (stat.S_IFREG | 0o644)
            ):
                raise WheelValidationError(f"non-canonical mode: {info.filename}")
            if info.extra or info.flag_bits or info.compress_type != zipfile.ZIP_DEFLATED:
                raise WheelValidationError(f"non-canonical ZIP metadata: {info.filename}")
        try:
            metadata = BytesParser().parsebytes(archive.read(f"{dist_info}/METADATA"))
            wheel = BytesParser().parsebytes(archive.read(f"{dist_info}/WHEEL"))
        except UnicodeError as error:
            raise WheelValidationError("METADATA or WHEEL is not UTF-8") from error
        if metadata.get_all("Name") != ["omr-grader"] or metadata.get_all("Version") != [version]:
            raise WheelValidationError("METADATA does not bind the wheel name and version")
        if (
            wheel.get_all("Wheel-Version") != ["1.0"]
            or wheel.get_all("Root-Is-Purelib") != ["true"]
            or wheel.get_all("Tag") != ["py3-none-any"]
        ):
            raise WheelValidationError("WHEEL is not the required pure py3-none-any wheel")
        with archive.open(record) as stream:
            text = stream.read().decode("utf-8")
        rows = list(csv.reader(text.splitlines()))
        if any(len(row) != 3 for row in rows):
            raise WheelValidationError("malformed RECORD")
        for name, digest, size in rows:
            if name in records:
                raise WheelValidationError("duplicate RECORD entry")
            records[name] = (digest, size)
        if set(records) != set(names):
            raise WheelValidationError("RECORD entries do not exactly match wheel members")
        if records[record] != ("", ""):
            raise WheelValidationError("RECORD must omit its own hash and size")
        for name in names:
            if name == record:
                continue
            payload = archive.read(name)
            if records[name] != (f"sha256={_b64_digest(payload)}", str(len(payload))):
                raise WheelValidationError(f"RECORD mismatch: {name}")
        entries = [(name, archive.read(name)) for name in names if name != record]
        if path.read_bytes() != _canonical_wheel_bytes(entries, record, epoch):
            raise WheelValidationError("wheel bytes differ from regenerated canonical ZIP")
    return sha256_file(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--normalize", action="store_true")
    args = parser.parse_args()
    if args.normalize:
        normalize_wheel(args.wheel, args.source_date_epoch)
    print(validate_wheel(args.wheel, args.source_date_epoch, args.expected_version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
