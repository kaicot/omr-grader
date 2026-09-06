"""Strict format-2 portable-release receipt creation and verification.

The receipt deliberately does not contain an archive hash: an archive contains the
receipt, so doing so would create a hash cycle.  The adjacent ``.sha256`` file binds
the completed ZIP.  This module accepts only a small, Windows-safe path language
before it opens any receipt-declared file.
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, NoReturn


FORMAT = 2
PRODUCT = "OMR Grader"
PAYLOAD_ROOT = "OMR Grader"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_RE = re.compile(r"^[0-9a-f]{40}$")
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ReleaseVerificationError(RuntimeError):
    """The candidate cannot be considered a verified portable release."""


def _fail(message: str) -> NoReturn:
    raise ReleaseVerificationError(message)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def strict_json_bytes(value: bytes, *, label: str) -> dict[str, Any]:
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError as error:
        _fail(f"{label} is not UTF-8: {error}")
    try:
        parsed = json.loads(decoded, object_pairs_hook=_no_duplicates)
    except (json.JSONDecodeError, ReleaseVerificationError) as error:
        _fail(f"{label} is invalid JSON: {error}")
    if not isinstance(parsed, dict):
        _fail(f"{label} must be a JSON object")
    return parsed


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        _fail(f"{label} keys differ; expected {sorted(expected)}, got {sorted(actual)}")


def _safe_relative(path: object, *, label: str) -> PurePosixPath:
    if not isinstance(path, str) or not path:
        _fail(f"{label} must be a non-empty string")
    if "\\" in path or "\x00" in path or path.startswith("/") or path.startswith("//"):
        _fail(f"unsafe path syntax in {label}: {path!r}")
    if re.match(r"^[A-Za-z]:", path) or path.startswith("\\\\"):
        _fail(f"drive or UNC path in {label}: {path!r}")
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or not candidate.parts:
        _fail(f"unsafe path in {label}: {path!r}")
    for part in candidate.parts:
        stem = part.split(".", 1)[0].upper()
        if (
            part in {"", ".", ".."}
            or ":" in part
            or any(ord(character) < 32 or character in '<>"|?*' for character in part)
            or part.endswith((".", " "))
            or stem in WINDOWS_RESERVED
        ):
            _fail(f"unsafe Windows path component in {label}: {path!r}")
    if candidate.as_posix() != path:
        _fail(f"non-canonical path in {label}: {path!r}")
    return candidate


def _unique_paths(paths: Iterable[str], *, label: str) -> None:
    exact: set[str] = set()
    aliases: set[str] = set()
    for path in paths:
        if path in exact:
            _fail(f"duplicate {label} path: {path!r}")
        exact.add(path)
        alias = path.casefold()
        if alias in aliases:
            _fail(f"case-colliding {label} path: {path!r}")
        aliases.add(alias)


def _is_symlink_or_reparse(path: Path) -> bool:
    # ``is_symlink`` covers the normal case.  File attributes catches Windows
    # junctions, which Path.is_symlink does not reliably identify.
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return path.is_symlink()
    return path.is_symlink() or bool(attributes & 0x400)


def _safe_child(root: Path, relative: str, *, label: str) -> Path:
    safe = _safe_relative(relative, label=label)
    child = root.joinpath(*safe.parts)
    # Do not resolve first: resolving follows exactly the link we are rejecting.
    current = root
    if _is_symlink_or_reparse(current):
        _fail(f"{label} root is a symlink/reparse point")
    for part in safe.parts:
        current = current / part
        if current.exists() and _is_symlink_or_reparse(current):
            _fail(f"{label} traverses a symlink/reparse point: {relative!r}")
    return child


def _record(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    _exact_keys(value, {"path", "size", "sha256"}, label)
    path = _safe_relative(value["path"], label=f"{label}.path").as_posix()
    if not isinstance(value["size"], int) or isinstance(value["size"], bool) or value["size"] < 0:
        _fail(f"{label}.size must be a non-negative integer")
    if not isinstance(value["sha256"], str) or not SHA256_RE.fullmatch(value["sha256"]):
        _fail(f"{label}.sha256 must be lower-case SHA-256")
    return {"path": path, "size": value["size"], "sha256": value["sha256"]}


def _input_record(value: object, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    _exact_keys(value, {"path", "sha256"}, label)
    path = _safe_relative(value["path"], label=f"{label}.path").as_posix()
    digest = value["sha256"]
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        _fail(f"{label}.sha256 must be lower-case SHA-256")
    return {"path": path, "sha256": digest}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode:
        _fail(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def release_input_paths(repository: Path) -> list[str]:
    """Return the complete source/build input set, excluding only generated evidence."""
    tracked = _git(repository, "ls-files", "-z").split("\0")
    included: list[str] = []
    for path in tracked:
        if not path:
            continue
        # Documentation is not packaged code.  The exclusion is deliberately
        # narrow and is mirrored by the clean-tree guard below.
        if path.startswith(("docs/", "artifacts/", "build/", "dist/")):
            continue
        if path.startswith("src/") or path in {"main.py", "pyproject.toml"} or path.startswith(
            ("packaging/", "tools/", "requirements/", "constraints/")
        ):
            included.append(_safe_relative(path, label="git input path").as_posix())
    required = {
        "main.py",
        "pyproject.toml",
        "packaging/OMR_Grader.spec",
        "tools/build-portable-folder.ps1",
        "tools/verify-portable-folder.ps1",
        "tools/smoke-portable-onedir.py",
        "tools/portable_release.py",
        "requirements/direct-pins.txt",
        "constraints/windows-py312.lock",
    }
    missing = required - set(included)
    if missing:
        _fail(f"required release inputs are not tracked: {sorted(missing)}")
    return sorted(included)


def assert_clean_release_source(repository: Path) -> None:
    status = _git(repository, "status", "--porcelain=v1", "--untracked-files=all")
    unsafe: list[str] = []
    for line in status.splitlines():
        path = line[3:]
        if path.startswith(("build/", "dist/", "artifacts/")):
            continue
        unsafe.append(line)
    if unsafe:
        _fail("release source is not clean: " + "; ".join(unsafe[:8]))


def snapshot_inputs(repository: Path) -> dict[str, Any]:
    assert_clean_release_source(repository)
    head = _git(repository, "rev-parse", "HEAD")
    if not GIT_RE.fullmatch(head):
        _fail("Git HEAD is not a full SHA-1")
    records = []
    for relative in release_input_paths(repository):
        source = _safe_child(repository, relative, label="build input")
        if not source.is_file():
            _fail(f"build input is missing or not a file: {relative}")
        records.append({"path": relative, "sha256": sha256_file(source)})
    return {"git_head": head, "inputs": records}


def write_snapshot(repository: Path, output: Path) -> dict[str, Any]:
    snapshot = snapshot_inputs(repository)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
    return snapshot


def _load_snapshot(path: Path) -> dict[str, Any]:
    value = strict_json_bytes(path.read_bytes(), label="build input snapshot")
    _exact_keys(value, {"git_head", "inputs"}, "build input snapshot")
    if not isinstance(value["git_head"], str) or not GIT_RE.fullmatch(value["git_head"]):
        _fail("build input snapshot has invalid git_head")
    if not isinstance(value["inputs"], list) or not value["inputs"]:
        _fail("build input snapshot has no inputs")
    inputs = [_input_record(item, label=f"build input snapshot.inputs[{index}]") for index, item in enumerate(value["inputs"])]
    _unique_paths([item["path"] for item in inputs], label="build input")
    return {"git_head": value["git_head"], "inputs": inputs}


def _tool_version(python: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [str(python), *arguments], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if completed.returncode:
        _fail(f"tool version command failed: {' '.join(arguments)}")
    value = completed.stdout.strip() or completed.stderr.strip()
    if not value:
        _fail(f"tool version command gave no output: {' '.join(arguments)}")
    return value


def create_receipt(
    release: Path, repository: Path, before_snapshot: Path, python: Path
) -> dict[str, Any]:
    release = release.resolve()
    repository = repository.resolve()
    before = _load_snapshot(before_snapshot)
    after = snapshot_inputs(repository)
    if before != after:
        _fail("build inputs changed between pre-build and post-build snapshots")
    payload = release / PAYLOAD_ROOT
    if not payload.is_dir() or _is_symlink_or_reparse(payload):
        _fail(f"payload root is missing or unsafe: {payload}")
    files: list[dict[str, Any]] = []
    for candidate in sorted(payload.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if candidate.is_dir():
            if _is_symlink_or_reparse(candidate):
                _fail(f"payload contains a symlink/reparse directory: {candidate}")
            continue
        if not candidate.is_file() or _is_symlink_or_reparse(candidate):
            _fail(f"payload contains a non-regular file: {candidate}")
        relative = candidate.relative_to(payload).as_posix()
        _safe_relative(relative, label="payload path")
        files.append({"path": relative, "size": candidate.stat().st_size, "sha256": sha256_file(candidate)})
    _unique_paths([item["path"] for item in files], label="payload")
    executable_path = "OMR Grader.exe"
    executable = next((item for item in files if item["path"] == executable_path), None)
    if executable is None:
        _fail("payload has no OMR Grader.exe")
    pyproject = (repository / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"(?P<version>[^"]+)"\s*$', pyproject)
    if not match:
        _fail("could not read the project version")
    receipt = {
        "format": FORMAT,
        "product": PRODUCT,
        "version": match.group("version"),
        "git_head": after["git_head"],
        "payload_root": PAYLOAD_ROOT,
        "executable": executable,
        "payload_files": files,
        "build_inputs": after["inputs"],
        "tools": {"python": _tool_version(python, "--version"), "pyinstaller": _tool_version(python, "-m", "PyInstaller", "--version")},
    }
    (release / "release-receipt.json").write_bytes(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    )
    return receipt


@dataclass(frozen=True)
class Verification:
    status: str
    format: int
    payload_files: int
    archive_sha256: str | None
    global_approval: bool
    details: tuple[str, ...] = ()


def _parse_receipt(receipt_bytes: bytes) -> dict[str, Any]:
    receipt = strict_json_bytes(receipt_bytes, label="release receipt")
    if receipt.get("format") == 1:
        return receipt
    _exact_keys(
        receipt,
        {"format", "product", "version", "git_head", "payload_root", "executable", "payload_files", "build_inputs", "tools"},
        "release receipt",
    )
    if receipt["format"] != FORMAT or receipt["product"] != PRODUCT or receipt["payload_root"] != PAYLOAD_ROOT:
        _fail("release receipt identity is invalid")
    if not isinstance(receipt["version"], str) or not receipt["version"]:
        _fail("release receipt version is invalid")
    if not isinstance(receipt["git_head"], str) or not GIT_RE.fullmatch(receipt["git_head"]):
        _fail("release receipt git_head is invalid")
    if not isinstance(receipt["payload_files"], list) or not receipt["payload_files"]:
        _fail("release receipt has no payload files")
    payloads = [_record(item, label=f"payload_files[{index}]") for index, item in enumerate(receipt["payload_files"])]
    _unique_paths([item["path"] for item in payloads], label="payload")
    receipt["payload_files"] = payloads
    receipt["executable"] = _record(receipt["executable"], label="executable")
    if receipt["executable"]["path"] != "OMR Grader.exe":
        _fail("release receipt executable path is invalid")
    if receipt["executable"] not in payloads:
        _fail("release receipt executable is not a matching payload record")
    if not isinstance(receipt["build_inputs"], list) or not receipt["build_inputs"]:
        _fail("release receipt has no build inputs")
    inputs = [_input_record(item, label=f"build_inputs[{index}]") for index, item in enumerate(receipt["build_inputs"])]
    _unique_paths([item["path"] for item in inputs], label="build input")
    receipt["build_inputs"] = inputs
    if not isinstance(receipt["tools"], dict):
        _fail("release receipt tools is invalid")
    _exact_keys(receipt["tools"], {"python", "pyinstaller"}, "tools")
    if not all(isinstance(value, str) and value for value in receipt["tools"].values()):
        _fail("release receipt tool versions are invalid")
    return receipt


def _verify_file_records(root: Path, records: list[dict[str, Any]]) -> None:
    actual: list[str] = []
    for candidate in root.rglob("*"):
        if candidate.is_dir():
            if _is_symlink_or_reparse(candidate):
                _fail(f"payload contains a symlink/reparse directory: {candidate}")
            continue
        if not candidate.is_file() or _is_symlink_or_reparse(candidate):
            _fail(f"payload contains a non-regular file: {candidate}")
        actual.append(candidate.relative_to(root).as_posix())
    _unique_paths(actual, label="actual payload")
    expected = [record["path"] for record in records]
    if set(actual) != set(expected):
        _fail(f"payload inventory differs: expected={sorted(expected)}, actual={sorted(actual)}")
    for record in records:
        candidate = _safe_child(root, record["path"], label="payload")
        if not candidate.is_file():
            _fail(f"payload file is missing: {record['path']}")
        if candidate.stat().st_size != record["size"] or sha256_file(candidate) != record["sha256"]:
            _fail(f"payload bytes differ: {record['path']}")


def _archive_names(archive: zipfile.ZipFile) -> list[str]:
    names: list[str] = []
    aliases: set[str] = set()
    for info in archive.infolist():
        name = info.filename
        directory = info.is_dir()
        candidate = name[:-1] if directory and name.endswith("/") else name
        if directory and (not candidate or name.count("/") != candidate.count("/") + 1):
            _fail(f"unsafe ZIP directory entry: {name!r}")
        _safe_relative(candidate, label="ZIP entry")
        unix_type = (info.external_attr >> 16) & 0o170000
        if unix_type == 0o120000:
            _fail(f"ZIP entry is a symlink: {name!r}")
        alias = candidate.casefold()
        if alias in aliases:
            _fail(f"duplicate/case-colliding ZIP entry: {name!r}")
        aliases.add(alias)
        if not directory:
            names.append(name)
    return names


def _verify_archive(archive_path: Path, release: Path, receipt_bytes: bytes, receipt: dict[str, Any]) -> str:
    sidecar = Path(f"{archive_path}.sha256")
    if not sidecar.is_file():
        _fail("archive SHA-256 sidecar is missing")
    archive_hash = sha256_file(archive_path)
    tokens = sidecar.read_text(encoding="ascii").strip().split()
    if len(tokens) != 2 or tokens[0] != archive_hash or tokens[1] != archive_path.name:
        _fail("archive SHA-256 sidecar is invalid")
    prefix = _safe_relative(release.name, label="release archive prefix").as_posix()
    expected = {f"{prefix}/release-receipt.json"}
    expected.update(f"{prefix}/{PAYLOAD_ROOT}/{record['path']}" for record in receipt["payload_files"])
    with zipfile.ZipFile(archive_path) as archive:
        names = _archive_names(archive)
        if set(names) != expected:
            _fail("ZIP inventory differs from release receipt")
        inside_receipt = archive.read(f"{prefix}/release-receipt.json")
        if inside_receipt != receipt_bytes:
            _fail("ZIP receipt bytes differ from external receipt")
        for record in receipt["payload_files"]:
            name = f"{prefix}/{PAYLOAD_ROOT}/{record['path']}"
            info = archive.getinfo(name)
            data = archive.read(info)
            if (
                info.file_size != record["size"]
                or (info.CRC & 0xFFFFFFFF) != (binascii.crc32(data) & 0xFFFFFFFF)
                or sha256_bytes(data) != record["sha256"]
            ):
                _fail(f"ZIP payload bytes differ: {record['path']}")
    return archive_hash


def verify_release(release: Path, archive_path: Path, repository: Path | None = None) -> Verification:
    # Check raw caller paths first; resolve() would silently follow the very
    # junction/symlink whose use the verifier must reject.
    if _is_symlink_or_reparse(release):
        _fail("release root is a symlink/reparse point")
    if _is_symlink_or_reparse(archive_path):
        _fail("archive is a symlink/reparse point")
    release = release.absolute()
    allowed_root = {PAYLOAD_ROOT, "release-receipt.json"}
    root_names = [item.name for item in release.iterdir()]
    if set(root_names) != allowed_root or len(root_names) != len(allowed_root):
        _fail(f"release root inventory is invalid: {sorted(root_names)}")
    receipt_path = release / "release-receipt.json"
    payload_path = release / PAYLOAD_ROOT
    if _is_symlink_or_reparse(receipt_path) or _is_symlink_or_reparse(payload_path):
        _fail("release receipt or payload root is a symlink/reparse point")
    receipt_bytes = receipt_path.read_bytes()
    receipt = _parse_receipt(receipt_bytes)
    if receipt.get("format") == 1:
        return Verification(
            status="LEGACY_AUDIT_ONLY",
            format=1,
            payload_files=0,
            archive_sha256=None,
            global_approval=False,
            details=("format 1 provenance and ZIP binding are unavailable; not rewritten",),
        )
    _verify_file_records(release / PAYLOAD_ROOT, receipt["payload_files"])
    if repository is not None:
        repository = repository.resolve()
        snapshot = snapshot_inputs(repository)
        if snapshot["git_head"] != receipt["git_head"] or snapshot["inputs"] != receipt["build_inputs"]:
            _fail("source/build input provenance differs from receipt")
    sidecar = Path(f"{archive_path}.sha256")
    if _is_symlink_or_reparse(sidecar):
        _fail("archive sidecar is a symlink/reparse point")
    archive_hash = _verify_archive(archive_path.absolute(), release, receipt_bytes, receipt)
    return Verification(
        status="STRUCTURE_PASS",
        format=FORMAT,
        payload_files=len(receipt["payload_files"]),
        archive_sha256=archive_hash,
        global_approval=False,
    )


def _main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = commands.add_parser("snapshot-inputs")
    snapshot_parser.add_argument("--repository", required=True, type=Path)
    snapshot_parser.add_argument("--output", required=True, type=Path)
    create_parser = commands.add_parser("create-receipt")
    create_parser.add_argument("--release", required=True, type=Path)
    create_parser.add_argument("--repository", required=True, type=Path)
    create_parser.add_argument("--before-snapshot", required=True, type=Path)
    create_parser.add_argument("--python", required=True, type=Path)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--release", required=True, type=Path)
    verify_parser.add_argument("--archive", required=True, type=Path)
    verify_parser.add_argument("--repository", type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.command == "snapshot-inputs":
            value = write_snapshot(arguments.repository, arguments.output)
        elif arguments.command == "create-receipt":
            value = create_receipt(
                arguments.release, arguments.repository, arguments.before_snapshot, arguments.python
            )
        else:
            value = verify_release(arguments.release, arguments.archive, arguments.repository).__dict__
    except ReleaseVerificationError as error:
        print(f"PORTABLE_RELEASE_VERIFY_FAILED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
