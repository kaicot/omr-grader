#!/usr/bin/env python3
"""Write a canonical, fail-closed inventory of an offline wheel installation."""

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
_NAME = re.compile(r"[-_.]+")
_HEX = re.compile(r"[0-9a-f]{64}\Z")


class ReceiptError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalized_name(value: str) -> str:
    name = _NAME.sub("-", value).lower()
    if not name or name.strip("-") != name:
        raise ReceiptError(f"invalid distribution name: {value!r}")
    return name


def regular_file(path: Path) -> None:
    try:
        status = path.lstat()
    except FileNotFoundError as error:
        raise ReceiptError(f"missing file: {path}") from error
    if (
        not stat.S_ISREG(status.st_mode)
        or os.path.islink(path)
        or getattr(status, "st_file_attributes", 0) & 0x400
    ):
        raise ReceiptError(f"link, reparse point, or non-regular file: {path}")


def safe_relative(value: str) -> str:
    if "\\" in value or value.startswith(("//", "/")) or re.match(r"^[A-Za-z]:", value):
        raise ReceiptError(f"unsafe RECORD path: {value!r}")
    item = PurePosixPath(value)
    if (
        not value
        or item.is_absolute()
        or ".." in item.parts
        or any(
            not part
            or unicodedata.normalize("NFC", part) != part
            or part[-1] in {" ", "."}
            or part.split(".", 1)[0].casefold()
            in {
                "con",
                "prn",
                "aux",
                "nul",
                *(f"com{number}" for number in range(1, 10)),
                *(f"lpt{number}" for number in range(1, 10)),
            }
            or any(character in '<>:"\\|?*' or ord(character) < 32 for character in part)
            for part in item.parts
        )
    ):
        raise ReceiptError(f"unsafe RECORD path: {value!r}")
    normalized = item.as_posix()
    if normalized != value:
        raise ReceiptError(f"non-canonical RECORD path: {value!r}")
    return normalized


def _load_json(path: Path, label: str) -> dict[str, Any]:
    regular_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReceiptError(f"invalid {label}: {path}") from error
    if not isinstance(value, dict):
        raise ReceiptError(f"invalid {label} object: {path}")
    return value


def _exact_keys(value: dict[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise ReceiptError(f"invalid {label} keys")


def load_manifest(
    bundle: Path, expected_sha256: str
) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    if not _HEX.fullmatch(expected_sha256):
        raise ReceiptError("expected manifest SHA-256 must be lowercase hexadecimal")
    try:
        verified = verify_bundle(bundle, expected_sha256)
    except SupplyManifestError as error:
        raise ReceiptError(f"strict supply verification failed: {error}") from error
    manifest = verified.manifest
    _exact_keys(
        manifest, {"schema_version", "release_id", "created_at", "target", "artifacts"}, "manifest"
    )
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list):
        raise ReceiptError("manifest artifacts must be a list")
    wheels: dict[tuple[str, str], dict[str, Any]] = {}
    previous = ""
    artifact_keys = {
        "path",
        "size",
        "sha256",
        "role",
        "distribution",
        "version",
        "wheel_tags",
        "upstream_url",
        "license",
        "signing_evidence",
    }
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ReceiptError("invalid manifest artifact")
        _exact_keys(artifact, artifact_keys, "manifest artifact")
        path = safe_relative(str(artifact["path"]))
        if path <= previous:
            raise ReceiptError("manifest artifacts are not sorted and unique")
        previous = path
        if (
            not isinstance(artifact["size"], int)
            or isinstance(artifact["size"], bool)
            or artifact["size"] < 0
            or not isinstance(artifact["sha256"], str)
            or not _HEX.fullmatch(artifact["sha256"])
        ):
            raise ReceiptError("invalid manifest artifact digest")
        disk = bundle / path
        regular_file(disk)
        if disk.stat().st_size != artifact["size"] or sha256_file(disk) != artifact["sha256"]:
            raise ReceiptError(f"manifest artifact drift: {path}")
        if path.startswith("wheelhouse/") and path.endswith(".whl"):
            key = (normalized_name(str(artifact["distribution"])), str(artifact["version"]))
            if key in wheels:
                raise ReceiptError(f"duplicate manifest wheel: {key[0]}=={key[1]}")
            wheels[key] = {**artifact, "_disk": disk}
    if not wheels:
        raise ReceiptError("manifest has no wheels")
    return manifest, wheels


def parse_lock(path: Path) -> list[tuple[str, str]]:
    regular_file(path)
    rows: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("--") or line.startswith("-"):
            continue
        requirement = line.split(" ", 1)[0]
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9!+_.-]+)", requirement)
        if not match:
            raise ReceiptError(f"lock is not an exact pinned requirement: {raw!r}")
        rows.append((normalized_name(match.group(1)), match.group(2)))
    if len(rows) != len(set(rows)):
        raise ReceiptError(f"duplicate lock requirement: {path}")
    return rows


