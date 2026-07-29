"""Offline, immutable Windows CPython 3.12 supply-bundle verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime
from email.parser import BytesParser
from enum import IntEnum
from pathlib import Path
from typing import Any, NoReturn


class ExitCode(IntEnum):
    OK = 0
    ARGUMENT = 2
    MANIFEST_DIGEST = 20
    MANIFEST_FORMAT = 21
    BUNDLE_LAYOUT = 22
    FILE_INTEGRITY = 23
    UNSAFE_PATH = 24
    INTERNAL = 70


class SupplyManifestError(Exception):
    def __init__(self, code: ExitCode, message: str) -> None:
        super().__init__(message)
        self.code = code


TOP_LEVEL_KEYS = ("schema_version", "release_id", "created_at", "target", "artifacts")
ARTIFACT_KEYS = (
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
)
RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
WHEEL_TAG = re.compile(r"^(?:cp312|py312|py3)-(?:cp312|abi3|none)-(?:win_amd64|any)$")
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


@dataclass(frozen=True)
class VerifiedBundle:
    bundle_root: Path
    manifest: dict[str, Any]


def _fail(code: ExitCode, message: str) -> NoReturn:
    raise SupplyManifestError(code, message)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(ExitCode.MANIFEST_FORMAT, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _is_reparse(path: Path, info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _check_component(component: str, whole_path: str) -> None:
    if not component or component in {".", ".."}:
        _fail(ExitCode.UNSAFE_PATH, f"unsafe path: {whole_path}")
    if unicodedata.normalize("NFC", component) != component:
        _fail(ExitCode.UNSAFE_PATH, f"path is not NFC: {whole_path}")
    stem = component.split(".", 1)[0].casefold()
    if (
        component[-1] in {" ", "."}
        or stem in WINDOWS_RESERVED_NAMES
        or any(char in '<>:"\\|?*' or ord(char) < 32 for char in component)
    ):
        _fail(ExitCode.UNSAFE_PATH, f"unsafe Windows path component: {whole_path}")


def _validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        _fail(ExitCode.MANIFEST_FORMAT, "artifact path must be a non-empty string")
    if "\\" in value or value.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", value):
        _fail(ExitCode.UNSAFE_PATH, f"artifact path is not portable: {value!r}")
    if unicodedata.normalize("NFC", value) != value:
        _fail(ExitCode.UNSAFE_PATH, f"artifact path is not NFC: {value!r}")
    parts = value.split("/")
    for part in parts:
        _check_component(part, value)
    return value


def _exact_keys(value: Any, expected: tuple[str, ...], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or tuple(value.keys()) != expected:
        _fail(ExitCode.MANIFEST_FORMAT, f"{label} keys must be exactly {', '.join(expected)}")
    return value


def _wheel_tags_from_name(name: str) -> list[str]:
    if not name.endswith(".whl"):
        return []
    pieces = name[:-4].split("-")
    if len(pieces) < 5:
        _fail(ExitCode.MANIFEST_FORMAT, f"invalid wheel filename: {name}")
    python_tags, abi_tags, platform_tags = pieces[-3:]
    return [
        f"{python_tag}-{abi_tag}-{platform_tag}"
        for python_tag in python_tags.split(".")
        for abi_tag in abi_tags.split(".")
        for platform_tag in platform_tags.split(".")
    ]


def _validate_artifact(artifact: Any) -> str:
    item = _exact_keys(artifact, ARTIFACT_KEYS, "artifact")
    path = _validate_relative_path(item["path"])
    if not isinstance(item["size"], int) or isinstance(item["size"], bool) or item["size"] < 0:
        _fail(ExitCode.MANIFEST_FORMAT, f"invalid size for {path}")
    if not isinstance(item["sha256"], str) or not SHA256.fullmatch(item["sha256"]):
        _fail(ExitCode.MANIFEST_FORMAT, f"invalid sha256 for {path}")
    for key in ("role", "distribution", "version", "upstream_url", "license"):
        if not isinstance(item[key], str) or not item[key].strip():
            _fail(ExitCode.MANIFEST_FORMAT, f"{key} must be a non-empty string for {path}")
    if not re.fullmatch(r"https://[^\s]+", item["upstream_url"]):
        _fail(ExitCode.MANIFEST_FORMAT, f"upstream_url must be an HTTPS URL for {path}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .+:/()_-]*", item["license"]):
        _fail(ExitCode.MANIFEST_FORMAT, f"invalid license evidence for {path}")
    evidence = item["signing_evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {
        "scheme",
        "identity",
        "signed_at",
        "digest",
    }:
        _fail(ExitCode.MANIFEST_FORMAT, f"invalid signing_evidence schema for {path}")
    if (
        not all(isinstance(evidence[key], str) and evidence[key].strip() for key in evidence)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .+:/@_-]*", evidence["scheme"])
        or not SHA256.fullmatch(evidence["digest"])
    ):
        _fail(ExitCode.MANIFEST_FORMAT, f"invalid signing_evidence values for {path}")
    try:
        signed_at = evidence["signed_at"].replace("Z", "+00:00")
        if not signed_at.endswith("+00:00"):
            raise ValueError
        datetime.fromisoformat(signed_at)
    except ValueError:
        _fail(ExitCode.MANIFEST_FORMAT, f"invalid signing_evidence timestamp for {path}")
    tags = item["wheel_tags"]
    if not isinstance(tags, list) or any(
        not isinstance(tag, str) or not WHEEL_TAG.fullmatch(tag) for tag in tags
    ):
        _fail(ExitCode.MANIFEST_FORMAT, f"invalid wheel_tags for {path}")
    if path.startswith("wheelhouse/"):
        if not path.endswith(".whl"):
            _fail(ExitCode.BUNDLE_LAYOUT, f"wheelhouse contains a non-wheel: {path}")
        filename_tags = _wheel_tags_from_name(Path(path).name)
        if not filename_tags or tags != filename_tags:
            _fail(ExitCode.MANIFEST_FORMAT, f"wheel tags do not match filename: {path}")
    elif tags:
        _fail(ExitCode.MANIFEST_FORMAT, f"non-wheel has wheel_tags: {path}")
    if path.endswith((".tar.gz", ".tgz", ".tar", ".zip")):
        _fail(ExitCode.BUNDLE_LAYOUT, f"source distribution is forbidden: {path}")
    if evidence["digest"] != item["sha256"]:
        _fail(ExitCode.MANIFEST_FORMAT, f"signing evidence digest does not bind artifact: {path}")
    return path


def _validate_wheel_binding(path: Path, artifact: dict[str, Any]) -> None:
    """Bind manifest identity fields to wheel filename and METADATA."""
    expected_name = Path(str(artifact["path"])).name
    filename = expected_name[:-4].split("-")
    if len(filename) < 5:
        _fail(ExitCode.MANIFEST_FORMAT, f"invalid wheel filename: {expected_name}")
    expected_distribution = re.sub(r"[-_.]+", "-", filename[0]).lower()
    if expected_distribution != re.sub(r"[-_.]+", "-", str(artifact["distribution"])).lower():
        _fail(ExitCode.MANIFEST_FORMAT, f"wheel filename distribution mismatch: {expected_name}")
    if filename[-4] != str(artifact["version"]):
        _fail(ExitCode.MANIFEST_FORMAT, f"wheel filename version mismatch: {expected_name}")
    dist_info = f"{filename[0].replace('-', '_')}-{filename[-4]}.dist-info/METADATA"
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = BytesParser().parsebytes(archive.read(dist_info))
    except (OSError, KeyError, UnicodeError, zipfile.BadZipFile) as error:
        _fail(ExitCode.FILE_INTEGRITY, f"wheel metadata is unreadable: {expected_name}: {error}")
    if re.sub(
        r"[-_.]+", "-", metadata.get("Name", "")
    ).lower() != expected_distribution or metadata.get("Version") != str(artifact["version"]):
        _fail(ExitCode.MANIFEST_FORMAT, f"wheel METADATA identity mismatch: {expected_name}")


def _check_tree(root: Path) -> set[str]:
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        _fail(ExitCode.BUNDLE_LAYOUT, f"bundle root does not exist: {root}")
    if not root.is_dir() or _is_reparse(root, root_info):
        _fail(ExitCode.UNSAFE_PATH, f"bundle root is not a real directory: {root}")

    files: set[str] = set()
    collisions: set[str] = set()
    file_identities: set[tuple[int, int]] = set()
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in [*directory_names, *file_names]:
            relative = (directory_path / name).relative_to(root).as_posix()
            _validate_relative_path(relative)
            folded = unicodedata.normalize("NFC", relative).casefold()
            if folded in collisions:
                _fail(ExitCode.UNSAFE_PATH, f"NFC/casefold path collision: {relative}")
            collisions.add(folded)
            entry = directory_path / name
            info = entry.lstat()
            if _is_reparse(entry, info):
                _fail(ExitCode.UNSAFE_PATH, f"link or reparse point is forbidden: {relative}")
            if name in file_names:
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) != 1:
                    _fail(
                        ExitCode.UNSAFE_PATH, f"alias or non-regular file is forbidden: {relative}"
                    )
                identity = (info.st_dev, info.st_ino)
                if identity in file_identities:
                    _fail(ExitCode.UNSAFE_PATH, f"global file alias is forbidden: {relative}")
                file_identities.add(identity)
                files.add(relative)
    return files


def _validate_layout(paths: set[str]) -> None:
    required = {
        "manifest.json",
        "cpython-3.12.10-amd64.exe",
        "pip-25.1.1.pyz",
        "bootstrap.lock",
        "constraints/windows-py312.lock",
        "application.lock",
    }
    if not required <= paths:
        _fail(ExitCode.BUNDLE_LAYOUT, f"required bundle files missing: {sorted(required - paths)}")
    wheels = [
        path
        for path in paths
        if path.startswith("wheelhouse/") and path.count("/") == 1 and path.endswith(".whl")
    ]
    licenses = [path for path in paths if path.startswith("licenses/") and path.count("/") == 1]
    if not wheels or not licenses:
        _fail(ExitCode.BUNDLE_LAYOUT, "wheelhouse and licenses must both be non-empty")
    allowed = required | set(wheels) | set(licenses)
    if paths != allowed:
        _fail(ExitCode.BUNDLE_LAYOUT, f"unexpected bundle paths: {sorted(paths - allowed)}")


def verify_bundle(bundle_root: str | Path, expected_manifest_sha256: str) -> VerifiedBundle:
    """Verify a bundle without executing or installing any of its artifacts."""
    if not isinstance(expected_manifest_sha256, str) or not SHA256.fullmatch(
        expected_manifest_sha256
    ):
        _fail(
            ExitCode.ARGUMENT,
            "expected manifest SHA-256 must be 64 lowercase hexadecimal characters",
        )
    root = Path(bundle_root)
    if root.name == "":
        _fail(ExitCode.ARGUMENT, "bundle root is required")
    expected_parent = Path("supply") / "windows-py312"
    parts = root.parts
    if len(parts) < 3 or tuple(parts[-3:-1]) != tuple(expected_parent.parts):
        _fail(ExitCode.BUNDLE_LAYOUT, "bundle root must be supply/windows-py312/<release-id>")
    if not RELEASE_ID.fullmatch(root.name):
        _fail(ExitCode.BUNDLE_LAYOUT, "invalid release-id directory")
    try:
        root_info = root.lstat()
    except OSError as error:
        _fail(ExitCode.BUNDLE_LAYOUT, f"cannot inspect bundle root: {error}")
    if not root.is_dir() or _is_reparse(root, root_info):
        _fail(ExitCode.UNSAFE_PATH, f"bundle root is not a real directory: {root}")
    manifest_path = root / "manifest.json"
    try:
        manifest_info = manifest_path.lstat()
    except OSError as error:
        _fail(ExitCode.BUNDLE_LAYOUT, f"cannot inspect manifest: {error}")
    if not stat.S_ISREG(manifest_info.st_mode) or _is_reparse(manifest_path, manifest_info):
        _fail(ExitCode.UNSAFE_PATH, "manifest is not a regular file")

    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as error:
        _fail(ExitCode.BUNDLE_LAYOUT, f"cannot read manifest: {error}")
    if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha256:
        _fail(ExitCode.MANIFEST_DIGEST, "external manifest SHA-256 does not match")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(ExitCode.MANIFEST_FORMAT, f"manifest is not UTF-8 JSON: {error}")
    try:
        canonical = (
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
            + b"\n"
        )
    except (TypeError, ValueError) as error:
        _fail(ExitCode.MANIFEST_FORMAT, f"manifest cannot be canonicalized: {error}")
    if manifest_bytes != canonical:
        _fail(ExitCode.MANIFEST_FORMAT, "manifest bytes are not canonical JSON with one LF")

    document = _exact_keys(manifest, TOP_LEVEL_KEYS, "manifest")
    if (
        document["schema_version"] != 1
        or not isinstance(document["release_id"], str)
        or not RELEASE_ID.fullmatch(document["release_id"])
    ):
        _fail(ExitCode.MANIFEST_FORMAT, "invalid schema_version or release_id")
    if document["release_id"] != root.name:
        _fail(ExitCode.BUNDLE_LAYOUT, "manifest release_id does not match bundle directory")
    if not isinstance(document["created_at"], str):
        _fail(ExitCode.MANIFEST_FORMAT, "created_at must be a UTC RFC 3339 timestamp")
    try:
        created_at = document["created_at"].replace("Z", "+00:00")
        if not created_at.endswith("+00:00"):
            raise ValueError
        datetime.fromisoformat(created_at)
    except ValueError:
        _fail(ExitCode.MANIFEST_FORMAT, "created_at must be a UTC RFC 3339 timestamp")
    if document["target"] != {"platform": "windows", "architecture": "x64", "python": "3.12.10"}:
        _fail(ExitCode.MANIFEST_FORMAT, "target must be Windows x64 CPython 3.12.10")
    if not isinstance(document["artifacts"], list):
        _fail(ExitCode.MANIFEST_FORMAT, "artifacts must be a list")

    listed_paths = [_validate_artifact(artifact) for artifact in document["artifacts"]]
    if "manifest.json" in listed_paths:
        _fail(ExitCode.MANIFEST_FORMAT, "manifest must not list itself")
    if len(set(listed_paths)) != len(listed_paths):
        _fail(ExitCode.MANIFEST_FORMAT, "duplicate artifact path")
    if listed_paths != sorted(listed_paths, key=lambda path: path.encode("utf-8")):
        _fail(ExitCode.MANIFEST_FORMAT, "artifacts are not sorted by UTF-8 path")

    actual_paths = _check_tree(root)
    _validate_layout(actual_paths)
    if set(listed_paths) != actual_paths - {"manifest.json"}:
        _fail(ExitCode.BUNDLE_LAYOUT, "manifest artifact list does not exactly match bundle files")
    for artifact in document["artifacts"]:
        path = root / artifact["path"]
        if path.stat().st_size != artifact["size"]:
            _fail(ExitCode.FILE_INTEGRITY, f"size mismatch: {artifact['path']}")
        if _sha256_file(path) != artifact["sha256"]:
            _fail(ExitCode.FILE_INTEGRITY, f"SHA-256 mismatch: {artifact['path']}")
        if artifact["path"].startswith("wheelhouse/"):
            _validate_wheel_binding(path, artifact)
    return VerifiedBundle(root, document)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    arguments = parser.parse_args(argv)
    try:
        verify_bundle(arguments.bundle_root, arguments.expected_manifest_sha256)
    except SupplyManifestError as error:
        print(f"SUPPLY_{error.code.name}: {error}", file=sys.stderr)
        return int(error.code)
    except OSError as error:
        print(f"SUPPLY_INTERNAL: {error}", file=sys.stderr)
        return int(ExitCode.INTERNAL)
    return int(ExitCode.OK)


if __name__ == "__main__":
    raise SystemExit(main())
