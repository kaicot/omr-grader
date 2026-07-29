"""Independent offline verification for an OMR Grader portable release."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from setuptools._vendor.packaging.markers import default_environment
from setuptools._vendor.packaging.requirements import InvalidRequirement, Requirement

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXE_NAME = "OMR Grader.exe"
RELEASE_PAYLOAD = frozenset({EXE_NAME, "release-receipt.json"})
MANIFEST_FIELDS = frozenset({"version", "wheels"})
WHEEL_FIELDS = frozenset(
    {"filename", "size", "sha256", "license", "provenance", "acquisition_record_id"}
)
RECEIPT_FIELDS = frozenset(
    {
        "format",
        "timestamp_utc",
        "executable",
        "source_hashes",
        "config_hashes",
        "builder_sources",
        "wheel_manifest",
        "environment_inventory",
        "tool_versions",
        "warnings",
    }
)
ARTIFACT_MANIFEST_FIELDS = frozenset({"format", "artifacts"})
ARTIFACT_FIELDS = frozenset({"filename", "size", "sha256"})
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ACQUISITION_RECORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}\Z")
CONFIG_HASH_PATHS = frozenset(
    {
        "packaging/OMR_Grader.spec",
        "packaging/build_release.py",
        "packaging/verify_release.py",
        "pyproject.toml",
    }
)
BUILDER_SOURCE_PATHS = frozenset({"packaging/build_release.py", "packaging/verify_release.py"})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _substantive_text(value: object) -> bool:
    return isinstance(value, str) and len(value.strip()) >= 3


def load_manifest(manifest_path: Path, wheelhouse: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid wheel manifest: {error}") from error
    if (
        not isinstance(manifest, dict)
        or set(manifest) != MANIFEST_FIELDS
        or manifest.get("version") != 1
    ):
        raise ValueError("Wheel manifest must contain exactly version 1 and wheels.")
    if not isinstance(manifest["wheels"], list) or not manifest["wheels"]:
        raise ValueError("Wheel manifest wheels must be a non-empty list.")
    declared: set[str] = set()
    for item in manifest["wheels"]:
        if not isinstance(item, dict) or set(item) != WHEEL_FIELDS:
            raise ValueError("Every wheel must use the exact required manifest schema.")
        filename = item["filename"]
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith(".whl")
        ):
            raise ValueError(f"Unsafe wheel filename: {filename!r}")
        if filename in declared:
            raise ValueError(f"Duplicate wheel manifest entry: {filename}")
        declared.add(filename)
        if not isinstance(item["size"], int) or item["size"] < 1:
            raise ValueError(f"Invalid wheel size for {filename}")
        if not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(item["sha256"]):
            raise ValueError(f"Invalid normalized SHA256 for {filename}")
        if not _substantive_text(item["license"]) or not _substantive_text(item["provenance"]):
            raise ValueError(f"Missing substantive license or provenance for {filename}")
        if not isinstance(
            item["acquisition_record_id"], str
        ) or not ACQUISITION_RECORD_RE.fullmatch(item["acquisition_record_id"]):
            raise ValueError(f"Invalid acquisition record identifier for {filename}")
        wheel = wheelhouse / filename
        if not wheel.is_file():
            raise ValueError(f"Manifest wheel is missing: {filename}")
        if wheel.stat().st_size != item["size"] or sha256_file(wheel) != item["sha256"]:
            raise ValueError(f"Manifest wheel integrity mismatch: {filename}")
    if {path.name for path in wheelhouse.glob("*.whl")} != declared:
        raise ValueError("Wheelhouse filenames do not exactly match the manifest.")
    return manifest


def _windows_marker_environment() -> dict[str, str]:
    if sys.implementation.name != "cpython":
        raise ValueError("The Windows release runtime must use CPython.")
    if sys.version_info[:2] != (3, 12):
        raise ValueError("The Windows release runtime must use Python 3.12.")
    if platform.machine().upper() != "AMD64" or sys.maxsize <= 2**32:
        raise ValueError("The Windows release runtime must use 64-bit AMD64.")
    environment = default_environment()
    environment.update(
        {
            "os_name": "nt",
            "sys_platform": "win32",
            "platform_system": "Windows",
            "platform_machine": "AMD64",
            "python_version": ".".join(map(str, sys.version_info[:2])),
            "python_full_version": ".".join(map(str, sys.version_info[:3])),
            "implementation_version": ".".join(map(str, sys.version_info[:3])),
            "extra": "",
        }
    )
    return environment


def _parse_requirement(value: str) -> Requirement:
    try:
        return Requirement(value)
    except InvalidRequirement as error:
        raise ValueError(f"Invalid Requires-Dist: {value!r}") from error


def _wheel_metadata(wheel: Path) -> tuple[str, str, list[Requirement]]:
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError("expected one .dist-info/METADATA file")
            metadata = BytesParser().parsebytes(archive.read(names[0]))
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as error:
        raise ValueError(f"Invalid wheel metadata in {wheel.name}: {error}") from error
    name, version = metadata.get("Name"), metadata.get("Version")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(version, str)
        or not version.strip()
    ):
        raise ValueError(f"Wheel metadata lacks Name or Version: {wheel.name}")
    requirements = [_parse_requirement(value) for value in metadata.get_all("Requires-Dist", [])]
    if any(requirement.url is not None for requirement in requirements):
        raise ValueError(
            f"Requires-Dist direct reference is not bound to the wheelhouse: {wheel.name}"
        )
    return normalize_distribution_name(name), version, requirements


def manifest_inventory(manifest: dict[str, Any], wheelhouse: Path) -> dict[str, str]:
    inventory: dict[str, str] = {}
    requirements: dict[str, list[Requirement]] = {}
    requested_extras: dict[str, set[str]] = {}
    for item in manifest["wheels"]:
        name, version, dependencies = _wheel_metadata(wheelhouse / item["filename"])
        if name in inventory:
            raise ValueError(f"Manifest contains multiple wheels for distribution: {name}")
        inventory[name] = version
        requirements[name] = dependencies
        requested_extras[name] = {""}

    unresolved: list[str] = []
    incompatible: list[str] = []
    processed: dict[str, set[str]] = {name: set() for name in inventory}
    while True:
        pending = [
            (name, extra)
            for name, extras in requested_extras.items()
            for extra in extras - processed[name]
        ]
        if not pending:
            break
        for name, extra in pending:
            processed[name].add(extra)
            environment = _windows_marker_environment()
            environment["extra"] = extra
            for dependency in requirements[name]:
                if dependency.marker is not None and not dependency.marker.evaluate(environment):
                    continue
                dependency_name = normalize_distribution_name(dependency.name)
                version = inventory.get(dependency_name)
                if version is None:
                    unresolved.append(f"{name}[{extra}] -> {dependency_name}")
                    continue
                requested_extras[dependency_name].update(dependency.extras)
                if dependency.specifier and not dependency.specifier.contains(
                    version, prereleases=True
                ):
                    incompatible.append(
                        f"{name}[{extra}] -> {dependency_name}"
                        f"{dependency.specifier} (found {version})"
                    )
    if unresolved:
        raise ValueError(
            "Manifest has unresolved transitive dependencies: " + ", ".join(sorted(set(unresolved)))
        )
    if incompatible:
        raise ValueError(
            "Manifest has incompatible transitive dependencies: "
            + ", ".join(sorted(set(incompatible)))
        )
    return inventory


def source_hashes() -> dict[str, str]:
    paths = [
        PROJECT_ROOT / "main.py",
        PROJECT_ROOT / "pyproject.toml",
        PROJECT_ROOT / "packaging" / "OMR_Grader.spec",
    ]
    paths.extend(sorted((PROJECT_ROOT / "src").rglob("*.py")))
    return {
        str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"): sha256_file(path) for path in paths
    }


def load_receipt(release_dir: Path) -> dict[str, Any]:
    try:
        receipt = json.loads((release_dir / "release-receipt.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid release receipt: {error}") from error
    if (
        not isinstance(receipt, dict)
        or set(receipt) != RECEIPT_FIELDS
        or receipt.get("format") != 1
    ):
        raise ValueError("Release receipt is incomplete or has an unsupported format.")
    return receipt


def _hash_mapping(value: object, label: str, *, nonempty: bool = False) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or (nonempty and not value)
        or not all(
            isinstance(path, str) and isinstance(digest, str) and SHA256_RE.fullmatch(digest)
            for path, digest in value.items()
        )
    ):
        raise ValueError(f"Release receipt {label} is invalid.")
    return value


def _assert_exact_payload(release_dir: Path) -> None:
    actual = {path.name for path in release_dir.iterdir()}
    if actual != RELEASE_PAYLOAD:
        raise ValueError(
            "Release payload does not exactly match the declared contract: "
            f"expected={sorted(RELEASE_PAYLOAD)}, actual={sorted(actual)}"
        )


def _verify_artifact_manifest(release_dir: Path, path: Path) -> None:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid external artifact manifest: {error}") from error
    if (
        not isinstance(manifest, dict)
        or set(manifest) != ARTIFACT_MANIFEST_FIELDS
        or manifest.get("format") != 1
    ):
        raise ValueError("External artifact manifest has an unsupported schema.")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(RELEASE_PAYLOAD):
        raise ValueError("External artifact manifest must bind every release payload file.")
    declared: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != ARTIFACT_FIELDS:
            raise ValueError("External artifact manifest has an invalid artifact binding.")
        filename, size, digest = item["filename"], item["size"], item["sha256"]
        if not isinstance(filename, str) or filename in declared or filename not in RELEASE_PAYLOAD:
            raise ValueError("External artifact manifest has an invalid artifact filename.")
        if (
            not isinstance(size, int)
            or size < 1
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
        ):
            raise ValueError("External artifact manifest has an invalid artifact binding.")
        artifact = release_dir / filename
        if (
            not artifact.is_file()
            or artifact.stat().st_size != size
            or sha256_file(artifact) != digest
        ):
            raise ValueError(f"External artifact binding does not match: {filename}")
        declared.add(filename)
    if declared != RELEASE_PAYLOAD:
        raise ValueError("External artifact manifest must bind every release payload file.")


def verify_receipt_inventory(receipt_inventory: object, expected: dict[str, str]) -> None:
    if not isinstance(receipt_inventory, dict) or set(receipt_inventory) != {
        "prefix",
        "distributions",
    }:
        raise ValueError("Release receipt has no valid isolated environment inventory.")
    prefix, distributions = receipt_inventory["prefix"], receipt_inventory["distributions"]
    if not isinstance(prefix, str) or not prefix or not isinstance(distributions, list):
        raise ValueError("Release receipt has no valid isolated environment inventory.")
    installed: dict[str, str] = {}
    for item in distributions:
        if not isinstance(item, dict) or set(item) != {"name", "version"}:
            raise ValueError("Release receipt inventory has an invalid distribution.")
        name, version = item["name"], item["version"]
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError("Release receipt inventory has an invalid distribution.")
        normalized = normalize_distribution_name(name)
        if normalized in installed:
            raise ValueError(f"Release receipt inventory duplicates {normalized}.")
        installed[normalized] = version
    unexpected, missing = (
        sorted(set(installed) - (set(expected) | {"omr-grader", "pip"})),
        sorted(set(expected) - set(installed)),
    )
    wrong = sorted(name for name, version in expected.items() if installed.get(name) != version)
    if unexpected or missing or wrong or "omr-grader" not in installed:
        raise ValueError(
            "Release receipt inventory does not bind the declared dependency closure: "
            f"unexpected={unexpected}, missing={missing}, wrong_versions={wrong}"
        )


def verify_release(
    release_dir: Path,
    manifest_path: Path,
    wheelhouse: Path,
    *,
    artifact_manifest: Path | None = None,
) -> dict[str, Any]:
    release_dir = release_dir.resolve()
    _assert_exact_payload(release_dir)
    receipt = load_receipt(release_dir)
    manifest = load_manifest(manifest_path, wheelhouse)
    expected_inventory = manifest_inventory(manifest, wheelhouse)
    source_hashes_record = _hash_mapping(receipt["source_hashes"], "source hashes", nonempty=True)
    config_hashes = _hash_mapping(receipt["config_hashes"], "configuration hashes", nonempty=True)
    builder_sources = _hash_mapping(receipt["builder_sources"], "builder sources", nonempty=True)
    if set(source_hashes_record) != set(source_hashes()):
        raise ValueError("Release receipt source binding is invalid.")
    if set(config_hashes) != CONFIG_HASH_PATHS:
        raise ValueError("Release receipt configuration binding is invalid.")
    if set(builder_sources) != BUILDER_SOURCE_PATHS:
        raise ValueError("Release receipt builder source binding is invalid.")
    if (
        not isinstance(receipt["tool_versions"], dict)
        or not isinstance(receipt["warnings"], list)
        or not isinstance(receipt["timestamp_utc"], str)
        or not receipt["timestamp_utc"]
    ):
        raise ValueError("Release receipt tool, warning, or timestamp record is invalid.")
    recorded_manifest = receipt["wheel_manifest"]
    if not isinstance(recorded_manifest, dict) or set(recorded_manifest) != {
        "filename",
        "sha256",
        "wheels",
    }:
        raise ValueError("Release receipt wheel manifest binding is invalid.")
    if recorded_manifest["filename"] != manifest_path.name or recorded_manifest[
        "sha256"
    ] != sha256_file(manifest_path):
        raise ValueError("Wheel manifest hash does not match the release receipt.")
    if canonical_json(recorded_manifest["wheels"]) != canonical_json(manifest["wheels"]):
        raise ValueError("Wheel manifest entries do not match the release receipt.")
    verify_receipt_inventory(receipt["environment_inventory"], expected_inventory)
    executable = release_dir / EXE_NAME
    recorded_executable = receipt["executable"]
    if not isinstance(recorded_executable, dict) or set(recorded_executable) != {
        "filename",
        "sha256",
        "size",
    }:
        raise ValueError("Release executable receipt binding is invalid.")
    if recorded_executable["filename"] != EXE_NAME or not executable.is_file():
        raise ValueError(f"Release executable is missing: {EXE_NAME}")
    if executable.stat().st_size != recorded_executable["size"]:
        raise ValueError("Release executable size does not match the receipt.")
    if sha256_file(executable) != recorded_executable["sha256"]:
        raise ValueError("Release executable SHA256 does not match the receipt.")
    if artifact_manifest is not None:
        _verify_artifact_manifest(release_dir, artifact_manifest)
    else:
        current = source_hashes()
        if canonical_json(current) != canonical_json(source_hashes_record):
            raise ValueError("Current source hashes do not match the release receipt.")
        for relative, expected_hash in config_hashes.items():
            if sha256_file(PROJECT_ROOT / relative) != expected_hash:
                raise ValueError(f"Build configuration hash mismatch: {relative}")
        expected_sources = {
            "packaging/build_release.py": sha256_file(
                PROJECT_ROOT / "packaging" / "build_release.py"
            ),
            "packaging/verify_release.py": sha256_file(
                PROJECT_ROOT / "packaging" / "verify_release.py"
            ),
        }
        if canonical_json(builder_sources) != canonical_json(expected_sources):
            raise ValueError("Builder or verifier source hash does not match the release receipt.")
    return receipt


def _windows_dll(name: str) -> Any:
    return ctypes.WinDLL(name, use_last_error=True)


def _window_titles(process_ids: set[int]) -> list[str]:
    titles: list[str] = []
    callback_error: RuntimeError | None = None
    user32 = _windows_dll("user32")
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user32.EnumWindows.restype = ctypes.c_bool
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_bool

    def collect(window: int, _parameter: int) -> bool:
        nonlocal callback_error
        try:
            owner_pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(window, ctypes.byref(owner_pid))
            if owner_pid.value in process_ids and user32.IsWindowVisible(window):
                length = user32.GetWindowTextLengthW(window)
                if length:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(window, buffer, len(buffer))
                    titles.append(buffer.value)
        except Exception as error:
            callback_error = RuntimeError(f"Could not inspect job window: {error}")
            return False
        return True

    ctypes.set_last_error(0)
    if not user32.EnumWindows(callback_type(collect), 0):
        if callback_error is not None:
            raise callback_error
        raise RuntimeError(f"Could not enumerate windows: Windows error {ctypes.get_last_error()}")
    return titles


def _close_windows(process_ids: set[int]) -> None:
    callback_error: RuntimeError | None = None
    user32 = _windows_dll("user32")
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user32.EnumWindows.restype = ctypes.c_bool
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.PostMessageW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_size_t,
    ]
    user32.PostMessageW.restype = ctypes.c_bool

    def close(window: int, _parameter: int) -> bool:
        nonlocal callback_error
        try:
            owner_pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(window, ctypes.byref(owner_pid))
            if owner_pid.value in process_ids and not user32.PostMessageW(window, 0x0010, 0, 0):
                callback_error = RuntimeError(
                    f"Could not close job window for process {owner_pid.value}: "
                    f"Windows error {ctypes.get_last_error()}"
                )
                return False
        except Exception as error:
            callback_error = RuntimeError(f"Could not close job window: {error}")
            return False
        return True

    ctypes.set_last_error(0)
    if not user32.EnumWindows(callback_type(close), 0):
        if callback_error is not None:
            raise callback_error
        raise RuntimeError(
            f"Could not enumerate windows for close: Windows error {ctypes.get_last_error()}"
        )


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobObjectBasicProcessIdList(ctypes.Structure):
    _fields_ = [
        ("NumberOfAssignedProcesses", ctypes.c_ulong),
        ("NumberOfProcessIdsInList", ctypes.c_ulong),
        ("ProcessIdList", ctypes.c_size_t * 1),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ThreadID", ctypes.c_ulong),
        ("th32OwnerProcessID", ctypes.c_ulong),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
    ]


class _WindowsJob:
    """Own a smoke-test process tree until this object is closed."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        kernel32 = _windows_dll("kernel32")
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_bool
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_bool
        kernel32.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
        ]
        kernel32.QueryInformationJobObject.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise RuntimeError(
                f"Could not create process job: Windows error {ctypes.get_last_error()}"
            )
        self._kernel32 = kernel32
        self._handle: int | None = handle
        try:
            limits = _JobObjectExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = 0x00002000
            if not kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise RuntimeError(
                    f"Could not configure process job: Windows error {ctypes.get_last_error()}"
                )
            if not kernel32.AssignProcessToJobObject(handle, process._handle):
                raise RuntimeError(
                    f"Could not assign process {process.pid} to job: "
                    f"Windows error {ctypes.get_last_error()}"
                )
        except Exception:
            kernel32.CloseHandle(handle)
            self._handle = None
            raise

    def process_ids(self) -> set[int]:
        if self._handle is None:
            raise RuntimeError("Cannot enumerate a closed process job.")
        capacity = 16
        while True:
            size = ctypes.sizeof(_JobObjectBasicProcessIdList) + (capacity - 1) * ctypes.sizeof(
                ctypes.c_size_t
            )
            buffer = ctypes.create_string_buffer(size)
            process_list = ctypes.cast(
                buffer, ctypes.POINTER(_JobObjectBasicProcessIdList)
            ).contents
            ctypes.set_last_error(0)
            if self._kernel32.QueryInformationJobObject(self._handle, 3, buffer, size, None):
                count = process_list.NumberOfProcessIdsInList
                if count > capacity:
                    raise RuntimeError("Windows returned an invalid process job PID count.")
                pid_array = ctypes.cast(
                    ctypes.addressof(buffer) + _JobObjectBasicProcessIdList.ProcessIdList.offset,
                    ctypes.POINTER(ctypes.c_size_t * capacity),
                ).contents
                return {int(pid_array[index]) for index in range(count)}
            if ctypes.get_last_error() != 234:
                raise RuntimeError(
                    f"Could not enumerate process job: Windows error {ctypes.get_last_error()}"
                )
            capacity *= 2

    def close(self) -> None:
        if self._handle is not None:
            handle, self._handle = self._handle, None
            if not self._kernel32.CloseHandle(handle):
                raise RuntimeError(
                    f"Could not close process job: Windows error {ctypes.get_last_error()}"
                )