def _site_roots() -> list[Path]:
    original_roots = [
        Path(value)
        for value in (sysconfig.get_paths().get("purelib"), sysconfig.get_paths().get("platlib"))
        if value
    ]
    if not original_roots:
        raise ReceiptError("cannot locate site-packages")
    roots: list[Path] = []
    configured: dict[Path, str] = {}
    for original in original_roots:
        status = original.lstat()
        if (
            not stat.S_ISDIR(status.st_mode)
            or original.is_symlink()
            or getattr(status, "st_file_attributes", 0) & 0x400
        ):
            raise ReceiptError(f"unsafe configured site-packages root: {original}")
        root = original.resolve(strict=True)
        previous = configured.setdefault(root, str(original))
        if previous != str(original):
            raise ReceiptError(f"configured site-packages root alias: {original}")
        if root not in roots:
            roots.append(root)
    return sorted(roots, key=lambda value: str(value).casefold())


def _distribution_root(distribution: importlib.metadata.Distribution, roots: list[Path]) -> Path:
    candidate = Path(
        str(distribution._path)
    ).resolve()  # importlib's public API lacks this location.
    for root in roots:
        try:
            candidate.relative_to(root)
            return root
        except ValueError:
            pass
    raise ReceiptError(f"distribution outside site-packages: {candidate}")


def _record_entries(dist_path: Path, root: Path) -> tuple[str, list[tuple[str, Path]]]:
    if not dist_path.name.endswith(".dist-info"):
        raise ReceiptError(f"non-wheel distribution metadata: {dist_path}")
    direct_url = dist_path / "direct_url.json"
    if direct_url.exists() or direct_url.is_symlink():
        raise ReceiptError(f"direct URL or editable install: {dist_path}")
    record = dist_path / "RECORD"
    regular_file(record)
    entries: list[tuple[str, Path]] = []
    seen: set[str] = set()
    try:
        with record.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        for row in rows:
            if len(row) != 3:
                raise ReceiptError(f"invalid RECORD row: {record}")
            relative = safe_relative(row[0])
            key = relative.casefold()
            if key in seen:
                raise ReceiptError(f"duplicate or alias RECORD path: {relative}")
            seen.add(key)
            disk = root / relative
            try:
                disk.relative_to(root)
            except ValueError as error:
                raise ReceiptError(f"RECORD escapes site-packages: {relative}") from error
            regular_file(disk)
            entries.append((relative, disk))
    except UnicodeError as error:
        raise ReceiptError(f"invalid RECORD encoding: {record}") from error
    if not entries:
        raise ReceiptError(f"empty RECORD: {record}")
    return sha256_file(record), entries


