"""Build the first-party wheel twice without network access and attest identical bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from supply_manifest import SupplyManifestError, verify_bundle
from validate_canonical_wheel import (
    WheelValidationError,
    normalize_wheel,
    sha256_file,
    validate_wheel,
)

EXCLUDED_TOP_LEVEL = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    ".gjc",
    ".tox",
    "build",
    "dist",
    "artifacts",
    "coverage",
    "tmp",
    "temp",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
EXCLUDED_NAMES = {"__pycache__", ".DS_Store", "Thumbs.db", ".coverage"}
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def _excluded(relative: str, child: str) -> bool:
    return (
        relative.split("/", 1)[0] in EXCLUDED_TOP_LEVEL
        or child in EXCLUDED_NAMES
        or child.endswith((".pyc", ".pyo", ".tmp"))
    )


class SourceTreeError(ValueError):
    pass


def _is_link_or_reparse(path: Path, mode: int) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(mode) or bool(attributes & reparse)


def _relative_name(root: Path, path: Path) -> str:
    name = path.relative_to(root).as_posix()
    if not name or "\\" in name or "\x00" in name or unicodedata.normalize("NFC", name) != name:
        raise SourceTreeError(f"non-canonical source path: {path}")
    for part in name.split("/"):
        if (
            not part
            or part in {".", ".."}
            or part[-1] in {" ", "."}
            or part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES
            or any(character in '<>:"\\|?*' or ord(character) < 32 for character in part)
        ):
            raise SourceTreeError(f"unsafe source path: {path}")
    return name


def source_manifest(source: Path) -> list[tuple[str, str, int]]:
    """Return the source binding records, rejecting aliases before reading bytes."""
    source = Path(source).resolve(strict=True)
    if not source.is_dir():
        raise SourceTreeError(f"source is not a directory: {source}")
    records: list[tuple[str, str, int]] = []
    aliases: set[str] = set()
    for current, directories, files in os.walk(source, topdown=True, followlinks=False):
        directory = Path(current)
        kept: list[str] = []
        for child in directories:
            path = directory / child
            relative = _relative_name(source, path)
            if _excluded(relative, child):
                continue
            mode = path.lstat().st_mode
            if _is_link_or_reparse(path, mode):
                raise SourceTreeError(f"source link or reparse point: {path}")
            folded = relative.casefold()
            if folded in aliases:
                raise SourceTreeError(f"source path alias: {relative}")
            aliases.add(folded)
            kept.append(child)
        directories[:] = kept
        for child in files:
            path = directory / child
            relative = _relative_name(source, path)
            if _excluded(relative, child):
                continue
            mode = path.lstat().st_mode
            if _is_link_or_reparse(path, mode) or not stat.S_ISREG(mode):
                raise SourceTreeError(f"source must contain regular files only: {path}")
            if path.stat().st_nlink != 1:
                raise SourceTreeError(f"source hard-link alias: {path}")
            folded = relative.casefold()
            if folded in aliases:
                raise SourceTreeError(f"source path alias: {relative}")
            aliases.add(folded)
            records.append((relative, sha256_file(path), path.stat().st_size))
    return sorted(records)


def canonical_source_hash(source: Path) -> str:
    lines = [f"{name}\0{digest}\0{size}\n" for name, digest, size in source_manifest(source)]
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def _copy_source(source: Path, destination: Path, records: list[tuple[str, str, int]]) -> None:
    for name, expected_digest, expected_size in records:
        original = source / Path(name)
        target = destination / Path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = original.stat()
        with original.open("rb") as input_stream, target.open("xb") as output_stream:
            digest = hashlib.sha256()
            size = 0
            for block in iter(lambda: input_stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
                output_stream.write(block)
            after = original.stat()
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or size != expected_size
            or digest.hexdigest() != expected_digest
            or target.stat().st_size != expected_size
            or sha256_file(target) != expected_digest
        ):
            raise SourceTreeError(f"source changed or snapshot mismatch: {name}")


def pip_wheel_command(python: Path, source: Path, wheel_dir: Path) -> list[str]:
    return [
        str(python),
        "-m",
        "pip",
        "wheel",
        "--no-deps",
        "--no-build-isolation",
        "--no-index",
        "--wheel-dir",
        str(wheel_dir),
        str(source),
    ]


def _network_trap(directory: Path) -> None:
    (directory / "sitecustomize.py").write_text(
        "import socket\n"
        "def _blocked(*args, **kwargs): "
        "raise RuntimeError('network access is forbidden during wheel build')\n"
        "socket.create_connection = _blocked\n"
        "socket.socket.connect = _blocked\n"
        "socket.socket.connect_ex = _blocked\n",
        encoding="utf-8",
        newline="\n",
    )


def _build_once(
    python: Path, source: Path, source_records: list[tuple[str, str, int]], epoch: int, work: Path
) -> Path:
    snapshot = work / "source"
    _copy_source(source, snapshot, source_records)
    wheel_dir = work / "wheel"
    wheel_dir.mkdir()
    trap = work / "network-trap"
    trap.mkdir()
    _network_trap(trap)
    environment = os.environ.copy()
    environment.update(
        {
            "SOURCE_DATE_EPOCH": str(epoch),
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(trap),
        }
    )
    subprocess.run(
        pip_wheel_command(python, snapshot, wheel_dir), cwd=snapshot, env=environment, check=True
    )
    wheels = list(wheel_dir.glob("omr_grader-*-py3-none-any.whl"))
    if len(wheels) != 1 or len(list(wheel_dir.iterdir())) != 1:
        raise WheelValidationError("pip must produce exactly one first-party pure wheel")
    normalize_wheel(wheels[0], epoch)
    validate_wheel(wheels[0], epoch)
    return wheels[0]


def _tool_hashes(python: Path) -> dict[str, Any]:
    status = python.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or _is_link_or_reparse(python, status)
        or status.st_nlink != 1
    ):
        raise WheelValidationError("approved Python executable is not one unaliased regular file")
    code = (
        "import hashlib,importlib.metadata,json,pathlib,sys\n"
        "def h(p):\n d=hashlib.sha256();\n"
        " with p.open('rb') as f:\n  for b in iter(lambda:f.read(1048576),b''):d.update(b)\n"
        " return d.hexdigest()\n"
        "out={}\n"
        "for name in ('pip','setuptools','wheel'):\n"
        " d=importlib.metadata.distribution(name); root=pathlib.Path(d._path);"
        " record=root/'RECORD'\n"
        " if not record.is_file(): raise RuntimeError('missing RECORD for '+name)\n"
        " files=[]\n"
        " for x,_,_ in __import__('csv').reader(record.read_text(encoding='utf-8').splitlines()):\n"
        "  q=(root.parent/x).resolve()\n"
        "  if not x or not q.is_file() or"
        " not q.is_relative_to(pathlib.Path(sys.prefix).resolve()):"
        " raise RuntimeError('unsafe backend RECORD')\n"
        "  files.append({'path':str(q.resolve()),'sha256':h(q)})\n"
        " out[name]={'version':d.version,'metadata_path':str(root.resolve()),"
        "'record_sha256':h(record),'files':sorted(files,key=lambda x:x['path'])}\n"
        "print(json.dumps({'executable':str(pathlib.Path(sys.executable).resolve()),'distributions':out},sort_keys=True))"
    )
    try:
        result = json.loads(subprocess.check_output([str(python), "-I", "-c", code], text=True))
    except (subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise WheelValidationError(
            f"cannot bind approved build backend distributions: {error}"
        ) from error
    distributions = result.get("distributions") if isinstance(result, dict) else None
    if not isinstance(distributions, dict) or set(distributions) != {"pip", "setuptools", "wheel"}:
        raise WheelValidationError("build backend distribution evidence is incomplete")
    return {
        "python_sha256": sha256_file(python),
        "python_path": str(python),
        "reported_executable": result.get("executable"),
        "distributions": distributions,
    }


def _directory_hash(root: Path) -> str:
    root = Path(root).resolve(strict=True)
    rows: list[str] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current)
        directories.sort()
        files.sort()
        for child in [*directories, *files]:
            path = directory / child
            mode = path.lstat().st_mode
            if _is_link_or_reparse(path, mode) or (path.is_file() and path.stat().st_nlink != 1):
                raise SourceTreeError(f"bundle link or alias: {path}")
        for child in files:
            path = directory / child
            if not path.is_file():
                raise SourceTreeError(f"bundle non-regular file: {path}")
            rows.append(
                f"{_relative_name(root, path)}\0{sha256_file(path)}\0{path.stat().st_size}\n"
            )
    return hashlib.sha256("".join(sorted(rows)).encode("utf-8")).hexdigest()


def _canonical_json(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")


def _sandbox_evidence(
    path: Path, expected_sha256: str, run_id: str, toolchain: dict[str, Any], invocation_sha256: str
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise WheelValidationError("--expected-sandbox-evidence-sha256 must be lowercase SHA-256")
    if not re.fullmatch(r"[0-9a-f]{64}", run_id):
        raise WheelValidationError("--sandbox-run-id must be an externally generated 256-bit nonce")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise WheelValidationError("sandbox evidence does not match its external digest")
    try:
        evidence = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WheelValidationError("sandbox evidence is not UTF-8 JSON") from error
    if not isinstance(evidence, dict) or raw != _canonical_json(evidence):
        raise WheelValidationError("sandbox evidence is not canonical JSON")
    required = {
        "schema_version",
        "platform",
        "policy",
        "run_id",
        "tool_sha256",
        "invocation_sha256",
        "process_id",
        "process_start_time",
        "session_id",
        "nonce_sha256",
        "active_negative_egress_control",
        "approved_toolchain_records",
    }
    if (
        set(evidence) != required
        or evidence["schema_version"] != 2
        or evidence["platform"] != "windows"
        or evidence["policy"] != "offline-egress-deny"
        or evidence["run_id"] != run_id
        or evidence["tool_sha256"] != sha256_file(Path(__file__).resolve())
        or evidence["invocation_sha256"] != invocation_sha256
        or evidence["active_negative_egress_control"] is not True
        or evidence["approved_toolchain_records"] != toolchain
        or not isinstance(evidence["process_id"], int)
        or evidence["process_id"] <= 0
        or not isinstance(evidence["session_id"], str)
        or not evidence["session_id"]
        or not isinstance(evidence["process_start_time"], str)
        or not evidence["process_start_time"]
        or not isinstance(evidence["nonce_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", evidence["nonce_sha256"])
    ):
        raise WheelValidationError(
            "sandbox evidence must be human-provided v2 OS session evidence "
            "bound to this exact invocation"
        )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--python", type=Path, required=True, help="Previously attested Python executable"
    )
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument(
        "--sandbox-evidence",
        type=Path,
        required=True,
        help="Externally attested canonical OS sandbox evidence for offline-egress-deny.",
    )
    parser.add_argument("--expected-sandbox-evidence-sha256", required=True)
    parser.add_argument("--sandbox-run-id", required=True)
    args = parser.parse_args()
    python = args.python.resolve(strict=True)
    source = args.source.resolve(strict=True)
    output = args.output.resolve()
    if output == source or source in output.parents:
        raise SourceTreeError("output must be outside the source tree")
    output.mkdir(parents=True, exist_ok=True)
    try:
        verified_bundle = verify_bundle(args.bundle, args.expected_manifest_sha256)
    except SupplyManifestError as error:
        raise WheelValidationError(f"approved supply bundle rejected: {error}") from error
    source_records = source_manifest(source)
    source_hash = hashlib.sha256(
        "".join(f"{name}\0{digest}\0{size}\n" for name, digest, size in source_records).encode(
            "utf-8"
        )
    ).hexdigest()
    toolchain = _tool_hashes(python)
    invocation_sha256 = hashlib.sha256(
        _canonical_json(
            {
                "bundle_tree_sha256": _directory_hash(verified_bundle.bundle_root),
                "manifest_sha256": args.expected_manifest_sha256,
                "python_sha256": toolchain["python_sha256"],
                "source_date_epoch": args.source_date_epoch,
                "source_tree_sha256": source_hash,
                "tool_sha256": sha256_file(Path(__file__).resolve()),
            }
        )
    ).hexdigest()
    sandbox = _sandbox_evidence(
        args.sandbox_evidence,
        args.expected_sandbox_evidence_sha256,
        args.sandbox_run_id,
        toolchain,
        invocation_sha256,
    )
    with (
        tempfile.TemporaryDirectory(prefix="omr-wheel-a-", dir=output) as first_dir,
        tempfile.TemporaryDirectory(prefix="omr-wheel-b-", dir=output) as second_dir,
    ):
        first = _build_once(python, source, source_records, args.source_date_epoch, Path(first_dir))
        second = _build_once(
            python, source, source_records, args.source_date_epoch, Path(second_dir)
        )
        if sha256_file(first) != sha256_file(second) or first.read_bytes() != second.read_bytes():
            raise WheelValidationError("two clean wheel builds are not byte-identical")
        version = first.name.removeprefix("omr_grader-").removesuffix("-py3-none-any.whl")
        final_wheel = output / first.name
        shutil.copyfile(first, final_wheel)
    if canonical_source_hash(source) != source_hash:
        raise SourceTreeError("source tree changed during wheel build")
    wheel_hash = validate_wheel(final_wheel, args.source_date_epoch, version)
    attestation: dict[str, object] = {
        "schema_version": 1,
        "source_date_epoch": args.source_date_epoch,
        "source_tree_sha256": source_hash,
        "toolchain": toolchain,
        "approved_manifest_sha256": args.expected_manifest_sha256,
        "approved_bundle_tree_sha256": _directory_hash(verified_bundle.bundle_root),
        "sandbox_evidence_sha256": args.expected_sandbox_evidence_sha256,
        "sandbox_run_id": sandbox["run_id"],
        "sandbox_invocation_sha256": invocation_sha256,
        "wheel": {"path": final_wheel.name, "sha256": wheel_hash, "version": version},
    }
    (output / "build-attestation.json").write_bytes(_canonical_json(attestation))
    print(wheel_hash)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