def _resume_suspended_process(process_id: int) -> None:
    kernel32 = _windows_dll("kernel32")
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Thread32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32First.restype = ctypes.c_bool
    kernel32.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32Next.restype = ctypes.c_bool
    kernel32.OpenThread.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel32.OpenThread.restype = ctypes.c_void_p
    kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
    kernel32.ResumeThread.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise RuntimeError(
            f"Could not snapshot suspended process: Windows error {ctypes.get_last_error()}"
        )
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            error = ctypes.get_last_error()
            raise RuntimeError(
                f"Could not enumerate suspended process threads: Windows error {error}"
            )
        while True:
            if entry.th32OwnerProcessID == process_id:
                thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                if not thread:
                    error = ctypes.get_last_error()
                    raise RuntimeError(
                        f"Could not open suspended process thread: Windows error {error}"
                    )
                try:
                    if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                        error = ctypes.get_last_error()
                        raise RuntimeError(
                            f"Could not resume suspended process: Windows error {error}"
                        )
                    return
                finally:
                    kernel32.CloseHandle(thread)
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                break
        raise RuntimeError("Could not find the suspended process primary thread.")
    finally:
        kernel32.CloseHandle(snapshot)


def _set_directory_write_denied(path: Path, denied: bool) -> None:
    identity = subprocess.run(["whoami"], text=True, capture_output=True, check=True).stdout.strip()
    command = [
        "icacls",
        str(path),
        "/remove:d" if not denied else "/deny",
        identity if not denied else f"{identity}:(W)",
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError(f"Could not set portable folder write permission: {completed.stderr}")


def smoke_portable(release_dir: Path, *, read_only: bool) -> None:
    if os.name != "nt":
        raise RuntimeError("Portable smoke testing requires Windows.")
    with tempfile.TemporaryDirectory(prefix="omr-grader-smoke-") as temporary:
        root = Path(temporary) / ("readonly" if read_only else "writable")
        root.mkdir()
        executable = root / EXE_NAME
        shutil.copy2(release_dir / EXE_NAME, executable)
        before = {item.name for item in root.iterdir()}
        if read_only:
            _set_directory_write_denied(root, True)
        process: subprocess.Popen[str] | None = None
        job: _WindowsJob | None = None
        primary_error: BaseException | None = None
        cleanup_errors: list[Exception] = []
        try:
            process = subprocess.Popen(
                [str(executable)],
                cwd=root,
                text=True,
                creationflags=getattr(subprocess, "CREATE_SUSPENDED", 0x00000004),
            )
            job = _WindowsJob(process)
            _resume_suspended_process(process.pid)
            deadline, titles = time.monotonic() + 30, []
            while time.monotonic() < deadline and process.poll() is None:
                process_ids = job.process_ids()
                if not process_ids:
                    raise RuntimeError("Process job unexpectedly contains no processes.")
                titles = _window_titles(process_ids)
                if "OMR Grader" in titles:
                    break
                time.sleep(0.1)
            if process.poll() is not None:
                raise RuntimeError(
                    f"Portable executable exited during startup ({process.returncode})."
                )
            if not titles:
                raise RuntimeError(
                    "Portable executable did not display a window within thirty seconds."
                )
            if "OMR Grader" not in titles:
                raise RuntimeError(
                    "Portable executable window title does not match the product name."
                )
            process_ids = job.process_ids()
            if not process_ids:
                raise RuntimeError("Process job unexpectedly contains no processes.")
            _close_windows(process_ids)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("Portable executable did not exit after WM_CLOSE.") from error
            if process.returncode:
                raise RuntimeError(
                    "Portable executable exited unsuccessfully after WM_CLOSE "
                    f"({process.returncode})."
                )
        except BaseException as error:
            primary_error = error
        finally:
            if job is not None:
                try:
                    job.close()
                except Exception as error:
                    cleanup_errors.append(error)
                if process is not None and process.poll() is None:
                    try:
                        process.wait(timeout=5)
                    except Exception as error:
                        cleanup_errors.append(error)
            elif process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception as error:
                    cleanup_errors.append(error)
            if read_only:
                try:
                    _set_directory_write_denied(root, False)
                except Exception as error:
                    cleanup_errors.append(error)
        if primary_error is not None:
            if cleanup_errors:
                raise RuntimeError(
                    f"{primary_error}; cleanup failed: "
                    + "; ".join(str(error) for error in cleanup_errors)
                ) from primary_error
            raise primary_error
        if cleanup_errors:
            raise RuntimeError(
                "Portable smoke-test cleanup failed: "
                + "; ".join(str(error) for error in cleanup_errors)
            )
        unexpected = (
            {item.name for item in root.iterdir()}
            - before
            - {"config.json", "Profiles", "OMR_Grader"}
        )
        if unexpected:
            raise RuntimeError(
                f"Portable executable wrote unexpected root entries: {sorted(unexpected)}"
            )
        if read_only and ({item.name for item in root.iterdir()} - before):
            raise RuntimeError("Read-only portable folder was modified.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify an OMR Grader release without network access."
    )
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument(
        "--artifact-manifest",
        type=Path,
        help="External trust anchor for artifact-only verification.",
    )
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    try:
        verify_release(
            args.release, args.manifest, args.wheelhouse, artifact_manifest=args.artifact_manifest
        )
        if args.smoke:
            smoke_portable(args.release, read_only=False)
            smoke_portable(args.release, read_only=True)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"release verification failed: {error}", file=sys.stderr)
        return 1
    print("release verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