def _validate_wheel_install(
    wheel: dict[str, Any], record_path: str, entries: list[tuple[str, Path]]
) -> None:
    try:
        with zipfile.ZipFile(wheel["_disk"]) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or any(
                info.is_dir() or info.external_attr >> 16 & 0o170000 == 0o120000
                for info in archive.infolist()
            ):
                raise ReceiptError("wheel contains duplicate, directory, or link entry")
            if record_path not in names:
                raise ReceiptError("wheel is missing its RECORD")
            expected: dict[str, tuple[str, str]] = {}
            wheel_record = archive.read(record_path)
            if (
                entries[[relative for relative, _ in entries].index(record_path)][1].read_bytes()
                != wheel_record
            ):
                raise ReceiptError("installed RECORD differs from approved wheel RECORD")
            for row in csv.reader(wheel_record.decode("utf-8").splitlines()):
                if len(row) != 3:
                    raise ReceiptError("invalid wheel RECORD row")
                relative = safe_relative(row[0])
                if relative.casefold() in {item.casefold() for item in expected}:
                    raise ReceiptError("wheel RECORD has path aliases")
                expected[relative] = (row[1], row[2])
    except (OSError, UnicodeError, zipfile.BadZipFile) as error:
        raise ReceiptError(f"invalid manifest wheel: {wheel['path']}") from error
    actual = {relative: (sha256_file(path), str(path.stat().st_size)) for relative, path in entries}
    if set(expected) != set(actual):
        raise ReceiptError("installed files do not exactly match wheel RECORD")
    for relative, (declared_hash, declared_size) in expected.items():
        digest, size = actual[relative]
        if relative == record_path:
            if declared_hash or declared_size:
                raise ReceiptError("wheel RECORD self-entry must omit hash and size")
            continue
        expected_hash = "sha256=" + base64.urlsafe_b64encode(bytes.fromhex(digest)).decode(
            "ascii"
        ).rstrip("=")
        if declared_hash != expected_hash or declared_size != size:
            raise ReceiptError(f"installed file differs from wheel: {relative}")


