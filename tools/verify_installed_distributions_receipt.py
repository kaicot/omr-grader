#!/usr/bin/env python3
"""Independently verify an installed-distribution receipt without loading its writer."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import subprocess
import sys
import sysconfig
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from supply_manifest import SupplyManifestError, verify_bundle

SCHEMA_VERSION = "installed-distributions-v1"
HEX = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[-_.]+")
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


class VerificationError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file(path: Path) -> None:
    status = path.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or path.is_symlink()
        or getattr(status, "st_file_attributes", 0) & 0x400
    ):
        raise VerificationError(f"unsafe or missing file: {path}")


def _name(value: str) -> str:
    name = _NAME.sub("-", value).lower()
    if not name or name.strip("-") != name:
        raise VerificationError(f"invalid distribution name: {value!r}")
    return name


def _safe_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or value.startswith(("/", "//"))
        or re.match(r"^[A-Za-z]:", value)
    ):
        raise VerificationError("unsafe installed path")
    item = PurePosixPath(value)
    if (
        item.is_absolute()
        or item.as_posix() != value
        or any(
            not part
            or part in {".", ".."}
            or unicodedata.normalize("NFC", part) != part
            or part[-1] in {" ", "."}
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
            or any(character in '<>:"\\|?*' or ord(character) < 32 for character in part)
            for part in item.parts
        )
    ):
        raise VerificationError("unsafe installed path")
    return value


def _locked(path: Path) -> set[tuple[str, str]]:
    _file(path)
    values: set[tuple[str, str]] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9!+_.-]+)(?:\s.*)?", line)
        if not match:
            raise VerificationError(f"lock is not exactly pinned: {raw!r}")
        item = (_name(match.group(1)), match.group(2))
        if item in values:
            raise VerificationError(f"duplicate lock requirement: {raw!r}")
        values.add(item)
    return values


def _wheel_record(wheel: Path, name: str, version: str) -> dict[str, tuple[str, str]]:
    record_name = f"{name.replace('-', '_')}-{version}.dist-info/RECORD"
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or record_name not in names:
                raise VerificationError("approved wheel has an invalid RECORD")
            records: dict[str, tuple[str, str]] = {}
            for row in csv.reader(archive.read(record_name).decode("utf-8").splitlines()):
                if len(row) != 3:
                    raise VerificationError("approved wheel RECORD is malformed")
                path = _safe_path(row[0])
                if path.casefold() in {entry.casefold() for entry in records}:
                    raise VerificationError("approved wheel RECORD has aliases")
                records[path] = (row[1], row[2])
    except (OSError, UnicodeError, zipfile.BadZipFile) as error:
        raise VerificationError(f"cannot read approved wheel: {wheel}") from error
    if set(records) != set(names) or records.get(record_name) != ("", ""):
        raise VerificationError("approved wheel RECORD does not bind all members")
    return records


def _site_roots() -> list[Path]:
    configured = [
        Path(value)
        for value in (sysconfig.get_paths().get("purelib"), sysconfig.get_paths().get("platlib"))
        if value
    ]
    if not configured:
        raise VerificationError("cannot locate live site-packages")
    roots: list[Path] = []
    resolved_from: dict[Path, str] = {}
    for original in configured:
        status = original.lstat()
        if (
            not stat.S_ISDIR(status.st_mode)
            or original.is_symlink()
            or getattr(status, "st_file_attributes", 0) & 0x400
        ):
            raise VerificationError(f"unsafe configured site-packages root: {original}")
        root = original.resolve(strict=True)
        previous = resolved_from.setdefault(root, str(original))
        if previous != str(original):
            raise VerificationError(f"configured site-packages root alias: {original}")
        if root not in roots:
            roots.append(root)
    return sorted(roots, key=lambda path: str(path).casefold())


def _pip_check() -> str:
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "pip", "check"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = completed.stdout.replace("\r\n", "\n")
    if completed.returncode:
        raise VerificationError(f"live pip check failed: {output.strip()}")
    return output


def _file_identity(path: Path) -> tuple[int, int]:
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or getattr(status, "st_nlink", 1) != 1:
        raise VerificationError(f"aliased or non-regular file: {path}")
    return status.st_dev, status.st_ino


def _receipt(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise VerificationError("receipt is not UTF-8 JSON") from error
    required = {
        "schema_version",
        "python",
        "bundle",
        "locks",
        "distributions",
        "pip_check",
        "pip_list",
        "expected_set",
        "unexpected_set",
        "missing_set",
        "pyinstaller_artifact",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema_version") != SCHEMA_VERSION
        or raw != _canonical(value)
    ):
        raise VerificationError("receipt schema or canonical bytes are invalid")
    return value




def verify(receipt_path: Path, expected_manifest_sha256: str, expected_writer_sha256: str) -> None:
    if not HEX.fullmatch(expected_manifest_sha256) or not HEX.fullmatch(expected_writer_sha256):
        raise VerificationError("approved manifest and writer digests must be lowercase SHA-256")
    receipt = _receipt(receipt_path)
    bundle = receipt["bundle"]
    if not isinstance(bundle, dict) or set(bundle) != {
        "path",
        "manifest_sha256",
        "writer_tool_sha256",
        "verifier_tool_sha256",
        "release_id",
    }:
        raise VerificationError("invalid bundle binding")
    if (
        bundle["manifest_sha256"] != expected_manifest_sha256
        or bundle["writer_tool_sha256"] != expected_writer_sha256
    ):
        raise VerificationError(
            "receipt is not bound to caller-approved manifest and writer identities"
        )
    if bundle["verifier_tool_sha256"] != _sha256(Path(__file__).resolve()):
        raise VerificationError("receipt verifier identity is not bound to this immutable verifier")
    python = receipt["python"]
    if (
        not isinstance(python, dict)
        or set(python) != {"path", "version", "architecture", "exe_sha256"}
        or python["path"] != str(Path(sys.executable).resolve())
        or python["version"] != platform.python_version()
        or python["architecture"] != platform.machine()
        or python["exe_sha256"] != _sha256(Path(sys.executable))
    ):
        raise VerificationError("receipt Python binding does not match the executing interpreter")
    if receipt["pip_check"] != _pip_check():
        raise VerificationError("receipt pip check does not match the live environment")
    roots = _site_roots()
    owned_identities: set[tuple[int, int]] = set()
    owned_paths: set[str] = set()
    artifact = receipt["pyinstaller_artifact"]
    if (
        not isinstance(artifact, dict)
        or set(artifact) != {"path", "size", "sha256"}
        or not isinstance(artifact["path"], str)
        or not isinstance(artifact["size"], int)
        or isinstance(artifact["size"], bool)
        or artifact["size"] < 0
        or not isinstance(artifact["sha256"], str)
        or not HEX.fullmatch(artifact["sha256"])
    ):
        raise VerificationError("receipt lacks a valid PyInstaller artifact binding")
    artifact_path = Path(artifact["path"])
    _file(artifact_path)
    if (
        artifact_path.stat().st_size != artifact["size"]
        or _sha256(artifact_path) != artifact["sha256"]
    ):
        raise VerificationError("PyInstaller artifact drift")
    try:
        verified = verify_bundle(Path(bundle["path"]), expected_manifest_sha256)
    except SupplyManifestError as error:
        raise VerificationError(f"strict supply verification failed: {error}") from error
    if verified.manifest["release_id"] != bundle["release_id"]:
        raise VerificationError("bundle release identifier mismatch")
    approved_wheels: dict[str, dict[str, Any]] = {}
    manifest_pairs: set[tuple[str, str]] = set()
    for manifest_artifact in verified.manifest["artifacts"]:
        path = manifest_artifact["path"]
        if path.startswith("wheelhouse/") and path.endswith(".whl"):
            wheel_name = Path(path).name
            if wheel_name in approved_wheels:
                raise VerificationError("duplicate approved wheel filename")
            pair = (_name(manifest_artifact["distribution"]), str(manifest_artifact["version"]))
            if pair in manifest_pairs:
                raise VerificationError("duplicate approved wheel distribution")
            manifest_pairs.add(pair)
            approved_wheels[wheel_name] = {
                "path": verified.bundle_root / path,
                "sha256": manifest_artifact["sha256"],
                "pair": pair,
            }
    locks = receipt["locks"]
    if not isinstance(locks, dict) or set(locks) != {"bootstrap", "constraints", "application"}:
        raise VerificationError("invalid lock bindings")
    expected_lock_paths = {
        "bootstrap": verified.bundle_root / "bootstrap.lock",
        "constraints": verified.bundle_root / "constraints/windows-py312.lock",
        "application": verified.bundle_root / "application.lock",
    }
    for name, binding in locks.items():
        if (
            not isinstance(binding, dict)
            or set(binding) != {"path", "sha256"}
            or not HEX.fullmatch(binding["sha256"])
            or Path(binding["path"]).resolve() != expected_lock_paths[name].resolve()
        ):
            raise VerificationError(f"invalid {name} lock binding")
        path = expected_lock_paths[name]
        _file(path)
        if _sha256(path) != binding["sha256"]:
            raise VerificationError(f"{name} lock hash mismatch")
    locked_pairs = set().union(*(_locked(path) for path in expected_lock_paths.values()))
    if locked_pairs != manifest_pairs:
        raise VerificationError("approved wheel set does not exactly match locked set")
    distributions = receipt["distributions"]
    if not isinstance(distributions, list) or not distributions:
        raise VerificationError("receipt lacks distributions")
    pairs: list[tuple[str, str]] = []
    for distribution in distributions:
        required = {
            "name",
            "version",
            "supplying_wheel",
            "wheel_sha256",
            "metadata_sha256",
            "record_sha256",
            "installed_files",
        }
        if not isinstance(distribution, dict) or set(distribution) != required:
            raise VerificationError("invalid distribution receipt schema")
        if not all(
            isinstance(distribution[key], str) and distribution[key]
            for key in required - {"installed_files"}
        ):
            raise VerificationError("invalid distribution identity")
        if not all(
            HEX.fullmatch(distribution[key])
            for key in ("wheel_sha256", "metadata_sha256", "record_sha256")
        ):
            raise VerificationError("invalid distribution digest")
        approved = approved_wheels.get(distribution["supplying_wheel"])
        pair = (_name(distribution["name"]), distribution["version"])
        if (
            approved is None
            or approved["sha256"] != distribution["wheel_sha256"]
            or approved["pair"] != pair
        ):
            raise VerificationError("distribution is not bound to its approved supplying wheel")
        wheel_records = _wheel_record(approved["path"], *pair)
        files = distribution["installed_files"]
        if not isinstance(files, list) or not files:
            raise VerificationError("distribution has no installed RECORD files")
        previous = ""
        receipt_files: dict[str, dict[str, Any]] = {}
        for item in files:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "size", "sha256"}
                or not isinstance(item["size"], int)
                or isinstance(item["size"], bool)
                or item["size"] < 0
                or not HEX.fullmatch(item["sha256"])
            ):
                raise VerificationError("invalid installed file receipt")
            relative = _safe_path(item["path"])
            if relative <= previous:
                raise VerificationError("installed files are not canonically sorted")
            previous = relative
            receipt_files[relative] = item
            matches = [root / relative for root in roots if (root / relative).exists()]
            if len(matches) != 1:
                raise VerificationError(f"installed path is ambiguous or absent: {relative}")
            _file(matches[0])
            identity = _file_identity(matches[0])
            if identity in owned_identities:
                raise VerificationError(f"global installed file alias: {relative}")
            owned_identities.add(identity)
            if relative.casefold() in owned_paths:
                raise VerificationError(f"duplicate installed ownership: {relative}")
            owned_paths.add(relative.casefold())
            if matches[0].stat().st_size != item["size"] or _sha256(matches[0]) != item["sha256"]:
                raise VerificationError(f"installed file drift: {relative}")
        if set(receipt_files) != set(wheel_records):
            raise VerificationError("receipt files do not exactly match approved wheel RECORD")
        record_name = f"{pair[0].replace('-', '_')}-{pair[1]}.dist-info/RECORD"
        record_matches = [root / record_name for root in roots if (root / record_name).exists()]
        if len(record_matches) != 1:
            raise VerificationError("installed RECORD is ambiguous or absent")
        with zipfile.ZipFile(approved["path"]) as archive:
            approved_record = archive.read(record_name)
        if (
            record_matches[0].read_bytes() != approved_record
            or _sha256(record_matches[0]) != distribution["record_sha256"]
        ):
            raise VerificationError("installed RECORD differs from approved wheel RECORD")
        metadata_name = f"{pair[0].replace('-', '_')}-{pair[1]}.dist-info/METADATA"
        metadata_matches = [
            root / metadata_name for root in roots if (root / metadata_name).exists()
        ]
        if (
            len(metadata_matches) != 1
            or _sha256(metadata_matches[0]) != distribution["metadata_sha256"]
        ):
            raise VerificationError("installed METADATA differs from receipt")
        for relative, (declared_hash, declared_size) in wheel_records.items():
            item = receipt_files[relative]
            if relative == record_name:
                if declared_hash or declared_size:
                    raise VerificationError("approved wheel RECORD self-entry is invalid")
                continue
            expected_hash = "sha256=" + base64.urlsafe_b64encode(
                bytes.fromhex(item["sha256"])
            ).decode("ascii").rstrip("=")
            if declared_hash != expected_hash or declared_size != str(item["size"]):
                raise VerificationError(
                    f"receipt file is not proven by approved wheel RECORD: {relative}"
                )
        pairs.append(pair)
    if pairs != sorted(pairs) or len(pairs) != len(set(pairs)):
        raise VerificationError("distribution inventory is not sorted and unique")
    if set(pairs) != locked_pairs:
        raise VerificationError("receipt inventory does not exactly match locked distributions")
    live_pairs: set[tuple[str, str]] = set()
    for live in importlib.metadata.distributions():
        live_path = Path(str(live._path)).resolve(strict=True)
        if not any(live_path.is_relative_to(root) for root in roots):
            raise VerificationError(
                f"live distribution metadata outside site-packages: {live_path}"
            )
        _file(live_path / "RECORD")
        raw_name = live.metadata.get("Name", "")
        name = _name(raw_name)
        pair = (name, live.version)
        if pair in live_pairs:
            raise VerificationError(f"duplicate live distribution: {name}=={live.version}")
        live_pairs.add(pair)
    if live_pairs != set(pairs):
        raise VerificationError("receipt inventory does not exactly match live distributions")
    for root in roots:
        for directory, subdirs, files in os.walk(root, followlinks=False):
            current = Path(directory)
            for child in [current / name for name in subdirs + files]:
                status = child.lstat()
                if child.is_symlink() or getattr(status, "st_file_attributes", 0) & 0x400:
                    raise VerificationError(f"link or reparse point in site-packages: {child}")
            for name in files:
                relative = _safe_path((current / name).relative_to(root).as_posix())
                if relative.casefold() not in owned_paths:
                    raise VerificationError(f"unowned installed file: {relative}")
    if receipt["pip_list"] != [{"name": name, "version": version} for name, version in pairs]:
        raise VerificationError("pip list does not bind the verified inventory")
    if (
        receipt["expected_set"] != [f"{name}=={version}" for name, version in pairs]
        or receipt["missing_set"]
        or receipt["unexpected_set"]
    ):
        raise VerificationError("receipt set reconciliation failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument(
        "--expected-writer-tool-sha256",
        required=True,
        help="Immutable caller-approved writer digest; mutable writers cannot self-attest.",
    )
    args = parser.parse_args(argv)
    try:
        verify(args.receipt, args.expected_manifest_sha256, args.expected_writer_tool_sha256)
    except (VerificationError, OSError, ValueError, TypeError) as error:
        print(f"installed receipt verification error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
