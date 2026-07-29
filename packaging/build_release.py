"""Offline, reproducible-input Windows release builder for OMR Grader."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import venv
import zipfile
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from setuptools._vendor.packaging.markers import default_environment
from setuptools._vendor.packaging.requirements import InvalidRequirement, Requirement

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = PROJECT_ROOT / "packaging" / "OMR_Grader.spec"
EXE_NAME = "OMR Grader.exe"
MANIFEST_FIELDS = frozenset({"version", "wheels"})
WHEEL_FIELDS = frozenset(
    {"filename", "size", "sha256", "license", "provenance", "acquisition_record_id"}
)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ACQUISITION_RECORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}\Z")
RELEASE_PAYLOAD = frozenset({EXE_NAME, "release-receipt.json"})


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
    """Validate the exact, auditable offline wheel manifest."""
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
        if not _substantive_text(item["license"]):
            raise ValueError(f"Missing substantive license for {filename}")
        if not _substantive_text(item["provenance"]):
            raise ValueError(f"Missing substantive provenance for {filename}")
        record = item["acquisition_record_id"]
        if not isinstance(record, str) or not ACQUISITION_RECORD_RE.fullmatch(record):
            raise ValueError(f"Invalid acquisition record identifier for {filename}")
        wheel = wheelhouse / filename
        if not wheel.is_file():
            raise ValueError(f"Manifest wheel is missing: {filename}")
        if wheel.stat().st_size != item["size"]:
            raise ValueError(f"Wheel size mismatch: {filename}")
        if sha256_file(wheel) != item["sha256"]:
            raise ValueError(f"Wheel SHA256 mismatch: {filename}")

    actual = {path.name for path in wheelhouse.glob("*.whl")}
    if actual != declared:
        raise ValueError("Wheelhouse filenames do not exactly match the manifest.")
    return manifest


def _windows_marker_environment() -> dict[str, str]:
    """PEP 508 environment for the interpreter that will run the Windows release."""
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


def parse_requirement(requirement: str) -> Requirement:
    try:
        return Requirement(requirement)
    except InvalidRequirement as error:
        raise ValueError(f"Invalid Requires-Dist: {requirement!r}") from error


def wheel_metadata(wheel: Path) -> tuple[str, str, list[Requirement]]:
    """Return normalized distribution name, version, and PEP 508 dependencies."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ValueError("expected one .dist-info/METADATA file")
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as error:
        raise ValueError(f"Invalid wheel metadata in {wheel.name}: {error}") from error
    name = metadata.get("Name")
    version = metadata.get("Version")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(version, str)
        or not version.strip()
    ):
        raise ValueError(f"Wheel metadata lacks Name or Version: {wheel.name}")
    dependencies = [
        parse_requirement(requirement) for requirement in metadata.get_all("Requires-Dist", [])
    ]
    if any(dependency.url is not None for dependency in dependencies):
        raise ValueError(
            f"Requires-Dist direct reference is not bound to the wheelhouse: {wheel.name}"
        )
    return normalize_distribution_name(name), version, dependencies


def manifest_inventory(manifest: dict[str, Any], wheelhouse: Path) -> dict[str, str]:
    """Ensure the manifest closes every dependency selected by its extras."""
    inventory: dict[str, str] = {}
    requirements: dict[str, list[Requirement]] = {}
    requested_extras: dict[str, set[str]] = {}
    for item in manifest["wheels"]:
        name, version, dependencies = wheel_metadata(wheelhouse / item["filename"])
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


def assert_exact_release_payload(release_dir: Path) -> None:
    actual = {path.name for path in release_dir.iterdir()}
    if actual != RELEASE_PAYLOAD:
        raise RuntimeError(
            "Release payload does not exactly match the declared contract: "
            f"expected={sorted(RELEASE_PAYLOAD)}, actual={sorted(actual)}"
        )