def inventory(wheels: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    roots = _site_roots()
    found: dict[tuple[str, str], importlib.metadata.Distribution] = {}
    owned: dict[str, str] = {}
    identities: set[tuple[int, int]] = set()
    result: list[dict[str, Any]] = []
    for distribution in importlib.metadata.distributions():
        name = normalized_name(distribution.metadata.get("Name", ""))
        version = distribution.version
        key = (name, version)
        if key in found:
            raise ReceiptError(f"duplicate installed distribution: {name}=={version}")
        root = _distribution_root(distribution, roots)
        dist_path = Path(str(distribution._path))
        if dist_path.is_symlink():
            raise ReceiptError(f"linked distribution metadata: {dist_path}")
        found[key] = distribution
        if key not in wheels:
            raise ReceiptError(f"installed distribution lacks manifest wheel: {name}=={version}")
        record_sha, entries = _record_entries(dist_path, root)
        wheel = wheels[key]
        record_path = (dist_path.relative_to(root) / "RECORD").as_posix()
        _validate_wheel_install(wheel, record_path, entries)
        installed_files = []
        for relative, disk in entries:
            owner_key = relative.casefold()
            if owner_key in owned:
                raise ReceiptError(f"owned-file collision: {relative}")
            owned[owner_key] = name
            identity = (disk.stat().st_dev, disk.stat().st_ino)
            if getattr(disk.stat(), "st_nlink", 1) != 1 or identity in identities:
                raise ReceiptError(f"global installed file alias: {relative}")
            identities.add(identity)
            installed_files.append(
                {"path": relative, "size": disk.stat().st_size, "sha256": sha256_file(disk)}
            )
        metadata = dist_path / "METADATA"
        regular_file(metadata)
        result.append(
            {
                "name": name,
                "version": version,
                "supplying_wheel": Path(str(wheel["path"])).name,
                "wheel_sha256": wheel["sha256"],
                "metadata_sha256": sha256_file(metadata),
                "record_sha256": record_sha,
                "installed_files": sorted(
                    installed_files, key=lambda item: item["path"].encode("utf-8")
                ),
            }
        )
    # No byte in a site-packages root may be silently outside an installed wheel RECORD.
    for root in roots:
        for directory, subdirs, files in os.walk(root, followlinks=False):
            current = Path(directory)
            for child in [current / name for name in subdirs + files]:
                if child.is_symlink() or getattr(child.lstat(), "st_file_attributes", 0) & 0x400:
                    raise ReceiptError(f"link or reparse point under site-packages: {child}")
            for filename in files:
                child = current / filename
                relative = child.relative_to(root).as_posix()
                if relative.casefold() not in owned:
                    raise ReceiptError(f"unowned installed file: {relative}")
    return sorted(result, key=lambda item: (item["name"], item["version"]))


def _run_pip_check() -> str:
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = completed.stdout.replace("\r\n", "\n")
    if completed.returncode:
        raise ReceiptError(f"pip check failed: {output.strip()}")
    return output


def build_receipt(
    bundle: Path,
    expected_manifest_sha256: str,
    bootstrap_lock: Path,
    constraints_lock: Path,
    application_lock: Path,
    pyinstaller_artifact: Path | None,
    verifier_tool_sha256: str,
) -> dict[str, Any]:
    if not isinstance(verifier_tool_sha256, str) or not _HEX.fullmatch(verifier_tool_sha256):
        raise ReceiptError("verifier tool SHA-256 must be supplied as lowercase hexadecimal")
    bundle = bundle.resolve()
    manifest, wheels = load_manifest(bundle, expected_manifest_sha256)
    locks = [
        ("bootstrap", bootstrap_lock),
        ("constraints", constraints_lock),
        ("application", application_lock),
    ]
    expected = {entry for _, lock in locks for entry in parse_lock(lock)}
    installed = inventory(wheels)
    installed_set = {(item["name"], item["version"]) for item in installed}
    missing = sorted(expected - installed_set)
    unexpected = sorted(installed_set - expected)
    if missing or unexpected:
        raise ReceiptError(
            f"installed set mismatch: missing={missing!r}, unexpected={unexpected!r}"
        )
    if set(wheels) != expected:
        raise ReceiptError("manifest wheel set does not exactly match locked set")
    artifact: dict[str, Any] | None
    if pyinstaller_artifact is None:
        raise ReceiptError("a PyInstaller artifact binding is required")
    regular_file(pyinstaller_artifact)
    artifact: dict[str, Any] = {
        "path": str(pyinstaller_artifact.resolve()),
        "size": pyinstaller_artifact.stat().st_size,
        "sha256": sha256_file(pyinstaller_artifact),
    }
    lock_values = {
        name: {"path": str(path.resolve()), "sha256": sha256_file(path)} for name, path in locks
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "python": {
            "path": str(Path(sys.executable).resolve()),
            "version": platform.python_version(),
            "architecture": platform.machine(),
            "exe_sha256": sha256_file(Path(sys.executable)),
        },
        "bundle": {
            "path": str(bundle),
            "manifest_sha256": expected_manifest_sha256,
            "writer_tool_sha256": sha256_file(Path(__file__).resolve()),
            "verifier_tool_sha256": verifier_tool_sha256,
            "release_id": manifest["release_id"],
        },
        "locks": lock_values,
        "distributions": installed,
        "pip_check": _run_pip_check(),
        "pip_list": [{"name": item["name"], "version": item["version"]} for item in installed],
        "expected_set": [f"{name}=={version}" for name, version in sorted(expected)],
        "unexpected_set": [],
        "missing_set": [],
        "pyinstaller_artifact": artifact,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--bootstrap-lock", required=True, type=Path)
    parser.add_argument("--constraints-lock", required=True, type=Path)
    parser.add_argument("--application-lock", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--pyinstaller-artifact", required=True, type=Path)
    parser.add_argument("--verifier-tool-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        receipt = build_receipt(
            args.bundle,
            args.expected_manifest_sha256,
            args.bootstrap_lock,
            args.constraints_lock,
            args.application_lock,
            args.pyinstaller_artifact,
            args.verifier_tool_sha256,
        )
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_bytes(canonical_bytes(receipt))
    except (ReceiptError, OSError, ValueError) as error:
        print(f"installed receipt error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
