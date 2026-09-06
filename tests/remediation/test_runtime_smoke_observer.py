from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from omr_grader.domain.errors import Ok
from omr_grader.infrastructure.smoke_observer import observe_main_ready


@dataclass
class _Settings:
    default_profile: str = ""
    default_sensitivity: int = 5
    use_multiprocessing: bool = True


@dataclass
class _State:
    settings: _Settings
    revision: int = 1


def _scope(monkeypatch, root: Path, ready: Path, marker: Path, *, mode: str, phase: str) -> None:
    nonce = "nonce-123"
    marker.write_text(
        json.dumps(
            {
                "schema": 1,
                "nonce": nonce,
                "app_root": str(root.resolve()),
                "ready_file": str(ready.resolve(strict=False)),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OMR_GRADER_SMOKE_MODE", mode)
    monkeypatch.setenv("OMR_GRADER_SMOKE_PHASE", phase)
    monkeypatch.setenv("OMR_GRADER_SMOKE_NONCE", nonce)
    monkeypatch.setenv("OMR_GRADER_SMOKE_SCOPE_MARKER", str(marker))
    monkeypatch.setenv("OMR_GRADER_SMOKE_READY_FILE", str(ready))


def test_readonly_smoke_observer_requires_sibling_scope_and_never_persists(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "OMR Grader"
    root.mkdir()
    ready, marker = tmp_path / "readonly-ready.json", tmp_path / "scope.json"
    _scope(monkeypatch, root, ready, marker, mode="readonly", phase="readonly")

    observe_main_ready(
        root,
        write_enabled=False,
        settings_load=None,
        settings_save=None,
        settings_command=None,
        session_persistence_available=False,
    )

    result = json.loads(ready.read_text(encoding="utf-8"))
    assert result["state"] == "main-ready"
    assert result["read_only"] and not result["write_enabled"]
    assert result["affordances"] == {"config_persistence": False, "session_persistence": False}
    assert result["persistence_roundtrip"] is False
    assert result["persistence"] is None
    assert not (root / "config.json").exists()


def test_writable_write_phase_reports_actual_saved_config(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "OMR Grader"
    root.mkdir()
    config = root / "config.json"
    config.write_text('{"default_sensitivity":5}', encoding="utf-8")
    ready, marker = tmp_path / "write-ready.json", tmp_path / "scope.json"
    _scope(monkeypatch, root, ready, marker, mode="writable", phase="write")
    state = _State(_Settings())

    def load() -> Ok[_State]:
        return Ok(state)

    def command(settings: _Settings, revision: int, sensitivity: int) -> tuple[_Settings, int]:
        return _Settings(settings.default_profile, sensitivity, settings.use_multiprocessing), revision

    def save(value: tuple[_Settings, int]) -> Ok[object]:
        settings, _ = value
        state.settings = settings
        config.write_text(
            json.dumps({"default_sensitivity": settings.default_sensitivity}), encoding="utf-8"
        )
        return Ok(object())

    observe_main_ready(
        root,
        write_enabled=True,
        settings_load=load,
        settings_save=save,
        settings_command=command,
        session_persistence_available=True,
    )

    result = json.loads(ready.read_text(encoding="utf-8"))
    assert result["persistence_roundtrip"] is False
    assert result["persistence"]["default_sensitivity"] in {6, 7}
    assert result["persistence"]["phase"] == "written"
    assert len(result["persistence"]["config_sha256"]) == 64


def test_readonly_smoke_reports_an_unexpected_actual_write_authority(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "OMR Grader"
    root.mkdir()
    ready, marker = tmp_path / "mismatch-ready.json", tmp_path / "scope.json"
    _scope(monkeypatch, root, ready, marker, mode="readonly", phase="readonly")

    observe_main_ready(
        root,
        write_enabled=True,
        settings_load=lambda: Ok(_State(_Settings())),
        settings_save=lambda _command: Ok(object()),
        settings_command=lambda settings, revision, sensitivity: (settings, revision, sensitivity),
        session_persistence_available=True,
    )

    result = json.loads(ready.read_text(encoding="utf-8"))
    assert result["read_only"] is False
    assert result["write_enabled"] is True
    assert result["affordances"] == {"config_persistence": True, "session_persistence": True}
    assert result["persistence"] is None
    assert result["persistence_roundtrip"] is False
    assert not (root / "config.json").exists()
