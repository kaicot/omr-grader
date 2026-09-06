"""Opt-in, runner-scoped readiness evidence for disposable portable smoke copies."""

from __future__ import annotations

import json
import os
import ctypes
from ctypes import wintypes
from hashlib import sha256
from pathlib import Path
from typing import Callable

from omr_grader.domain.errors import Ok

_MODE = "OMR_GRADER_SMOKE_MODE"
_PHASE = "OMR_GRADER_SMOKE_PHASE"
_READY = "OMR_GRADER_SMOKE_READY_FILE"
_SCOPE = "OMR_GRADER_SMOKE_SCOPE_MARKER"
_NONCE = "OMR_GRADER_SMOKE_NONCE"


def _scope(root: Path) -> tuple[Path, str, str] | None:
    """Accept only a marker beside this root that binds its exact nonce and output."""
    mode = os.environ.get(_MODE)
    phase = os.environ.get(_PHASE)
    ready_value = os.environ.get(_READY)
    scope_value = os.environ.get(_SCOPE)
    nonce = os.environ.get(_NONCE)
    if (
        mode not in {"writable", "readonly"}
        or phase not in {"write", "read", "readonly"}
        or (mode == "writable" and phase not in {"write", "read"})
        or (mode == "readonly" and phase != "readonly")
        or not ready_value
        or not scope_value
        or not nonce
    ):
        return None
    try:
        resolved_root = root.resolve(strict=True)
        marker = Path(scope_value).resolve(strict=True)
        ready = Path(ready_value).resolve(strict=False)
        if (
            marker.parent != resolved_root.parent
            or ready.parent != resolved_root.parent
            or ready == resolved_root
        ):
            return None
        document = json.loads(marker.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or set(document) != {"schema", "nonce", "app_root", "ready_file"}
            or document["schema"] != 1
            or document["app_root"] != str(resolved_root)
            or document["nonce"] != nonce
            or document["ready_file"] != str(ready)
            or ready.exists()
            or not ready.parent.is_dir()
        ):
            return None
        return ready, mode, phase
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_exclusive(path: Path, payload: dict[str, object], nonce: str) -> None:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    temporary = path.with_name(f".{path.name}.{nonce}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.MoveFileExW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        kernel32.MoveFileExW.restype = wintypes.BOOL
        if not kernel32.MoveFileExW(str(temporary), str(path), 0):
            raise OSError(ctypes.get_last_error(), "exclusive ready-file publication failed")
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def observe_main_ready(
    root: Path,
    *,
    write_enabled: bool,
    settings_load: Callable[..., object] | None,
    settings_save: Callable[..., object] | None,
    settings_command: Callable[[object, int, int], object] | None,
    session_persistence_available: bool,
) -> None:
    """Emit one runner-owned JSON record only after the caller enters Qt's loop.

    The caller intentionally queues this after the main window is visible.  The
    observer does nothing unless the runner proves ownership using the marker;
    it therefore cannot add files to an installed application from inherited
    smoke variables.
    """
    scope = _scope(root)
    if scope is None:
        return
    ready, mode, phase = scope
    persistence_roundtrip = False
    sensitivity: int | None = None
    config_hash: str | None = None
    if mode == "writable" and write_enabled and settings_load is not None:
        loaded = settings_load()
        if isinstance(loaded, Ok):
            value = loaded.value
            settings = getattr(value, "settings", None)
            revision = getattr(value, "revision", None)
            current = getattr(settings, "default_sensitivity", None)
            if type(current) is int:
                sensitivity = current
            if (
                phase == "write"
                and settings_save is not None
                and settings_command is not None
                and settings is not None
                and type(revision) is int
                and type(current) is int
            ):
                target = 6 if current != 6 else 7
                saved = settings_save(settings_command(settings, revision, target))
                reloaded = settings_load() if isinstance(saved, Ok) else None
                reloaded_settings = (
                    getattr(reloaded.value, "settings", None)
                    if isinstance(reloaded, Ok)
                    else None
                )
                sensitivity = getattr(reloaded_settings, "default_sensitivity", None)
                # The second process performs the cross-process comparison.  A
                # write-phase reload merely reports the authoritative saved
                # settings through the ordinary application path.
                persistence_roundtrip = False
            elif phase == "read":
                persistence_roundtrip = type(sensitivity) is int and sensitivity in {6, 7}
    config = root / "config.json"
    if mode == "writable" and type(sensitivity) is int and config.is_file():
        try:
            config_hash = sha256(config.read_bytes()).hexdigest()
        except OSError:
            sensitivity = None
    persistence: dict[str, object] | None = (
        {"default_sensitivity": sensitivity, "config_sha256": config_hash}
        if mode == "writable" and type(sensitivity) is int and config_hash is not None
        else None
    )
    payload: dict[str, object] = {
        "schema": 1,
        "pid": os.getpid(),
        "state": "main-ready",
        "read_only": mode == "readonly",
        "write_enabled": mode == "writable" and write_enabled,
        "affordances": {
            "config_persistence": bool(
                mode == "writable" and write_enabled and settings_save is not None
            ),
            "session_persistence": bool(
                mode == "writable" and write_enabled and session_persistence_available
            ),
        },
        "persistence_roundtrip": persistence_roundtrip,
        "persistence": persistence,
    }
    try:
        _write_exclusive(ready, payload, os.environ[_NONCE])
    except OSError:
        # A bad runner path must never turn application readiness into a write
        # attempt against a different location or a normal-startup failure.
        return
