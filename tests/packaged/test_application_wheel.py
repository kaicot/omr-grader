from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MISSING = object()


def _load_application_wheel():
    tools = Path(__file__).parents[2] / "tools"
    dependency_names = ("validate_canonical_wheel", "supply_manifest")
    originals = {name: sys.modules.pop(name, _MISSING) for name in dependency_names}
    try:
        for dependency_name in dependency_names:
            dependency_spec = importlib.util.spec_from_file_location(
                dependency_name, tools / f"{dependency_name}.py"
            )
            assert dependency_spec and dependency_spec.loader
            dependency = importlib.util.module_from_spec(dependency_spec)
            sys.modules[dependency_name] = dependency
            dependency_spec.loader.exec_module(dependency)

        spec = importlib.util.spec_from_file_location(
            "build_application_wheel_test", tools / "build_application_wheel.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for dependency_name, original in originals.items():
            if original is _MISSING:
                sys.modules.pop(dependency_name, None)
            else:
                sys.modules[dependency_name] = original


APPLICATION_WHEEL = _load_application_wheel()
SourceTreeError = APPLICATION_WHEEL.SourceTreeError
canonical_source_hash = APPLICATION_WHEEL.canonical_source_hash
pip_wheel_command = APPLICATION_WHEEL.pip_wheel_command
source_manifest = APPLICATION_WHEEL.source_manifest

def test_isolated_loader_restores_dependency_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    originals = {
        "validate_canonical_wheel": object(),
        "supply_manifest": object(),
    }
    for name, module in originals.items():
        monkeypatch.setitem(sys.modules, name, module)

    _load_application_wheel()

    for name, module in originals.items():
        assert sys.modules[name] is module


def test_pip_command_is_offline_no_dependency_non_isolated_wheel_build(tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    source = tmp_path / "source"
    destination = tmp_path / "wheel"
    command = pip_wheel_command(python, source, destination)
    assert command == [
        str(python),
        "-m",
        "pip",
        "wheel",
        "--no-deps",
        "--no-build-isolation",
        "--no-index",
        "--wheel-dir",
        str(destination),
        str(source),
    ]


def test_source_manifest_is_stable_and_excludes_build_cache_outputs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "generated.py").write_text("ignored", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "main.pyc").write_bytes(b"ignored")
    before = canonical_source_hash(tmp_path)
    (tmp_path / "build" / "generated.py").write_text("changed", encoding="utf-8")
    assert canonical_source_hash(tmp_path) == before
    assert [record[0] for record in source_manifest(tmp_path)] == ["src/main.py"]


def test_source_manifest_rejects_link_aliases(tmp_path: Path) -> None:
    target = tmp_path / "actual.py"
    target.write_text("x = 1\n", encoding="utf-8")
    alias = tmp_path / "alias.py"
    try:
        alias.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(SourceTreeError, match="link"):
        source_manifest(tmp_path)
