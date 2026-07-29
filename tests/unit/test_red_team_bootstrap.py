"""Adversarial checks for validation-token and bootstrap failure boundaries."""

from __future__ import annotations

import builtins
import logging
from pathlib import Path
from typing import Any

import pytest

from omr_grader import bootstrap as bootstrap_module
from omr_grader.application.validation_token import ResponseValidationToken, ValidatedBackup
from omr_grader.domain.errors import Err, Ok
from omr_grader.infrastructure.paths import ManagedPaths


def _opened(token_type: type[ResponseValidationToken] | type[ValidatedBackup], source: Path) -> Any:
    opened = token_type.open(str(source))
    assert isinstance(opened, Ok)
    return opened.value


@pytest.mark.parametrize(
    ("token_type", "consume_name", "closed_code", "replay_code"),
    [
        (
            ResponseValidationToken,
            "consume_for_import",
            "XLSX_VALIDATION_TOKEN_CLOSED",
            "XLSX_SOURCE_CHANGED",
        ),
        (ValidatedBackup, "consume_for_restore", "BACKUP_HANDLE_CLOSED", "BACKUP_SOURCE_CHANGED"),
    ],
)
def test_live_validation_tokens_fail_closed_after_close_and_consume_replay(
    tmp_path: Path,
    token_type: type[ResponseValidationToken] | type[ValidatedBackup],
    consume_name: str,
    closed_code: str,
    replay_code: str,
) -> None:
    source = tmp_path / "원본.xlsx"
    source.write_bytes(b"verified bytes")
    token = _opened(token_type, source)
    try:
        first = getattr(token, consume_name)()
        assert isinstance(first, Ok)
        assert first.value.read() == b"verified bytes"

        replay = getattr(token, consume_name)()
        assert isinstance(replay, Err)
        assert replay.errors[0].code == replay_code
    finally:
        assert isinstance(token.close(), Ok)

    closed_token = _opened(token_type, source)
    assert isinstance(closed_token.close(), Ok)
    after_close = getattr(closed_token, consume_name)()
    assert isinstance(after_close, Err)
    assert after_close.errors[0].code == closed_code
    revalidated = closed_token.revalidate()
    assert isinstance(revalidated, Err)
    assert revalidated.errors[0].code == closed_code


def test_bootstrap_missing_root_has_no_qt_import_or_outside_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing_root = tmp_path / "missing-portable-root"
    outside = tmp_path / "outside"
    outside.mkdir()
    paths = ManagedPaths.from_root(missing_root)
    imports: list[str] = []
    original_import = builtins.__import__

    def record_import(name: str, *args: object, **kwargs: object):
        imports.append(name)
        if name.startswith("PySide6"):
            raise AssertionError("bootstrap imported PySide6 before GUI launch")
        return original_import(name, *args, **kwargs)

    monkeypatch.chdir(outside)
    monkeypatch.setattr(builtins, "__import__", record_import)
    monkeypatch.setattr(
        bootstrap_module, "configure_logging", lambda log_path=None: Ok(logging.getLogger("test"))
    )

    result = bootstrap_module.bootstrap(paths)

    assert isinstance(result, Err)
    assert result.errors[0].code == "PORTABLE_ROOT_UNAVAILABLE"
    assert not missing_root.exists()
    assert tuple(outside.iterdir()) == ()
    assert not any(name.startswith("PySide6") for name in imports)


def test_bootstrap_denied_root_does_not_fall_back_outside_portable_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "portable"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    paths = ManagedPaths.from_root(root)

    monkeypatch.chdir(outside)
    logging_paths: list[Path | None] = []
    monkeypatch.setattr(
        bootstrap_module,
        "configure_logging",
        lambda log_path=None: logging_paths.append(log_path) or Ok(logging.getLogger("test")),
    )
    monkeypatch.setattr("omr_grader.infrastructure.capabilities.is_path_writable", lambda _: False)

    result = bootstrap_module.bootstrap(paths)

    assert isinstance(result, Ok)
    assert result.value.write_enabled is False
    assert not paths.config_path.exists()
    assert not paths.profiles_dir.exists()
    assert not paths.data_dir.exists()
    assert tuple(outside.iterdir()) == ()
    assert logging_paths == [None]