def source_hashes() -> dict[str, str]:
    paths = [PROJECT_ROOT / "main.py", PROJECT_ROOT / "pyproject.toml", SPEC_PATH]
    paths.extend(sorted((PROJECT_ROOT / "src").rglob("*.py")))
    return {
        str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"): sha256_file(path) for path in paths
    }


def run(command: list[str], *, env: dict[str, str]) -> str:
    completed = subprocess.run(command, cwd=PROJECT_ROOT, env=env, text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError(
            f"Command failed ({' '.join(command)}):\n{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout.strip()


def tool_version(command: list[str], env: dict[str, str]) -> str:
    return run(command, env=env).splitlines()[0]


def isolated_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def create_isolated_venv(venv_dir: Path) -> Path:
    """Create a fresh venv; no build command may use the invoking interpreter."""
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    python = isolated_python(venv_dir)
    if not python.is_file():
        raise RuntimeError("Isolated virtual environment did not provide a Python interpreter.")
    return python


def install_wheels(
    manifest: dict[str, Any], wheelhouse: Path, python: Path, env: dict[str, str]
) -> None:
    wheels = [str((wheelhouse / item["filename"]).resolve()) for item in manifest["wheels"]]
    run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", *wheels], env=env)
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            ".",
        ],
        env=env,
    )


