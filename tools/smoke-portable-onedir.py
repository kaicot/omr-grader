"""Smoke-test the current PyInstaller onedir portable contract on Windows."""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def _visible_titles(process_id: int) -> list[str]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user32.EnumWindows.restype = ctypes.c_bool
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_bool
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    titles: list[str] = []

    def collect(window: int, _parameter: int) -> bool:
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value != process_id or not user32.IsWindowVisible(window):
            return True
        length = user32.GetWindowTextLengthW(window)
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(window, buffer, len(buffer))
            titles.append(buffer.value)
        return True

    if not user32.EnumWindows(callback_type(collect), 0):
        raise RuntimeError(f"Could not enumerate windows: {ctypes.get_last_error()}")
    return titles


def _close_windows(process_id: int) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user32.EnumWindows.restype = ctypes.c_bool
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.SendMessageTimeoutW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = ctypes.c_size_t

    def close(window: int, _parameter: int) -> bool:
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == process_id:
            result = ctypes.c_size_t()
            if not user32.SendMessageTimeoutW(
                window,
                0x0112,
                0xF060,
                0,
                0x0002,
                5000,
                ctypes.byref(result),
            ):
                raise RuntimeError(f"Could not close window: {ctypes.get_last_error()}")
        return True

    if not user32.EnumWindows(callback_type(close), 0):
        raise RuntimeError(f"Could not enumerate windows for close: {ctypes.get_last_error()}")


def _copy_release(source: Path) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    temporary = tempfile.TemporaryDirectory(prefix="omr-grader-onedir-smoke-")
    root = Path(temporary.name) / "OMR Grader"
    shutil.copytree(source, root)
    return root, temporary


def writable_smoke(source: Path, *, require_graceful_close: bool) -> None:
    root, temporary = _copy_release(source)
    process: subprocess.Popen[str] | None = None
    try:
        executable = root / "OMR Grader.exe"
        process = subprocess.Popen([str(executable)], cwd=root)
        deadline = time.monotonic() + 30
        titles: list[str] = []
        while time.monotonic() < deadline and process.poll() is None:
            titles = _visible_titles(process.pid)
            if "OMR Grader" in titles:
                break
            time.sleep(0.25)
        if process.poll() is not None:
            raise RuntimeError(f"startup exit {process.returncode}")
        if "OMR Grader" not in titles:
            raise RuntimeError(f"window title missing: {titles!r}")
        _close_windows(process.pid)
        graceful_close = True
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            graceful_close = False
            process.kill()
            process.wait(timeout=5)
            if require_graceful_close:
                raise RuntimeError("close timeout")
        if process.returncode != 0 and graceful_close:
            raise RuntimeError(f"close exit {process.returncode}")
        entries = {item.name for item in root.iterdir()}
        allowed = {
            "OMR Grader.exe",
            "_internal",
            "config.json",
            "Profiles",
            "Data",
            "logs",
            ".locks",
            ".reservations",
            ".deleting",
        }
        unexpected = sorted(entries - allowed)
        if unexpected:
            raise RuntimeError(f"unexpected portable-root entries: {unexpected}")
        print(
            "writable smoke passed: "
            f"titles={titles!r}, graceful_close={'pass' if graceful_close else 'inconclusive'}"
        )
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        temporary.cleanup()


def read_only_smoke(source: Path) -> None:
    root, temporary = _copy_release(source)
    process: subprocess.Popen[str] | None = None
    identity = subprocess.check_output(
        ["whoami"], text=True, encoding="utf-8", errors="replace"
    ).strip()
    denied = False
    before = sorted(item.name for item in root.iterdir())
    try:
        command = ["icacls", str(root), "/deny", f"{identity}:(WD,AD,DC,DE)"]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"could not deny portable-root writes: {result.stderr}")
        denied = True
        try:
            process = subprocess.Popen([str(root / "OMR Grader.exe")], cwd=root)
            time.sleep(8)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        except PermissionError:
            pass
        after = sorted(item.name for item in root.iterdir())
        if before != after:
            raise RuntimeError(f"read-only root changed: before={before}, after={after}")
        print("read-only smoke passed: portable root unchanged")
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if denied:
            subprocess.run(
                ["icacls", str(root), "/remove:d", identity],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        temporary.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--require-graceful-close", action="store_true")
    arguments = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("onedir smoke testing requires Windows")
    if arguments.read_only:
        read_only_smoke(arguments.release.resolve())
    else:
        writable_smoke(
            arguments.release.resolve(),
            require_graceful_close=arguments.require_graceful_close,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
