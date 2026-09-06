from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure import atomic_io
from omr_grader.infrastructure.atomic_io import atomic_write_bytes, atomic_write_json


def test_atomic_write_replaces_target_from_a_sibling_temp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "config.json"
    destination.write_bytes(b"old")
    replacements: list[tuple[Path, Path]] = []
    real_replace = atomic_io._replace_durably

    def recording_replace(source: Path | str, target: Path | str) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(atomic_io, "_replace_durably", recording_replace)

    result = atomic_write_bytes(destination, b"new")

    assert isinstance(result, Ok)
    assert destination.read_bytes() == b"new"
    assert len(replacements) == 1
    temporary, target = replacements[0]
    assert target == destination
    assert temporary.parent == destination.parent
    assert temporary.name.startswith(".config.json.")
    assert temporary.name.endswith(".tmp")
    assert not temporary.exists()


def test_atomic_write_cleans_up_sibling_temp_file_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "config.json"
    temporary = tmp_path / ".config.json.failed.tmp"
    monkeypatch.setattr(atomic_io, "_temp_path", lambda target: temporary)

    def denied_replace(source: Path | str, target: Path | str) -> None:
        raise OSError("replacement denied")

    monkeypatch.setattr(atomic_io, "_replace_durably", denied_replace)

    result = atomic_write_bytes(destination, b"new")

    assert isinstance(result, Err)
    assert result.errors[0].code == "ATOMIC_WRITE_FAILED"
    assert "원자적으로" in str(result.errors[0].context["reason"])
    assert not temporary.exists()
    assert not destination.exists()


def test_atomic_write_preserves_old_target_and_cleans_temp_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "config.json"
    destination.write_bytes(b"old")
    temporary = tmp_path / ".config.json.failed.tmp"
    monkeypatch.setattr(atomic_io, "_temp_path", lambda target: temporary)
    monkeypatch.setattr(
        atomic_io,
        "_replace_durably",
        lambda source, target: (_ for _ in ()).throw(OSError("replacement denied")),
    )

    result = atomic_write_bytes(destination, b"new")

    assert isinstance(result, Err)
    assert result.errors[0].cause_type == "OSError"
    assert destination.read_bytes() == b"old"
    assert not temporary.exists()


def test_posix_durable_replace_syncs_parent_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def record_replace(temporary: Path, target: Path) -> None:
        calls.append(("replace", (temporary, target)))

    def record_open(path: Path, flags: int) -> int:
        calls.append(("open", (path, flags)))
        return 41

    def record_fsync(descriptor: int) -> None:
        calls.append(("fsync", descriptor))

    def record_close(descriptor: int) -> None:
        calls.append(("close", descriptor))

    monkeypatch.setattr(atomic_io.sys, "platform", "linux")
    monkeypatch.setattr(atomic_io.os, "replace", record_replace)
    monkeypatch.setattr(atomic_io.os, "open", record_open)
    monkeypatch.setattr(atomic_io.os, "fsync", record_fsync)
    monkeypatch.setattr(atomic_io.os, "close", record_close)

    atomic_io._replace_durably(Path("temporary"), Path("parent") / "target")

    assert calls[0] == ("replace", (Path("temporary"), Path("parent") / "target"))
    assert calls[1][0] == "open"
    assert calls[2:] == [("fsync", 41), ("close", 41)]


def test_posix_durable_replace_closes_parent_descriptor_when_sync_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    monkeypatch.setattr(atomic_io.sys, "platform", "linux")
    monkeypatch.setattr(atomic_io.os, "replace", lambda temporary, target: None)
    monkeypatch.setattr(atomic_io.os, "open", lambda path, flags: 41)
    monkeypatch.setattr(
        atomic_io.os, "fsync", lambda descriptor: (_ for _ in ()).throw(OSError("sync failed"))
    )
    monkeypatch.setattr(atomic_io.os, "close", closed.append)

    with pytest.raises(OSError, match="sync failed"):
        atomic_io._replace_durably(Path("temporary"), Path("parent") / "target")

    assert closed == [41]


class _MoveFileEx:
    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, temporary: str, target: str, flags: int) -> int:
        self.calls.append((temporary, target, flags))
        return self.result


def test_windows_durable_replace_requests_write_through(monkeypatch: pytest.MonkeyPatch) -> None:
    move_file_ex = _MoveFileEx(1)
    kernel = type("Kernel", (), {"MoveFileExW": move_file_ex})()
    monkeypatch.setattr(atomic_io.sys, "platform", "win32")
    monkeypatch.setattr(atomic_io.ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)

    atomic_io._replace_durably(Path("temporary"), Path("target"))

    assert move_file_ex.calls == [("temporary", "target", 0x00000001 | 0x00000008)]


def test_windows_durable_replace_surfaces_write_through_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    move_file_ex = _MoveFileEx(0)
    kernel = type("Kernel", (), {"MoveFileExW": move_file_ex})()
    monkeypatch.setattr(atomic_io.sys, "platform", "win32")
    monkeypatch.setattr(atomic_io.ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(atomic_io.ctypes, "get_last_error", lambda: 5)
    monkeypatch.setattr(
        atomic_io.ctypes,
        "WinError",
        lambda code: OSError(code, "write-through failed"),
        raising=False,
    )

    with pytest.raises(OSError, match="write-through failed"):
        atomic_io._replace_durably(Path("temporary"), Path("target"))


def test_atomic_write_rejects_non_bytes_payload(tmp_path: Path) -> None:
    result = atomic_write_bytes(tmp_path / "config.json", "text")  # type: ignore[arg-type]

    assert isinstance(result, Err)
    assert result.errors[0].code == "INVALID_WRITE_PAYLOAD"


def test_atomic_write_json_emits_nfc_sorted_compact_utf8_bytes_with_one_lf(tmp_path: Path) -> None:
    destination = tmp_path / "config.json"

    result = atomic_write_json(
        destination,
        {"b": [True, 1, None], "a": "cafe\u0301"},
    )

    expected = b'{"a":"caf\xc3\xa9","b":[true,1,null]}\n'
    assert isinstance(result, Ok)
    assert destination.read_bytes() == expected
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == (
        "8728107ee59fb8c2e5bc3b16ce45d7a5ce23f749df93ab5cb9fb53bf782bdde6"
    )


def test_atomic_write_json_rejects_normalized_duplicate_keys_and_nan(tmp_path: Path) -> None:
    destination = tmp_path / "config.json"
    destination.write_bytes(b"old")

    duplicate_keys = atomic_write_json(destination, {"e\u0301": "first", "\u00e9": "second"})
    nan = atomic_write_json(destination, {"value": float("nan")})

    assert isinstance(duplicate_keys, Err)
    assert duplicate_keys.errors[0].code == "INVALID_JSON_VALUE"
    assert isinstance(nan, Err)
    assert nan.errors[0].code == "INVALID_JSON_VALUE"
    assert destination.read_bytes() == b"old"