def environment_inventory(python: Path, venv_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    probe = (
        "import importlib.metadata as m, json, sys; "
        "print(json.dumps({'prefix': sys.prefix, 'base_prefix': sys.base_prefix, "
        "'distributions': sorted({'name': d.metadata['Name'], 'version': d.version} "
        "for d in m.distributions() if d.metadata.get('Name')}, key=lambda d: d['name'].lower())))"
    )
    try:
        result = json.loads(run([str(python), "-c", probe], env=env))
    except json.JSONDecodeError as error:
        raise RuntimeError("Could not read isolated environment inventory.") from error
    if (
        Path(result.get("prefix", "")).resolve() != venv_dir.resolve()
        or result["prefix"] == result["base_prefix"]
    ):
        raise RuntimeError("Build interpreter is not the newly created isolated environment.")
    distributions = result.get("distributions")
    if not isinstance(distributions, list):
        raise RuntimeError("Isolated environment inventory is malformed.")
    return {"prefix": str(venv_dir.resolve()), "distributions": distributions}


def verify_environment_closure(inventory: dict[str, Any], expected: dict[str, str]) -> None:
    installed: dict[str, str] = {}
    for item in inventory["distributions"]:
        if not isinstance(item, dict) or set(item) != {"name", "version"}:
            raise RuntimeError("Isolated environment inventory contains an invalid distribution.")
        name, version = item["name"], item["version"]
        if not isinstance(name, str) or not isinstance(version, str):
            raise RuntimeError("Isolated environment inventory contains an invalid distribution.")
        normalized = normalize_distribution_name(name)
        if normalized in installed:
            raise RuntimeError(f"Isolated environment has duplicate distribution: {normalized}")
        installed[normalized] = version
    allowed = set(expected) | {"omr-grader", "pip"}
    unexpected = sorted(set(installed) - allowed)
    missing = sorted(set(expected) - set(installed))
    wrong = sorted(name for name, version in expected.items() if installed.get(name) != version)
    if unexpected or missing or wrong or "omr-grader" not in installed:
        raise RuntimeError(
            "Isolated environment dependency closure failed: "
            f"unexpected={unexpected}, missing={missing}, wrong_versions={wrong}"
        )


def verify_runtime_imports(python: Path, env: dict[str, str]) -> None:
    probe = (
        "import multiprocessing; multiprocessing.freeze_support(); "
        "import PySide6, cv2, fitz, openpyxl, tzdata; "
        "import omr_grader.bootstrap; "
        "assert 'PySide6.QtWidgets' not in sys.modules"
    )
    run([str(python), "-c", "import sys; " + probe], env=env)
    child_probe = """
import multiprocessing

def child(queue):
    import omr_grader.bootstrap
    queue.put("ok")

if __name__ == "__main__":
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=child, args=(queue,))
    process.start()
    process.join(15)
    assert process.exitcode == 0 and queue.get(timeout=1) == "ok"
"""
    with tempfile.TemporaryDirectory(prefix="omr-grader-multiprocessing-") as temporary:
        path = Path(temporary) / "probe.py"
        path.write_text(child_probe, encoding="utf-8")
        run([str(python), str(path)], env=env)


def build_release(manifest_path: Path, wheelhouse: Path, output: Path) -> Path:
    if os.name != "nt":
        raise RuntimeError("Windows is required to produce a Windows executable.")
    manifest = load_manifest(manifest_path, wheelhouse)
    expected_inventory = manifest_inventory(manifest, wheelhouse)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise RuntimeError(f"Release output must not already exist: {output}")
    env = os.environ.copy()
    env.update({"PYTHONHASHSEED": "0", "PYTHONNOUSERSITE": "1", "SOURCE_DATE_EPOCH": "0"})
    with tempfile.TemporaryDirectory(
        prefix="omr-grader-release-staging-", dir=output.parent
    ) as temporary:
        staging_root = Path(temporary)
        staging = staging_root / "release"
        staging.mkdir()
        workpath = staging_root / ".pyinstaller-work"
        venv_dir = staging_root / "venv"
        python = create_isolated_venv(venv_dir)
        install_wheels(manifest, wheelhouse, python, env)
        inventory = environment_inventory(python, venv_dir, env)
        verify_environment_closure(inventory, expected_inventory)
        verify_runtime_imports(python, env)
        versions = {
            "python": tool_version([str(python), "--version"], env),
            "pip": tool_version([str(python), "-m", "pip", "--version"], env),
            "pyinstaller": tool_version([str(python), "-m", "PyInstaller", "--version"], env),
        }
        run(
            [
                str(python),
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--distpath",
                str(staging),
                "--workpath",
                str(workpath),
                str(SPEC_PATH),
            ],
            env=env,
        )
        executable = staging / EXE_NAME
        if not executable.is_file():
            raise RuntimeError(f"PyInstaller did not produce {executable}")
        warnings = [
            line
            for path in sorted(workpath.rglob("warn-*.txt"))
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        receipt = {
            "format": 1,
            "timestamp_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "executable": {
                "filename": EXE_NAME,
                "sha256": sha256_file(executable),
                "size": executable.stat().st_size,
            },
            "source_hashes": source_hashes(),
            "config_hashes": {
                "packaging/OMR_Grader.spec": sha256_file(SPEC_PATH),
                "packaging/build_release.py": sha256_file(Path(__file__).resolve()),
                "packaging/verify_release.py": sha256_file(
                    PROJECT_ROOT / "packaging" / "verify_release.py"
                ),
                "pyproject.toml": sha256_file(PROJECT_ROOT / "pyproject.toml"),
            },
            "builder_sources": {
                "packaging/build_release.py": sha256_file(Path(__file__).resolve()),
                "packaging/verify_release.py": sha256_file(
                    PROJECT_ROOT / "packaging" / "verify_release.py"
                ),
            },
            "wheel_manifest": {
                "filename": manifest_path.name,
                "sha256": sha256_file(manifest_path),
                "wheels": manifest["wheels"],
            },
            "environment_inventory": inventory,
            "tool_versions": versions,
            "warnings": warnings,
        }
        receipt_path = staging / "release-receipt.json"
        receipt_path.write_text(canonical_json(receipt) + "\n", encoding="utf-8")
        assert_exact_release_payload(staging)
        staging.replace(output)
    return output / "release-receipt.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build OMR Grader from a verified offline wheelhouse."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "dist" / "OMR_Grader_App")
    args = parser.parse_args()
    try:
        receipt = build_release(args.manifest, args.wheelhouse, args.output)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"release build failed: {error}", file=sys.stderr)
        return 1
    print(receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
